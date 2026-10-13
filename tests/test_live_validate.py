"""No provider/network calls: fake hub/runtime, real temporary seeded SQLite."""

import asyncio
import importlib
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.state import ActionRecord, HumanRequest, RunState
from connectors.common import db
from eval import live_validate as live
from eval import run_eval as evaluation


class FakeHub:
    def __init__(self, config_path):
        self.config_path = config_path
        self.calls = []
        self.closed = False

    def load_config(self):
        pass

    async def start_all(self):
        pass

    def list_connectors(self):
        return [{"connected": True}]

    async def shutdown(self):
        self.closed = True

    @staticmethod
    def write(connector, sql, args=()):
        with closing(sqlite3.connect(db.DATA_DIR / f"{connector}.db")) as connection, connection:
            cursor = connection.execute(sql, args)
            return cursor.lastrowid

    async def call_tool(self, name, args):
        self.calls.append((name, deepcopy(args)))
        if name == "chat__decide_approval":
            self.write("chat", "UPDATE approvals SET status=?, decided_by=? WHERE id=?",
                       (args["decision"], args["decided_by"], args["approval_id"]))
            return {"status": "approved"}
        if name == "chat__get_approval":
            return next(r for r in live._rows("chat", "approvals") if r["id"] == args["approval_id"])
        if name == "payments__list_refunds":
            return [r for r in live._rows("payments", "refunds") if r["charge_id"] == args["charge_id"]]
        if name == "helpdesk__get_ticket":
            ticket = next(r for r in live._rows("helpdesk", "tickets") if r["id"] == args["ticket_id"])
            ticket["replies"] = [r for r in live._rows("helpdesk", "ticket_replies") if r["ticket_id"] == args["ticket_id"]]
            return ticket
        raise AssertionError(f"Unexpected tool: {name}")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    config = {"provider": {
        "base_url": "https://user:password@fictional.provider/v1?key=query-secret",
        "api_key": "configured-secret", "model": "configured-free-model", "max_retries": 5,
    }, "agent": {"max_steps": 30}}
    create = Mock(return_value=SimpleNamespace(chat=Mock()))
    load = Mock(return_value=config)
    monkeypatch.setattr(live, "load_config", load)
    monkeypatch.setattr(live, "create_llm", create)
    monkeypatch.setenv("EXTRA_API_KEY", "environment-secret")
    loops = []
    mode = {"fault": None}

    class FakeLoop:
        def __init__(self, llm, hub, policy, max_steps, max_llm_calls):
            self.llm, self.hub = llm, hub
            self.max_llm_calls = max_llm_calls
            self.resumes = []
            loops.append(self)

        def action(self, connector, tool, args, result):
            key = args.get("idempotency_key", f"{self.state.ticket_id}:1:{tool}")
            self.state.actions.append(ActionRecord(
                key=key, connector=connector, tool=tool, args=args,
                status="succeeded", result=result,
            ))

        def refund(self, ticket):
            charge, amount = (1, 3200) if ticket == "1" else (2, 18000)
            args = {"charge_id": charge, "amount": amount, "reason": "Fictional damaged item",
                    "idempotency_key": f"{ticket}:1:create_refund"}
            refund_id = self.hub.write("payments", "INSERT INTO refunds (charge_id, amount, reason, idempotency_key) VALUES (?, ?, ?, ?)",
                                       (charge, amount, args["reason"], args["idempotency_key"]))
            self.hub.write("payments", "UPDATE charges SET status='refunded' WHERE id=?", (charge,))
            self.action("payments", "create_refund", args, {"id": refund_id})

        def complete(self, ticket):
            status = "escalated" if ticket == "5" else "resolved"
            if ticket in ("1", "2") and mode["fault"] != "narrated_refund":
                self.refund(ticket)
            if mode["fault"] == "financial_change":
                self.hub.write("payments", "UPDATE charges SET amount=1 WHERE id=10")
            if ticket != "5" and mode["fault"] != "missing_reply":
                body = "Fictional refund completed."
                if ticket == "6":
                    body = "Your shipment is in transit. Tracking: BD1047006"
                    if mode["fault"] == "missing_tracking":
                        body = "Your shipment is in transit."
                reply_id = self.hub.write("helpdesk", "INSERT INTO ticket_replies (ticket_id, author, body) VALUES (?, 'agent', ?)",
                                         (int(ticket), body))
                self.action("helpdesk", "add_reply", {"ticket_id": int(ticket), "body": body}, {"id": reply_id})
            self.hub.write("helpdesk", "UPDATE tickets SET status=? WHERE id=?", (status, int(ticket)))
            self.action("helpdesk", "set_status", {"ticket_id": int(ticket), "status": status}, {"id": int(ticket)})
            self.state.status = status
            self.state.pending_human = None

        async def run_ticket(self, ticket, run_id=None):
            self.resumes.append(run_id)
            if run_id is None:
                self.state = RunState(ticket_id=ticket)
                self.state.facts["_llm_usage"] = {"calls": 2, "prompt_tokens": 100, "completion_tokens": 20}
                self.llm.chat()
                if ticket == "2":
                    summary = "Fictional refund needs approval"
                    approval_id = self.hub.write("chat", "INSERT INTO approvals (channel_id, requestor, summary, amount) VALUES (2, 'relay-agent', ?, 18000)",
                                                 (summary,))
                    self.action("chat", "request_approval", {"summary": summary, "amount": 18000}, {"id": approval_id})
                    self.state.status = "waiting_human"
                    self.state.pending_human = HumanRequest(
                        type="approval", approval_id=approval_id, message=summary,
                        tool="payments__create_refund", args={"charge_id": 2, "amount": 18000},
                    )
                else:
                    self.complete(ticket)
            else:
                assert run_id == self.state.run_id
                if self.state.status == "waiting_human":
                    approval = await self.hub.call_tool("chat__get_approval", {"approval_id": self.state.pending_human.approval_id})
                    if approval["status"] == "approved":
                        self.state.facts["_llm_usage"]["calls"] += 3
                        self.llm.chat()
                        self.complete(ticket)
                    elif mode["fault"] == "duplicate_pending":
                        self.hub.write("chat", "INSERT INTO approvals (channel_id, requestor, summary, amount) VALUES (2, 'relay-agent', 'duplicate', 18000)")
                elif mode["fault"] == "duplicate_finished":
                    self.hub.write("helpdesk", "INSERT INTO ticket_replies (ticket_id, author, body) VALUES (2, 'agent', 'duplicate')")
            if mode["fault"] == "wrong_outcome":
                self.state.status = "failed"
            run_dir = evaluation.runtime.RUNS_DIR / self.state.run_id
            self.state.save(run_dir)
            # Provider errors can echo secrets; all copies must be redacted.
            (run_dir / "trace.jsonl").write_text(json.dumps({
                "error": "configured-secret environment-secret query-secret password",
                "api_key": "unknown-secret", "authorization": "Bearer unknown-token",
            }) + "\n", encoding="utf-8")
            if mode["fault"] == "exception":
                raise RuntimeError("provider failed configured-secret")
            return self.state

    monkeypatch.setattr(live, "AgentLoop", FakeLoop)
    monkeypatch.setattr(evaluation, "ConnectorHub", FakeHub)
    return SimpleNamespace(config=config, create=create, load=load, loops=loops, mode=mode, output=tmp_path / "evidence")


