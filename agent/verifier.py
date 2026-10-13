from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent.state import ActionRecord, RunState

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
    """Verify persisted outcomes, never just successful tool responses."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.results: list[VerificationResult] = []

    async def verify_actions(self, state: RunState, hub) -> list[VerificationResult]:
        self.results = []
        # Status and tags are mutable: only the last successful assignment can
        # be compared with the current system of record.
        latest_updates = {}
        for action in state.actions:
            if action.status == "succeeded" and action.connector == "helpdesk" and action.tool in ("set_status", "set_tags"):
                latest_updates[(action.tool, str(action.args.get("ticket_id", state.ticket_id)))] = action.key
        for action in state.actions:
            if action.status in ("started", "unknown"):
                self.results.append(VerificationResult(action.key, action.tool, {}, {}, False, "Action outcome is unresolved"))
            elif action.status == "succeeded":
                update = (action.tool, str(action.args.get("ticket_id", state.ticket_id)))
                if action.connector == "helpdesk" and action.tool in ("set_status", "set_tags") and latest_updates[update] != action.key:
                    continue
                self.results.append(await self.verify_action(action, hub, state))
        self._save_results()
        return self.results

    async def verify_action(self, action: ActionRecord, hub, state: RunState) -> VerificationResult:
        """Read back a known write; unsupported writes fail closed."""
        try:
            if (action.connector, action.tool) == ("payments", "create_refund"):
                return await self._verify_refund(action, hub)
            if action.connector == "helpdesk" and action.tool in ("add_reply", "add_internal_note"):
                return await self._verify_reply(action, hub, state)
            if action.connector == "helpdesk" and action.tool in ("set_status", "set_tags"):
                return await self._verify_ticket_update(action, hub, state)
            if (action.connector, action.tool) == ("inventory", "create_replacement_order"):
                rows = await hub.call_tool("inventory__list_replacements_for_order", {"order_id": action.args["original_order_id"]})
                rows = self._rows(rows, "replacements")
                found = next((row for row in rows if row.get("original_order_id") == action.args["original_order_id"]
                              and row.get("product_id") == action.args["product_id"]
                              and row.get("status") in ("pending", "shipped", "delivered")
                              and self._matches_identity(action, row)), None)
                absent = action.before is not None and not any(row.get("id") not in action.before.get("ids", []) for row in rows)
                return self._result(action, bool(found), {"replacement": found, "absent": absent}, "Replacement read back")
            if (action.connector, action.tool) == ("chat", "post_message"):
                rows = await hub.call_tool("chat__get_messages", {"channel_name": action.args["channel_name"], "limit": 1000})
                rows = self._rows(rows, "messages")
                found = next((row for row in rows if row.get("body") == action.args["body"].strip()
                              and row.get("author") == action.args.get("author", "relay-agent")
                              and self._matches_identity(action, row)), None)
                # This is a bounded history; absence cannot authorize a retry.
                return self._result(action, bool(found), {"message": found}, "Chat message read back")
            if (action.connector, action.tool) == ("chat", "request_approval"):
                approval_id = (action.result or {}).get("id")
                if not approval_id:
                    return self._result(action, False, {}, "Approval ID unknown; manual reconciliation required")
                actual = self._object(await hub.call_tool("chat__get_approval", {"approval_id": approval_id}))
                passed = actual.get("id") == approval_id and actual.get("summary") == action.args.get("summary") and actual.get("amount") == action.args.get("amount")
                return self._result(action, passed, actual, "Approval request read back")
            return self._result(action, False, {}, "No read-back verifier for this write; manual reconciliation required")
        except Exception as exc:
            logger.exception("Verification failed for action %s (%s.%s)", action.key, action.connector, action.tool)
            return self._result(action, False, {"error": str(exc)}, f"Verification failed: {exc}")

    @staticmethod
    def _object(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or "error" in value or value.get("isError"):
            raise ValueError(f"Invalid system-of-record response: {value!r}")
        return value

    @classmethod
    def _rows(cls, value: Any, key: str) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            value = cls._object(value)
            value = value.get(key)
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise ValueError("Invalid system-of-record list response")
        return value

    @staticmethod
    def _matches_identity(action: ActionRecord, row: dict[str, Any]) -> bool:
        result_id = (action.result or {}).get("id")
        if result_id is not None:
            return row.get("id") == result_id
        # Reconciliation without a returned ID needs a pre-write baseline;
        # an old, identical reply or replacement is not proof of this write.
        return action.before is not None and row.get("id") is not None and row["id"] not in action.before.get("ids", [])

    @staticmethod
    def _result(action: ActionRecord, passed: bool, actual: dict[str, Any], message: str) -> VerificationResult:
        return VerificationResult(action.key, action.tool, action.args, actual, passed,
                                  message if passed else f"Not verified: {message}")

    async def _verify_refund(self, action: ActionRecord, hub) -> VerificationResult:
        try:
            rows = self._rows(await hub.call_tool("payments__list_refunds", {"charge_id": action.args["charge_id"]}), "refunds")
            key = action.args.get("idempotency_key", action.key)
            found = next((row for row in rows if row.get("idempotency_key") == key), None)
            passed = bool(found and found.get("charge_id") == action.args["charge_id"]
                          and found.get("amount") == action.args["amount"] and found.get("status") == "succeeded")
            return self._result(action, passed, {"refund": found, "absent": found is None}, "Refund amount, charge and status match")
        except Exception as exc:
            logger.exception("Refund verification failed for action %s", action.key)
            return self._result(action, False, {"error": str(exc)}, f"Verification failed: {exc}")

    async def _verify_reply(self, action: ActionRecord, hub, state: RunState) -> VerificationResult:
        try:
            ticket_id = int(action.args.get("ticket_id", state.ticket_id))
            ticket = self._object(await hub.call_tool("helpdesk__get_ticket", {"ticket_id": ticket_id}))
            rows = self._rows(ticket.get("replies"), "replies")
            body = action.args.get("body", "").strip()
            internal = action.tool == "add_internal_note"
            found = next((row for row in rows if body and row.get("body") == body
                          and row.get("author") == action.args.get("author", "agent")
                          and bool(row.get("is_internal", False)) == internal
                          and self._matches_identity(action, row)), None)
            passed = ticket.get("id") == ticket_id and found is not None
            absent = ticket.get("id") == ticket_id and action.before is not None and not any(row.get("id") not in action.before.get("ids", []) for row in rows)
            return self._result(action, passed, {"reply": found, "absent": absent}, "Exact reply and visibility match")
        except Exception as exc:
            logger.exception("Reply verification failed for action %s", action.key)
            return self._result(action, False, {"error": str(exc)}, f"Verification failed: {exc}")

    async def _verify_ticket_update(self, action: ActionRecord, hub, state: RunState) -> VerificationResult:
        try:
            ticket_id = int(action.args.get("ticket_id", state.ticket_id))
            actual = self._object(await hub.call_tool("helpdesk__get_ticket", {"ticket_id": ticket_id}))
            field = "status" if action.tool == "set_status" else "tags"
            expected = action.args.get(field)
            value = actual.get(field)
            if field == "tags" and isinstance(expected, str) and isinstance(value, str):
                passed = {tag.strip() for tag in expected.split(",") if tag.strip()} == {tag.strip() for tag in value.split(",") if tag.strip()}
            else:
                passed = expected is not None and value == expected
            passed = passed and actual.get("id") == ticket_id
            # Replaying the same absolute assignment is safe after a valid
            # read-back shows a mismatch; this is not an append operation.
            absent = actual.get("id") == ticket_id and expected is not None and field in actual and not passed
            return self._result(action, passed, {field: value, "absent": absent}, f"Ticket {field} matches")
        except Exception as exc:
            logger.exception("Ticket update verification failed for action %s", action.key)
            return self._result(action, False, {"error": str(exc)}, f"Verification failed: {exc}")

    async def verify_completion(self, state: RunState, hub) -> Literal["resolved", "waiting_customer", "escalated"] | None:
        """Require a real terminal ticket outcome and a verified public reply."""
        try:
            ticket = self._object(await hub.call_tool("helpdesk__get_ticket", {"ticket_id": int(state.ticket_id)}))
            status = ticket.get("status")
            reply_keys = {action.key for action in state.actions if action.connector == "helpdesk" and action.tool == "add_reply"
                          and str(action.args.get("ticket_id")) == str(state.ticket_id)
                          and action.args.get("author", "agent") == "agent"}
            reply_verified = any(result.passed and result.action_key in reply_keys for result in self.results)
            passed = ticket.get("id") == int(state.ticket_id) and status in ("resolved", "closed", "waiting_customer", "escalated")
            if status != "escalated":
                passed = passed and reply_verified
            self.results.append(VerificationResult("completion", "get_ticket", {"terminal_status": True, "public_reply": True},
                                                   {"status": status, "public_reply_verified": reply_verified}, passed,
                                                   "Ticket outcome verified" if passed else "Terminal ticket outcome or public reply missing"))
            outcome: Literal["resolved", "waiting_customer", "escalated"] | None = None
            if status in ("closed", "resolved"):
                outcome = "resolved"
            elif status == "waiting_customer":
                outcome = "waiting_customer"
            elif status == "escalated":
                outcome = "escalated"
        except Exception as exc:
            logger.exception("Completion verification failed for ticket %s", state.ticket_id)
            self.results.append(VerificationResult("completion", "get_ticket", {}, {"error": str(exc)}, False, f"Completion verification failed: {exc}"))
            outcome = None
        self._save_results()
        return outcome if self.all_passed else None

    def _save_results(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "verification.json").write_text(json.dumps([vars(result) for result in self.results], indent=2), encoding="utf-8")

    @property
    def all_passed(self) -> bool:
        return all(result.passed for result in self.results)

    def summary(self) -> str:
        if not self.results:
            return "No actions to verify."
        passed = sum(result.passed for result in self.results)
        lines = [f"Verification: {passed}/{len(self.results)} passed"]
        lines.extend(f"  {'✅' if result.passed else '❌'} {result.tool}: {result.message}" for result in self.results)
        return "\n".join(lines)
