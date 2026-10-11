from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.state import RunState, ActionRecord

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    action_key: str
    tool: str
    expected: dict[str, Any]
    actual: dict[str, Any]
    passed: bool
    message: str


class Verifier:
    """Post-action verification.
    
    After the agent completes actions, the verifier re-reads state from
    the system of record to confirm that side effects actually occurred.
    """
    
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.results: list[VerificationResult] = []
    
    async def verify_actions(self, state: RunState, hub) -> list[VerificationResult]:
        """Verify all succeeded side-effecting actions."""
        self.results = []
        
        for action in state.actions:
            if action.status != "succeeded":
                continue
            
            if action.tool == "create_refund":
                result = await self._verify_refund(action, hub)
                self.results.append(result)
            elif action.tool == "add_reply":
                result = await self._verify_reply(action, hub, state)
                self.results.append(result)
            elif action.tool in ("set_status", "set_tags"):
                result = await self._verify_ticket_update(action, hub, state)
                self.results.append(result)
        
        self._save_results()
        return self.results
    
    async def _verify_refund(self, action: ActionRecord, hub) -> VerificationResult:
        """Verify a refund was actually created."""
        try:
            # Read back refunds for the order
            charge_id = action.args.get("charge_id")
            result = await hub.call_tool("payments__list_refunds", {"charge_id": charge_id})
            
            if isinstance(result, list):
                refunds = result
            elif isinstance(result, dict):
                if "refunds" in result:
                    refunds = result["refunds"]
                elif "id" in result:
                    refunds = [result]
                else:
                    refunds = []
            else:
                refunds = []
            
            # Check if our refund exists
            found = False
            if isinstance(refunds, list):
                for r in refunds:
                    if isinstance(r, dict) and r.get("idempotency_key") == action.key:
                        found = True
                        break
            
            # Also check by idempotency_key in args
            idem_key = action.args.get("idempotency_key", action.key)
            if not found and isinstance(refunds, list):
                for r in refunds:
                    if isinstance(r, dict) and r.get("idempotency_key") == idem_key:
                        found = True
                        break
            
            return VerificationResult(
                action_key=action.key,
                tool="create_refund",
                expected={"refund_exists": True, "idempotency_key": idem_key},
                actual={"refund_exists": found, "refunds": refunds},
                passed=found,
                message="Refund verified" if found else "Refund NOT found in system of record!",
            )
        except Exception as e:
            return VerificationResult(
                action_key=action.key,
                tool="create_refund",
                expected={"refund_exists": True},
                actual={"error": str(e)},
                passed=False,
                message=f"Verification failed: {e}",
            )
    
    async def _verify_reply(self, action: ActionRecord, hub, state: RunState) -> VerificationResult:
        """Verify a reply was posted to the ticket."""
        try:
            ticket_id = action.args.get("ticket_id", state.ticket_id)
            result = await hub.call_tool("helpdesk__get_ticket", {"ticket_id": int(ticket_id)})
            
            replies = result.get("replies", []) if isinstance(result, dict) else []
            reply_found = any(
                r.get("author") == "agent" and action.args.get("body", "")[:50] in r.get("body", "")
                for r in replies if isinstance(r, dict)
            )
            
            return VerificationResult(
                action_key=action.key,
                tool="add_reply",
                expected={"reply_posted": True},
                actual={"reply_found": reply_found, "total_replies": len(replies)},
                passed=reply_found,
                message="Reply verified" if reply_found else "Reply not found",
            )
        except Exception as e:
            return VerificationResult(
                action_key=action.key,
                tool="add_reply",
                expected={"reply_posted": True},
                actual={"error": str(e)},
                passed=False,
                message=f"Verification failed: {e}",
            )
    
    async def _verify_ticket_update(self, action: ActionRecord, hub, state: RunState) -> VerificationResult:
        """Verify ticket status/tags were updated."""
        try:
            ticket_id = action.args.get("ticket_id", state.ticket_id)
            result = await hub.call_tool("helpdesk__get_ticket", {"ticket_id": int(ticket_id)})
            
            if action.tool == "set_status":
                expected_status = action.args.get("status")
                actual_status = result.get("status") if isinstance(result, dict) else None
                passed = actual_status == expected_status
                return VerificationResult(
                    action_key=action.key,
                    tool="set_status",
                    expected={"status": expected_status},
                    actual={"status": actual_status},
                    passed=passed,
                    message=f"Status {'matches' if passed else 'mismatch'}",
                )
            else:  # set_tags
                expected_tags = action.args.get("tags", "")
                actual_tags = result.get("tags", "") if isinstance(result, dict) else ""
                passed = expected_tags in actual_tags
                return VerificationResult(
                    action_key=action.key,
                    tool="set_tags",
                    expected={"tags": expected_tags},
                    actual={"tags": actual_tags},
                    passed=passed,
                    message=f"Tags {'match' if passed else 'mismatch'}",
                )
        except Exception as e:
            return VerificationResult(
                action_key=action.key,
                tool=action.tool,
                expected={},
                actual={"error": str(e)},
                passed=False,
                message=f"Verification failed: {e}",
            )
    
    def _save_results(self) -> None:
        """Save verification results to JSON."""
        results_file = self.run_dir / "verification.json"
        results_data = [
            {
                "action_key": r.action_key,
                "tool": r.tool,
                "expected": r.expected,
                "actual": r.actual,
                "passed": r.passed,
                "message": r.message,
            }
            for r in self.results
        ]
        results_file.write_text(json.dumps(results_data, indent=2), encoding="utf-8")
    
    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)
    
    def summary(self) -> str:
        if not self.results:
            return "No actions to verify."
        passed = sum(1 for r in self.results if r.passed)
        total = len(self.results)
        lines = [f"Verification: {passed}/{total} passed"]
        for r in self.results:
            icon = "\u2705" if r.passed else "\u274c"
            lines.append(f"  {icon} {r.tool}: {r.message}")
        return "\n".join(lines)