def run(harness, ticket, **kwargs):
    report = asyncio.run(live.validate_ticket(ticket, output_dir=harness.output, **kwargs))
    destination = harness.output / report["run_id"]
    evidence = json.loads((destination / "evidence.json").read_text(encoding="utf-8"))
    assert json.loads((destination / "report.json").read_text(encoding="utf-8")) == report
    return report, evidence, destination


@pytest.mark.parametrize("ticket,simulate,expected,calls", [
    ("1", False, "resolved", 2), ("2", False, "wait_human", 2),
    ("2", True, "resolved", 5), ("5", False, "escalated", 2), ("6", False, "resolved", 2),
])
def test_expected_outcomes_fresh_db_and_resume(harness, ticket, simulate, expected, calls):
    original_data, original_runs = db.DATA_DIR, evaluation.runtime.RUNS_DIR
    report, evidence, destination = run(harness, ticket, simulate_approval=simulate)
    assert report["checked"] is True
    assert report["status"] == "passed"
    assert report["actual"] == report["expected"] == expected
    assert report["usage"]["calls"] == calls
    assert report["provider"] == "fictional.provider"
    assert report["model"] == "configured-free-model"
    assert "opt-in" in report["limitation"]
    assert len(harness.loops) == 1
    loop = harness.loops[0]
    assert loop.max_llm_calls == 12
    assert loop.hub.closed
    assert not loop.hub.config_path.exists()
    assert db.DATA_DIR == original_data
    assert evaluation.runtime.RUNS_DIR == original_runs
    harness.create.assert_called_once()
    assert harness.create.call_args.args[0]["provider"]["max_retries"] == 1
    assert harness.config["provider"]["max_retries"] == 5
    assert evidence["verification"] and all(r["passed"] for r in evidence["verification"])
    assert list((destination / "runtime").rglob("state.json"))
    decisions = [call for call in loop.hub.calls if call[0] == "chat__decide_approval"]
    assert len(decisions) == int(simulate)
    if ticket == "2":
        assert evidence["checks"]["pending_resume_no_duplicates"]
        assert loop.resumes == [None] + [report["runtime_run_id"]] * (3 if simulate else 1)
        assert len(evidence["after"]["approvals"]) == 1
        assert loop.llm.chat.call_count == (2 if simulate else 1)
    if ticket in ("5", "6") or (ticket == "2" and not simulate):
        assert evidence["before"]["payments"] == evidence["after"]["payments"]
    else:
        assert len(evidence["after"]["payments"]["refunds"]) == len(evidence["before"]["payments"]["refunds"]) + 1
    if ticket == "6":
        assert evidence["checks"]["tracking_number_in_public_reply"]


