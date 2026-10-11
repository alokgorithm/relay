from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.state import RunState, Step, ActionRecord, HumanRequest
from agent.action_log import ActionLog
from agent.tracer import Tracer
from agent.policy import PolicyEngine
from agent.verifier import Verifier
from agent.hub import ConnectorHub
from agent.llm.base import LLMResponse
from agent.prompts.templates import (
    SYSTEM_PROMPT, UNDERSTAND_PROMPT, ACT_PROMPT,
    VERIFY_PROMPT, RESPOND_PROMPT, MISSING_INFO_PROMPT,
    ESCALATION_PROMPT,
)

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT_DIR / "runs"


# Built-in control tools (not connectors)
CONTROL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": "Replace the remaining plan steps. Call this after analyzing the ticket to set your action plan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "goal": {"type": "string", "description": "What this step should accomplish"},
                                "done_when": {"type": "string", "description": "How to verify this step is complete"},
                            },
                            "required": ["goal", "done_when"],
                        },
                        "description": "List of plan steps",
                    }
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Store a fact for later use. Use this to record observations, policy decisions, and important data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Fact name (e.g., 'order_details', 'refund_eligible')"},
                    "value": {"type": "string", "description": "Fact value or JSON string"},
                    "reason": {"type": "string", "description": "Why this fact matters"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate",
            "description": "Escalate to a human agent. Use when the situation is outside policy or requires human judgment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why escalation is needed"},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Signal that the current ticket is complete. Call after all actions, verification, and responses are done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Summary of what was done and the outcome"},
                },
                "required": ["summary"],
            },
        },
    },
]


