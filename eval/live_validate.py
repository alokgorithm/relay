"""Opt-in live model validation on fictional, isolated connector databases.

Run explicitly: python eval/live_validate.py --ticket 1
Ticket 2 never receives a manager decision unless --simulate-approval is supplied.
Imports do not instantiate a provider or run validation. Evidence is sanitized;
raw sandbox databases and provider configuration are never exported.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import uuid
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agent.loop import AgentLoop
from agent.policy import PolicyEngine
from agent.verifier import Verifier
from cli import create_llm, load_config
from connectors.common import db
from eval.run_eval import isolated_connectors

EXPECTED = {"1": "resolved", "2": "wait_human", "5": "escalated", "6": "resolved"}
ORDERS = {"1": 1042, "2": 1043, "5": 1046, "6": 1047}
LIMITATION = (
    "Fictional isolated fixtures; not production validation. Approval simulator "
    "is opt-in, outside the agent, and does not validate a real manager workflow. "
    "Usage counts returned responses; failed provider attempts may not return usage."
)
SENSITIVE_KEY = re.compile(
    r"api[_-]?key|authorization|password|secret|credential|access[_-]?token|refresh[_-]?token",
    re.IGNORECASE,
)


class Sanitizer:
    """Redact configured/environment credentials, including echoed error text."""

    def __init__(self, config: dict[str, Any]):
        self.secrets: set[str] = set()

        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if SENSITIVE_KEY.search(str(key)) and isinstance(item, str) and item:
                        self.secrets.add(item)
                    else:
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(config)
        for key, value in os.environ.items():
            if SENSITIVE_KEY.search(key) and value:
                self.secrets.add(value)
        endpoint = urlsplit(str(config.get("provider", {}).get("base_url", "")))
        self.secrets.update(value for value in (endpoint.username, endpoint.password) if value)
        self.secrets.update(value for _, value in parse_qsl(endpoint.query) if value)

    def text(self, value: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
        # URLs in provider errors can carry credentials, paths or query tokens.
        return re.sub(r"https?://[^\s\"'<>]+", "[URL REDACTED]", value)

    def clean(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                self.text(str(key)): "[REDACTED]" if SENSITIVE_KEY.search(str(key)) else self.clean(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def _write_json(path: Path, value: Any, sanitizer: Sanitizer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitizer.clean(value), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _rows(connector: str, table: str) -> list[dict[str, Any]]:
    # All identifiers are private constants below, never model/user input.
    path = db.DATA_DIR / f"{connector}.db"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY id')]


def snapshot(ticket: str) -> dict[str, Any]:
    """Read fresh persisted records, not model facts or cached tool results."""
    return {
        "payments": {table: _rows("payments", table) for table in ("charges", "refunds")},
        "approvals": _rows("chat", "approvals"),
        "messages": _rows("chat", "messages"),
        "ticket": next(row for row in _rows("helpdesk", "tickets") if row["id"] == int(ticket)),
        "replies": [row for row in _rows("helpdesk", "ticket_replies") if row["ticket_id"] == int(ticket)],
        "shipment": next(row for row in _rows("shop", "shipments") if row["order_id"] == ORDERS[ticket]),
    }


def _usage(state) -> dict[str, int]:
    raw = state.facts.get("_llm_usage", {}) if state else {}
    return {key: raw.get(key, 0) for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens")}


def _actual(state) -> str:
    return "wait_human" if state.status == "waiting_human" else state.status


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _copy_artifacts(runs_dir: Path, destination: Path, sanitizer: Sanitizer) -> None:
    if not runs_dir.exists():
        return
    for source in runs_dir.rglob("*"):
        if not source.is_file() or source.suffix not in (".json", ".jsonl", ".md", ".txt"):
            continue
        target = destination / "runtime" / source.relative_to(runs_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = source.read_text(encoding="utf-8", errors="replace")
        try:
            if source.suffix == ".json":
                _write_json(target, json.loads(text), sanitizer)
            elif source.suffix == ".jsonl":
                lines = [json.dumps(sanitizer.clean(json.loads(line)), ensure_ascii=False)
                         for line in text.splitlines() if line.strip()]
                target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            else:
                target.write_text(sanitizer.text(text), encoding="utf-8")
        except json.JSONDecodeError:
            target.write_text(sanitizer.text(text), encoding="utf-8")


async def validate_ticket(
    ticket: str, *, max_calls: int = 12, simulate_approval: bool = False,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Explicit reusable entry point; returns a report and preserves failed evidence.

    The same AgentLoop, provider and run ID are used for every resume, so the
    runtime's persisted call budget is never reset. This helper changes global
    sandbox paths through isolated_connectors and must not run concurrently.
    """
    ticket = str(ticket)
    if ticket not in EXPECTED:
        raise ValueError("ticket must be one of 1, 2, 5, 6")
    if type(max_calls) is not int or max_calls < 1:
        raise ValueError("max_calls must be a positive integer")
    if simulate_approval and ticket != "2":
        raise ValueError("--simulate-approval is only valid for ticket 2")

    validation_id = uuid.uuid4().hex[:12]
    destination = (output_dir if output_dir is not None else ROOT_DIR / "runs" / "live_validation") / validation_id
    destination.mkdir(parents=True, exist_ok=False)
    sanitizer = Sanitizer({})
    expected = "resolved" if simulate_approval else EXPECTED[ticket]
    report = {
        "run_id": validation_id, "ticket": ticket, "checked": False, "status": "failed",
        "expected": expected, "actual": "not_started", "usage": _usage(None),
        "provider": "", "model": "", "max_calls": max_calls,
        "simulate_approval": simulate_approval, "limitation": LIMITATION,
    }
    evidence: dict[str, Any] = {"fictional": True, "checks": {}}
    state = None
    try:
        config = deepcopy(load_config())
        sanitizer = Sanitizer(config)
        provider = config.setdefault("provider", {})
        provider["max_retries"] = 1
        # Match create_llm's config/env resolution, but export only the hostname.
        endpoint = provider.get("base_url", os.environ.get("LLM_BASE_URL", ""))
        report["provider"] = urlsplit(str(endpoint)).hostname or ""
        report["model"] = provider.get("model", os.environ.get("LLM_MODEL", ""))
        _require(bool(report["model"]), "A model must be configured; no fallback is selected")
        llm = create_llm(config)
        async with isolated_connectors("shop", "payments", "helpdesk", "chat") as (hub, runs_dir):
            try:
                before = snapshot(ticket)
                evidence["before"] = before
                loop = AgentLoop(
                    llm=llm, hub=hub, policy=PolicyEngine(),
                    max_steps=config.get("agent", {}).get("max_steps", 30), max_llm_calls=max_calls,
                )
                state = await loop.run_ticket(ticket)
                evidence["initial"] = snapshot(ticket)
                if ticket == "2":
                    _require(state.status == "waiting_human", "Ticket 2 must pause for human approval")
                    pending = state.pending_human
                    _require(pending is not None and pending.type == "approval", "Missing pending approval binding")
                    approvals = evidence["initial"]["approvals"]
                    _require(len(approvals) == 1 and approvals[0]["id"] == pending.approval_id
                             and approvals[0]["status"] == "pending" and approvals[0]["amount"] == 18000,
                             "Expected exactly one pending approval for 18000")
                    _require(pending.tool == "payments__create_refund" and pending.args.get("charge_id") == 2
                             and pending.args.get("amount") == 18000, "Approval must bind the expected refund")
                    _require(evidence["initial"]["payments"] == before["payments"], "Financial change before approval")
                    run_id, usage, binding = state.run_id, _usage(state), pending.model_dump()
                    state = await loop.run_ticket(ticket, run_id=run_id)
                    evidence["pending_resume"] = snapshot(ticket)
                    _require(state.run_id == run_id and state.status == "waiting_human"
                             and state.pending_human is not None and state.pending_human.model_dump() == binding,
                             "Pending resume changed the approval binding or run")
                    _require(evidence["pending_resume"] == evidence["initial"] and _usage(state) == usage,
                             "Pending resume duplicated a request/write or consumed model budget")
                    evidence["checks"]["pending_resume_no_duplicates"] = True
                    if simulate_approval:
                        # Deliberately outside AgentLoop; never expose this as an agent tool.
                        result = await hub.call_tool("chat__decide_approval", {
                            "approval_id": pending.approval_id, "decision": "approved",
                            "decided_by": "live_validation_simulated_manager",
                        })
                        _require(isinstance(result, dict) and not result.get("error") and not result.get("isError"),
                                 "Simulated manager decision failed")
                        evidence["manager_decision"] = snapshot(ticket)
                        decision = evidence["manager_decision"]["approvals"]
                        _require(len(decision) == 1 and decision[0]["status"] == "approved"
                                 and decision[0]["decided_by"] == "live_validation_simulated_manager",
                                 "Manager decision was not persisted")
                        state = await loop.run_ticket(ticket, run_id=run_id)
                        _require(state.run_id == run_id and _usage(state)["calls"] >= usage["calls"],
                                 "Approval resume reset the run or call budget")
                        finished = snapshot(ticket)
                        evidence["approved_resume"] = finished
                        _require(state.status == "resolved", "Approved continuation did not resolve")
                        usage, actions = _usage(state), [action.model_dump() for action in state.actions]
                        state = await loop.run_ticket(ticket, run_id=run_id)
                        evidence["finished_resume"] = snapshot(ticket)
                        _require(state.run_id == run_id and state.status == "resolved"
                                 and evidence["finished_resume"] == finished and _usage(state) == usage
                                 and [action.model_dump() for action in state.actions] == actions,
                                 "Finished resume duplicated a write/request or consumed model budget")
                        evidence["checks"]["finished_resume_no_duplicates"] = True

                after = snapshot(ticket)
                evidence["after"] = after
                _require(_actual(state) == expected, "Unexpected runtime outcome")
                _require(_usage(state)["calls"] <= max_calls, "Model call budget exceeded")
                checks = evidence["checks"]
                checks["outcome"] = True
                refund_expected = ticket == "1" or simulate_approval
                if refund_expected:
                    charge_id, amount = (1, 3200) if ticket == "1" else (2, 18000)
                    old = before["payments"]["refunds"]
                    new = [row for row in after["payments"]["refunds"] if row["id"] not in {r["id"] for r in old}]
                    _require(len(new) == 1 and new[0]["charge_id"] == charge_id
                             and new[0]["amount"] == amount and new[0]["status"] == "succeeded",
                             "Expected exactly one fresh persisted refund with the correct amount/charge")
                    _require([r for r in after["payments"]["refunds"] if r["id"] in {r["id"] for r in old}] == old,
                             "Existing refunds changed")
                    actions = [a for a in state.actions if a.connector == "payments" and a.tool == "create_refund"]
                    _require(len(actions) == 1 and actions[0].status == "succeeded"
                             and new[0]["idempotency_key"] == actions[0].key, "Refund action/key not verified")
                    old_charges = before["payments"]["charges"]
                    _require([r for r in after["payments"]["charges"] if r["id"] != charge_id]
                             == [r for r in old_charges if r["id"] != charge_id], "Unrelated charges changed")
                    charge = next(r for r in after["payments"]["charges"] if r["id"] == charge_id)
                    baseline = next(r for r in old_charges if r["id"] == charge_id)
                    _require(charge == {**baseline, "status": "refunded"}, "Charge not fully refunded or other fields changed")
                    checks["fresh_refund"] = True
                else:
                    _require(after["payments"] == before["payments"], "Unexpected financial changes")
                    checks["no_financial_changes"] = True

                public = [r for r in after["replies"] if r["author"] == "agent" and not r["is_internal"]
                          and r["id"] not in {r["id"] for r in before["replies"]}]
                evidence["public_replies"] = public
                if expected == "resolved":
                    _require(bool(public), "Missing fresh public agent reply")
                if ticket == "6":
                    tracking = after["shipment"]["tracking_number"]
                    _require(bool(tracking) and any(tracking in r["body"] for r in public),
                             "Public reply is missing the real tracking number")
                    checks["tracking_number_in_public_reply"] = True
                verifier = Verifier(runs_dir / state.run_id / "live_checks")
                await verifier.verify_actions(state, hub)
                if expected != "wait_human":
                    _require(await verifier.verify_completion(state, hub) == expected, "Read-back completion verification failed")
                _require(bool(verifier.results) and verifier.all_passed, "Missing or failed read-back verifier results")
                evidence["verification"] = [vars(result) for result in verifier.results]
                checks["read_back_verification"] = True
                report.update(checked=True, status="passed")
            finally:
                if state is not None:
                    report.update(actual=_actual(state), usage=_usage(state), runtime_run_id=state.run_id)
                    evidence["state"] = state.model_dump()
                try:
                    evidence["after"] = snapshot(ticket)
                finally:
                    _write_json(destination / "evidence.json", evidence, sanitizer)
                    _copy_artifacts(runs_dir, destination, sanitizer)
    except Exception as exc:  # noqa: BLE001 — preserve evidence for arbitrary runtime/provider failures
        report.update(checked=False, status="failed", error=f"{type(exc).__name__}: {exc}")
    except SystemExit as exc:
        # cli.create_llm reports configuration failures with typer.Exit.
        report.update(checked=False, status="failed", error=f"Configuration exited: {exc.code}")
    finally:
        _write_json(destination / "evidence.json", evidence, sanitizer)
        _write_json(destination / "report.json", report, sanitizer)
    return sanitizer.clean(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticket", required=True, choices=tuple(EXPECTED))
    parser.add_argument("--max-calls", type=int, default=12)
    parser.add_argument("--simulate-approval", action="store_true", help="Ticket 2 only: simulate a manager decision")
    args = parser.parse_args(argv)
    if args.max_calls < 1:
        parser.error("--max-calls must be positive")
    if args.simulate_approval and args.ticket != "2":
        parser.error("--simulate-approval is only valid for ticket 2")
    report = asyncio.run(validate_ticket(args.ticket, max_calls=args.max_calls, simulate_approval=args.simulate_approval))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["checked"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