@pytest.mark.parametrize("ticket,simulate,fault,error", [
    ("1", False, "narrated_refund", "fresh persisted refund"),
    ("1", False, "missing_reply", "public agent reply"),
    ("6", False, "missing_tracking", "tracking number"),
    ("5", False, "financial_change", "financial changes"),
    ("6", False, "financial_change", "financial changes"),
    ("2", False, "duplicate_pending", "Pending resume duplicated"),
    ("2", True, "duplicate_finished", "Finished resume duplicated"),
    ("1", False, "wrong_outcome", "Unexpected runtime outcome"),
    ("1", False, "exception", "provider failed"),
])
def test_failure_preserves_sanitized_artifacts(harness, ticket, simulate, fault, error):
    harness.mode["fault"] = fault
    report, evidence, destination = run(harness, ticket, simulate_approval=simulate)
    assert not report["checked"]
    assert report["status"] == "failed"
    assert error in report["error"]
    assert evidence["before"] and evidence["after"]
    assert list((destination / "runtime").rglob("state.json"))
    assert harness.loops[0].hub.closed
    for path in destination.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for secret in ("configured-secret", "environment-secret", "query-secret", "password", "unknown-secret", "unknown-token"):
            assert secret not in text
        assert path.suffix != ".db"


def test_budget_is_not_reset_on_approved_resume(harness):
    report, _, _ = run(harness, "2", simulate_approval=True, max_calls=4)
    assert not report["checked"]
    assert "budget exceeded" in report["error"]
    assert harness.loops[0].max_llm_calls == 4
    assert report["usage"]["calls"] == 5


def test_failed_readback_is_rejected(harness, monkeypatch):
    original = FakeHub.call_tool

    async def missing_refund(self, name, args):
        if name == "payments__list_refunds":
            return []
        return await original(self, name, args)

    monkeypatch.setattr(FakeHub, "call_tool", missing_refund)
    report, _, _ = run(harness, "1")
    assert not report["checked"]
    assert "verification failed" in report["error"]


def test_configuration_failure_retains_report_without_provider(harness):
    harness.config["provider"]["model"] = ""
    report, _, destination = run(harness, "1")
    assert not report["checked"]
    assert report["actual"] == "not_started"
    assert (destination / "report.json").exists()
    harness.create.assert_not_called()


@pytest.mark.parametrize("ticket,kwargs", [
    ("3", {}), ("1", {"simulate_approval": True}),
    ("1", {"max_calls": 0}), ("1", {"max_calls": True}),
])
def test_invalid_options_never_create_provider(harness, ticket, kwargs):
    with pytest.raises(ValueError):
        asyncio.run(live.validate_ticket(ticket, output_dir=harness.output, **kwargs))
    harness.load.assert_not_called()
    harness.create.assert_not_called()
    assert not harness.output.exists()


@pytest.mark.parametrize("args", [[], ["--ticket", "3"], ["--ticket", "1", "--simulate-approval"],
                                  ["--ticket", "1", "--max-calls", "0"]])
def test_cli_rejects_invalid_options_without_running(monkeypatch, args):
    runner = Mock()
    monkeypatch.setattr(live, "validate_ticket", runner)
    with pytest.raises(SystemExit) as error:
        live.main(args)
    assert error.value.code == 2
    runner.assert_not_called()


@pytest.mark.parametrize("checked,exit_code", [(True, 0), (False, 1)])
def test_cli_json_and_exit_status(monkeypatch, capsys, checked, exit_code):
    observed = []

    async def fake_validate(ticket, **kwargs):
        observed.append((ticket, kwargs))
        return {"checked": checked, "status": "passed" if checked else "failed"}

    monkeypatch.setattr(live, "validate_ticket", fake_validate)
    assert live.main(["--ticket", "2"]) == exit_code
    assert observed == [("2", {"max_calls": 12, "simulate_approval": False})]
    assert json.loads(capsys.readouterr().out)["checked"] is checked


def test_import_does_not_create_provider(harness):
    import cli

    create = Mock(side_effect=AssertionError("Provider must not be constructed on import"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "create_llm", create)
        importlib.reload(live)
        create.assert_not_called()
    importlib.reload(live)