class AgentLoop:
    """The core agent loop: understand → plan → act → observe → adapt → verify → respond.
    
    Processes a single ticket through the full lifecycle.
    State is persisted after every step for crash recovery.
    """
    
    def __init__(
        self,
        llm: Any,  # implements LLM protocol
        hub: ConnectorHub,
        policy: PolicyEngine,
        max_steps: int = 30,
    ):
        self.llm = llm
        self.hub = hub
        self.policy = policy
        self.max_steps = max_steps
    
    async def run_ticket(self, ticket_id: str, run_id: str | None = None) -> RunState:
        """Process a single ticket from start to finish."""
        is_resumed = False
        if run_id:
            run_dir = RUNS_DIR / run_id
            if (run_dir / "state.json").exists():
                state = RunState.load(run_dir)
                is_resumed = True
            else:
                state = RunState(ticket_id=ticket_id, run_id=run_id)
        else:
            state = RunState(ticket_id=ticket_id)
            run_dir = RUNS_DIR / state.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
        
        action_log = ActionLog(run_dir)
        tracer = Tracer(run_dir)
        verifier = Verifier(run_dir)
        
        tracer.event("run_started", {"ticket_id": ticket_id, "run_id": state.run_id, "resumed": is_resumed})
        
        try:
            # Reconcile any started or unknown actions from prior attempt
            await self._reconcile(state, action_log, tracer)
            
            # Phase 1: Understand the ticket (only if not already planned)
            if not state.plan:
                await self._understand(state, tracer)
                state.save(run_dir)
            else:
                if state.status in ("waiting_human", "waiting_customer"):
                    state.status = "running"
            
            # Check if we need to stop early (missing info, escalation)
            if state.status in ("waiting_customer", "escalated", "waiting_human"):
                tracer.run_completed(state.status, f"Stopped: {state.status}")
                self._write_summary(state, run_dir, verifier)
                return state
            
            # Phase 2: Execute plan
            await self._execute_plan(state, action_log, tracer, run_dir)
            
            # Phase 3: Verify
            if state.status in ("running", "resolved"):
                state.status = "verifying"
                state.save(run_dir)
                verification_results = await verifier.verify_actions(state, self.hub)
                tracer.verification({"results": [r.message for r in verification_results], "all_passed": verifier.all_passed})
            
            # Finalize
            state.status = "resolved"
            
            tracer.run_completed(state.status, f"Ticket {ticket_id} -> {state.status}")
            
        except Exception as e:
            logger.error(f"Error processing ticket {ticket_id}: {e}", exc_info=True)
            state.status = "failed"
            tracer.error(str(e))
        
        state.save(run_dir)
        self._write_summary(state, run_dir, verifier)
        return state

    async def _reconcile(self, state: RunState, action_log: ActionLog, tracer: Tracer) -> None:
        """Reconcile unconfirmed actions (started or unknown) before executing further."""
        unresolved = action_log.get_actions_needing_reconciliation(state)
        if not unresolved:
            return
        
        logger.info(f"Reconciling {len(unresolved)} unresolved action(s)...")
        for action in unresolved:
            tracer.event("reconciliation_started", {"key": action.key, "tool": action.tool})
            try:
                if action.tool == "create_refund":
                    charge_id = action.args.get("charge_id")
                    refunds_res = await self.hub.call_tool("payments__list_refunds", {"charge_id": charge_id})
                    refunds = refunds_res if isinstance(refunds_res, list) else refunds_res.get("refunds", [])
                    found = None
                    if isinstance(refunds, list):
                        for r in refunds:
                            if isinstance(r, dict) and r.get("idempotency_key") == action.key:
                                found = r
                                break
                    if found:
                        action_log.record_success(state, action.key, found)
                        tracer.event("reconciled", {"key": action.key, "status": "succeeded", "result": found})
                    else:
                        action_log.record_failure(state, action.key, "Reconciliation determined action was not applied")
                        tracer.event("reconciled", {"key": action.key, "status": "failed"})
                elif action.tool == "add_reply":
                    ticket_id = action.args.get("ticket_id", state.ticket_id)
                    ticket_data = await self.hub.call_tool("helpdesk__get_ticket", {"ticket_id": int(ticket_id)})
                    replies = ticket_data.get("replies", []) if isinstance(ticket_data, dict) else []
                    found = any(
                        r.get("author") == "agent" and action.args.get("body", "")[:40] in r.get("body", "")
                        for r in replies if isinstance(r, dict)
                    )
                    if found:
                        action_log.record_success(state, action.key, {"verified": True})
                        tracer.event("reconciled", {"key": action.key, "status": "succeeded"})
                    else:
                        action_log.record_failure(state, action.key, "Reply not found in ticket")
                        tracer.event("reconciled", {"key": action.key, "status": "failed"})
                else:
                    action_log.record_failure(state, action.key, "Reconciled as failed/unapplied")
                    tracer.event("reconciled", {"key": action.key, "status": "failed"})
            except Exception as e:
                logger.warning(f"Reconciliation error for {action.key}: {e}")
    
    async def _understand(self, state: RunState, tracer: Tracer) -> None:
        """Phase 1: Read ticket, choose playbook, extract intent, check for missing info."""
        # Get the ticket
        ticket = await self.hub.call_tool("helpdesk__get_ticket", {"ticket_id": int(state.ticket_id)})
        tracer.tool_call("helpdesk", "get_ticket", {"ticket_id": state.ticket_id})
        tracer.tool_result("helpdesk", "get_ticket", ticket)
        
        # Store ticket info as facts
        state.facts["ticket"] = ticket
        state.facts["ticket_email"] = ticket.get("customer_email", "")
        state.facts["ticket_subject"] = ticket.get("subject", "")
        state.facts["ticket_body"] = ticket.get("body", "")
        
        # Check for social engineering
        body = ticket.get("body", "")
        if self.policy.is_social_engineering(body):
            state.facts["social_engineering_detected"] = True
            state.facts["security_flag"] = "Social engineering attempt detected in ticket body"
            logger.warning(f"Social engineering detected in ticket {state.ticket_id}")
        
        # Load available playbooks
        playbooks = self._load_playbooks()
        
        # Build messages for LLM to understand the ticket
        system_msg = SYSTEM_PROMPT.format(
            policy_summary=self.policy.get_refund_policy_summary(),
            available_connectors=", ".join(self.hub.connectors.keys()),
        )
        
        understand_msg = UNDERSTAND_PROMPT.format(
            ticket_id=state.ticket_id,
            subject=ticket.get("subject", ""),
            body=body,
            customer_email=ticket.get("customer_email", ""),
            status=ticket.get("status", ""),
            playbooks_list="\n".join(f"- {name}: {pb['description']}" for name, pb in playbooks.items()),
        )
        
        state.messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": understand_msg},
        ]
        
        # Get available tools (all connectors + control tools)
        tools = self.hub.get_all_tools() + CONTROL_TOOLS
        
        # LLM call to understand and plan
        tracer.llm_call(len(state.messages), len(tools))
        response = self.llm.chat(state.messages, tools=tools)
        tracer.llm_response(response.content, len(response.tool_calls), response.usage)
        
        # Process tool calls from understanding phase
        if response.content:
            state.messages.append({"role": "assistant", "content": response.content})
        
        if response.has_tool_calls:
            await self._process_tool_calls(state, response, tracer, ActionLog(RUNS_DIR / state.run_id))
        
        # If no plan was set, try one more LLM call
        if not state.plan:
            state.messages.append({"role": "user", "content": "Please call 'update_plan' with your planned steps, and 'remember' to store key facts."})
            response = self.llm.chat(state.messages, tools=tools)
            if response.has_tool_calls:
                await self._process_tool_calls(state, response, tracer, ActionLog(RUNS_DIR / state.run_id))
    
    async def _execute_plan(self, state: RunState, action_log: ActionLog, tracer: Tracer, run_dir: Path) -> None:
        """Phase 2: Execute the plan step by step."""
        # Activate first step
        if state.plan and not state.get_active_step():
            state.plan[0].status = "active"
        
        tools = self.hub.get_all_tools() + CONTROL_TOOLS
        
        for iteration in range(self.max_steps):
            state.step_count += 1
            active_step = state.get_active_step()
            
            if not active_step:
                logger.info("No more active steps, plan complete")
                break
            
            if state.status not in ("running",):
                break
            
            tracer.step_started(active_step.id, active_step.goal)
            
            # Build the act prompt
            plan_display = "\n".join(
                f"  {'→' if s.status == 'active' else '  '} [{s.status}] Step {s.id}: {s.goal} (done when: {s.done_when})"
                for s in state.plan
            )
            
            recent_actions = "\n".join(
                f"  - {a.connector}.{a.tool}({a.args}) → {a.status}: {json.dumps(a.result) if a.result else 'pending'}"
                for a in state.actions[-5:]
            ) or "None yet"
            
            act_msg = ACT_PROMPT.format(
                plan_display=plan_display,
                facts=json.dumps(state.facts, indent=2, default=str),
                recent_actions=recent_actions,
                active_step=f"Step {active_step.id}: {active_step.goal}",
                ticket_id=state.ticket_id,
                step_id=active_step.id,
                tool_name="<tool>",
            )
            
            state.messages.append({"role": "user", "content": act_msg})
            
            # Send system prompt + current act context to stay within model token limits
            system_msg = state.messages[0] if (state.messages and state.messages[0].get("role") == "system") else {"role": "system", "content": "You are Relay, an autonomous customer support agent."}
            call_messages = [system_msg, {"role": "user", "content": act_msg}]
            
            # LLM decides what tool to call
            tracer.llm_call(len(call_messages), len(tools))
            response = self.llm.chat(call_messages, tools=tools)
            tracer.llm_response(response.content, len(response.tool_calls), response.usage)
            
            if response.content:
                state.messages.append({"role": "assistant", "content": response.content})
            
            if response.has_tool_calls:
                should_stop = await self._process_tool_calls(state, response, tracer, action_log)
                if should_stop:
                    break
                tracer.step_completed(active_step.id, "done")
                state.advance_step()
            else:
                # LLM didn't call any tools - advance to next step
                tracer.step_completed(active_step.id, "done")
                state.advance_step()
            
            state.save(run_dir)
            
            # Check if waiting for human
            if state.status in ("waiting_human", "waiting_customer", "escalated"):
                break
        else:
            logger.warning(f"Reached max steps ({self.max_steps}) for ticket {state.ticket_id}")
    
    async def _process_tool_calls(
        self, state: RunState, response: LLMResponse, tracer: Tracer, action_log: ActionLog
    ) -> bool:
        """Process tool calls from an LLM response. Returns True if the loop should stop."""
        # Build assistant message with tool calls for conversation history
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if response.content:
            assistant_msg["content"] = response.content
        else:
            assistant_msg["content"] = None
        
        assistant_msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in response.tool_calls
        ]
        state.messages.append(assistant_msg)
        
        should_stop = False
        
        for tc in response.tool_calls:
            tool_name = tc.name
            tool_args = tc.arguments
            
            logger.info(f"Tool call: {tool_name}({json.dumps(tool_args, default=str)[:200]})")
            
            # Handle control tools
            if tool_name == "update_plan":
                result = self._handle_update_plan(state, tool_args)
            elif tool_name == "remember":
                result = self._handle_remember(state, tool_args)
            elif tool_name == "escalate":
                result = await self._handle_escalate(state, tool_args, tracer)
                should_stop = True
            elif tool_name == "finish":
                result = self._handle_finish(state, tool_args)
                should_stop = True
            else:
                # Connector tool call
                result = await self._handle_connector_tool(
                    state, tool_name, tool_args, action_log, tracer
                )
            
            # Add tool result to conversation
            state.messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str) if isinstance(result, dict) else str(result),
            })
        
        return should_stop
    
    async def _handle_connector_tool(
        self, state: RunState, qualified_name: str, args: dict[str, Any],
        action_log: ActionLog, tracer: Tracer,
    ) -> dict[str, Any]:
        """Handle a tool call to a connector."""
        try:
            connector_name, tool_name = self.hub.parse_tool_name(qualified_name)
        except ValueError:
            return {"error": f"Invalid tool name: {qualified_name}"}
        
        # Check tool info
        tool_info = self.hub.get_tool_info(qualified_name)
        is_write = tool_info and not tool_info.is_read_only if tool_info else False
        
        # Policy check for write operations
        if is_write:
            policy_decision = self.policy.check_action(connector_name, tool_name, args, state.facts)
            
            if policy_decision.action == "deny":
                logger.warning(f"Policy denied: {qualified_name} - {policy_decision.reason}")
                tracer.event("policy_denied", {"tool": qualified_name, "reason": policy_decision.reason})
                return {"error": f"Policy denied: {policy_decision.reason}", "policy_rule": policy_decision.rule}
            
            if policy_decision.action == "needs_approval":
                # Request approval via chat
                approval_result = await self._request_approval(
                    state, qualified_name, args, policy_decision.reason, tracer
                )
                if not approval_result:
                    return {"error": "Approval denied or pending", "reason": policy_decision.reason}
        
        # Check hub-level approval requirements
        approval_rule = self.hub.check_approval_required(qualified_name, args)
        if approval_rule:
            approval_result = await self._request_approval(
                state, qualified_name, args, approval_rule["reason"], tracer
            )
            if not approval_result:
                return {"error": "Approval required but not granted", "reason": approval_rule["reason"]}
        
        # Generate idempotency key for write operations
        idempotency_key = None
        if is_write:
            active_step = state.get_active_step()
            step_id = active_step.id if active_step else 0
            idempotency_key = action_log.make_key(state.ticket_id, step_id, tool_name)
            
            # Check if already succeeded
            existing = state.has_action_with_key(idempotency_key)
            if existing and existing.status == "succeeded":
                logger.info(f"Action {idempotency_key} already succeeded, returning cached result")
                return existing.result or {"status": "already_completed"}
            
            # Write-ahead log
            action_log.record_start(state, idempotency_key, connector_name, tool_name, args)
        
        # Call the tool
        tracer.tool_call(connector_name, tool_name, args, idempotency_key)
        
        try:
            result = await self.hub.call_tool(qualified_name, args, idempotency_key)
            tracer.tool_result(connector_name, tool_name, result, success=True)
            
            if is_write and idempotency_key:
                action_log.record_success(state, idempotency_key, result)
            
            # Advance step if this was the active step's main action
            active_step = state.get_active_step()
            if active_step:
                # Let the LLM decide when to advance by checking done_when
                pass
            
            return result
            
        except TimeoutError as e:
            logger.warning(f"Timeout on {qualified_name}: {e}")
            tracer.tool_result(connector_name, tool_name, {"error": str(e)}, success=False)
            
            if is_write and idempotency_key:
                # Critical: mark as unknown, must reconcile before retry
                action_log.record_unknown(state, idempotency_key)
                return {
                    "error": "Timeout - action may have succeeded. MUST reconcile by reading state before retrying.",
                    "idempotency_key": idempotency_key,
                    "reconcile_hint": f"Read back from {connector_name} to check if the action completed.",
                }
            
            return {"error": str(e)}
            
        except Exception as e:
            logger.error(f"Error calling {qualified_name}: {e}")
            tracer.tool_result(connector_name, tool_name, {"error": str(e)}, success=False)
            
            if is_write and idempotency_key:
                action_log.record_failure(state, idempotency_key, str(e))
            
            return {"error": str(e)}
    
    async def _request_approval(
        self, state: RunState, tool_name: str, args: dict, reason: str, tracer: Tracer
    ) -> bool:
        """Request approval via the chat connector."""
        try:
            amount = args.get("amount")
            summary = f"Ticket #{state.ticket_id}: {tool_name} — {reason}"
            
            approval_result = await self.hub.call_tool(
                "chat__request_approval",
                {
                    "channel_name": "approvals",
                    "summary": summary,
                    "amount": amount,
                },
            )
            
            approval_id = approval_result.get("id")
            if approval_id:
                state.pending_human = HumanRequest(
                    type="approval",
                    approval_id=approval_id,
                    message=summary,
                )
                state.status = "waiting_human"
                tracer.event("approval_requested", {"approval_id": approval_id, "reason": reason})
                
                # Poll for approval (in real system this would be event-driven)
                # For demo, check immediately - human uses CLI to approve
                for _ in range(60):  # Wait up to 60 seconds
                    await asyncio.sleep(1)
                    check = await self.hub.call_tool("chat__get_approval", {"approval_id": approval_id})
                    status = check.get("status")
                    if status == "approved":
                        state.pending_human = None
                        state.status = "running"
                        tracer.event("approval_granted", {"approval_id": approval_id})
                        return True
                    elif status == "denied":
                        state.pending_human = None
                        state.status = "running"
                        tracer.event("approval_denied", {"approval_id": approval_id})
                        return False
                
                # Timeout waiting for approval
                logger.warning(f"Approval {approval_id} timed out")
                return False
                
        except Exception as e:
            logger.error(f"Error requesting approval: {e}")
            return False
    
    def _handle_update_plan(self, state: RunState, args: dict) -> dict:
        """Handle the update_plan control tool."""
        steps_data = args.get("steps", [])
        state.plan = [
            Step(id=i + 1, goal=s["goal"], done_when=s["done_when"])
            for i, s in enumerate(steps_data)
        ]
        if state.plan:
            state.plan[0].status = "active"
        logger.info(f"Plan updated: {len(state.plan)} steps")
        return {"status": "plan_updated", "steps": len(state.plan)}
    
    def _handle_remember(self, state: RunState, args: dict) -> dict:
        """Handle the remember control tool."""
        key = args.get("key", "")
        value = args.get("value", "")
        reason = args.get("reason", "")
        
        # Try to parse value as JSON
        try:
            parsed_value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            parsed_value = value
        
        state.facts[key] = parsed_value
        if reason:
            state.facts[f"{key}_reason"] = reason
        
        logger.info(f"Remembered: {key} = {str(value)[:100]}")
        return {"status": "remembered", "key": key}
    
    async def _handle_escalate(self, state: RunState, args: dict, tracer: Tracer) -> dict:
        """Handle the escalate control tool."""
        reason = args.get("reason", "No reason given")
        state.status = "escalated"
        state.facts["escalation_reason"] = reason
        
        tracer.event("escalated", {"reason": reason})
        
        # Post to support-ops
        try:
            await self.hub.call_tool("chat__post_message", {
                "channel_name": "support-ops",
                "body": f"🚨 Ticket #{state.ticket_id} escalated: {reason}",
            })
        except Exception as e:
            logger.error(f"Failed to post escalation to chat: {e}")
        
        # Add internal note
        try:
            await self.hub.call_tool("helpdesk__add_internal_note", {
                "ticket_id": int(state.ticket_id),
                "body": f"Escalated to human agent. Reason: {reason}",
            })
        except Exception as e:
            logger.error(f"Failed to add escalation note: {e}")
        
        # Set ticket status
        try:
            await self.hub.call_tool("helpdesk__set_status", {
                "ticket_id": int(state.ticket_id),
                "status": "escalated",
            })
        except Exception as e:
            logger.error(f"Failed to set ticket status: {e}")
        
        return {"status": "escalated", "reason": reason}
    
    def _handle_finish(self, state: RunState, args: dict) -> dict:
        """Handle the finish control tool."""
        summary = args.get("summary", "")
        state.facts["summary"] = summary
        
        # Mark remaining steps as done or skipped
        for step in state.plan:
            if step.status == "active":
                step.status = "done"
            elif step.status == "pending":
                step.status = "skipped"
        
        if state.status == "running":
            state.status = "resolved"
        
        logger.info(f"Ticket {state.ticket_id} finished: {summary[:100]}")
        return {"status": "finished", "summary": summary}
    
    def _load_playbooks(self) -> dict[str, dict]:
        """Load available playbooks from company/playbooks/."""
        playbooks_dir = ROOT_DIR / "company" / "playbooks"
        playbooks = {}
        
        if playbooks_dir.exists():
            for pb_file in playbooks_dir.glob("*.md"):
                name = pb_file.stem
                content = pb_file.read_text()
                # Extract first line as description
                first_line = content.strip().split("\n")[0].lstrip("# ").strip()
                playbooks[name] = {
                    "description": first_line,
                    "content": content,
                    "file": str(pb_file),
                }
        
        return playbooks
    
    def _write_summary(self, state: RunState, run_dir: Path, verifier: Verifier) -> None:
        """Write a human-readable summary of the run."""
        summary_file = run_dir / "summary.md"
        
        lines = [
            f"# Run Summary: Ticket #{state.ticket_id}",
            f"",
            f"**Run ID:** {state.run_id}",
            f"**Status:** {state.status}",
            f"**Playbook:** {state.playbook or 'auto-selected'}",
            f"**Steps:** {state.step_count}",
            f"**Created:** {state.created_at}",
            f"**Updated:** {state.updated_at}",
            f"",
            f"## Decision",
            f"{state.facts.get('summary', 'No summary recorded')}",
            f"",
            f"## Policy Reasoning",
        ]
        
        for key, value in state.facts.items():
            if key.endswith("_reason") or key.endswith("_eligible"):
                lines.append(f"- **{key}:** {value}")
        
        lines.extend([
            f"",
            f"## Actions",
        ])
        
        for action in state.actions:
            icon = "✅" if action.status == "succeeded" else "❌" if action.status == "failed" else "⏳"
            lines.append(f"- {icon} `{action.connector}.{action.tool}` — {action.status} (key: `{action.key}`)")
        
        lines.extend([
            f"",
            f"## Verification",
            verifier.summary() if verifier.results else "No verification performed.",
        ])
        
        if state.facts.get("social_engineering_detected"):
            lines.extend([
                f"",
                f"## ⚠️ Security Flags",
                f"- Social engineering attempt detected in ticket body",
                f"- {state.facts.get('security_flag', '')}",
            ])
        
        summary_file.write_text("\n".join(lines), encoding="utf-8")
