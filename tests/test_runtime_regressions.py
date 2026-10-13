"""Runtime regressions using isolated, stateful systems of record (no demo DBs)."""
import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest

import agent.loop as runtime
from agent.action_log import ActionLog
from agent.hub import ConnectorConfig, ConnectorHub, ToolInfo
from agent.llm.base import LLMResponse, ToolCall
from agent.loop import AgentLoop
from agent.policy import PolicyEngine
from agent.state import ActionRecord, RunState, Step
from agent.tracer import Tracer
from agent.verifier import Verifier


def response(*calls, content=None):
    return LLMResponse(content=content, tool_calls=[ToolCall(id=f"call-{i}", name=name, arguments=args) for i, (name, args) in enumerate(calls)])


def plan():
    return response(("update_plan", {"steps": [{"goal": "Resolve the request", "done_when": "Actual outcome verified and customer replied"}]}))


REFUND = {"charge_id": 1, "amount": 3200, "reason": "Damaged"}
REPLY = {"ticket_id": 1, "body": "Your refund has been issued."}


class ScriptedLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.messages = []

    def chat(self, messages, **kwargs):
        self.messages.append(deepcopy(messages))
        assert self.responses, "Unexpected LLM call"
        result = self.responses.pop(0)
        return result(messages) if callable(result) else result


class MemoryHub(ConnectorHub):
    def __init__(self):
        super().__init__()
        self.connectors = {name: ConnectorConfig(name=name, command=[], scopes=["read", "write"]) for name in ("helpdesk", "shop", "payments", "chat", "inventory")}
        self.calls = []
        self.overrides = {}
        self.ticket = {"id": 1, "subject": "Damage", "body": "Order 1042 arrived damaged", "customer_email": "customer@example.com", "status": "open", "tags": "", "replies": []}
        self.charge = {"id": 1, "order_id": 1042, "customer_email": "customer@example.com", "amount": 3200}
        self.order = {"id": 1042, "customer_email": "customer@example.com"}
        self.shipment = {"order_id": 1042, "days_since_delivery": 4}
        self.refunds = []
        self.messages = []
        self.approvals = {}
        self.replacements = []
        for connector, read_names, write_names in (
            ("helpdesk", ["get_ticket"], ["add_reply", "add_internal_note", "set_status", "set_tags"]),
            ("shop", ["get_order", "get_shipment"], []),
            ("payments", ["get_charge", "list_refunds"], ["create_refund"]),
            ("chat", ["get_messages", "get_approval"], ["post_message", "request_approval", "decide_approval"]),
            ("inventory", ["list_replacements_for_order"], ["create_replacement_order"]),
        ):
            for name in read_names + write_names:
                properties = {"idempotency_key": {"type": "string"}} if name == "create_refund" else {}
                self.tools[f"{connector}__{name}"] = ToolInfo(name, name, {"properties": properties}, connector, {"readOnlyHint": name in read_names})

    async def call_tool(self, qualified_name: str, args: dict[str, Any], idempotency_key: str | None = None) -> Any:
        name = qualified_name
        self.calls.append((name, deepcopy(args), idempotency_key))
        if self.overrides.get(name):
            value = self.overrides[name].pop(0)
            if isinstance(value, Exception):
                raise value
            if callable(value):
                value = value(args)
            return deepcopy(value)
        if name == "helpdesk__get_ticket":
            return deepcopy(self.ticket)
        if name == "shop__get_order":
            return deepcopy(self.order)
        if name == "shop__get_shipment":
            return deepcopy(self.shipment)
        if name == "payments__get_charge":
            return deepcopy(self.charge)
        if name == "payments__list_refunds":
            return deepcopy(self.refunds)
        if name == "payments__create_refund":
            key = idempotency_key or args["idempotency_key"]
            existing = next((row for row in self.refunds if row["idempotency_key"] == key), None)
            if existing:
                return deepcopy(existing)
            row = {"id": len(self.refunds) + 1, **args, "idempotency_key": key, "status": "succeeded"}
            self.refunds.append(row)
            return deepcopy(row)
        if name in ("helpdesk__add_reply", "helpdesk__add_internal_note"):
            row = {"id": len(self.ticket["replies"]) + 1, **args, "author": args.get("author", "agent"), "is_internal": name.endswith("add_internal_note")}
            self.ticket["replies"].append(row)
            return deepcopy(row)
        if name in ("helpdesk__set_status", "helpdesk__set_tags"):
            self.ticket.update({key: value for key, value in args.items() if key != "ticket_id"})
            return deepcopy(self.ticket)
        if name == "chat__get_messages":
            return deepcopy(self.messages)
        if name == "chat__post_message":
            row = {"id": len(self.messages) + 1, "author": "relay-agent", **args}
            self.messages.append(row)
            return deepcopy(row)
        if name == "chat__request_approval":
            row = {"id": len(self.approvals) + 1, **args, "status": "pending"}
            self.approvals[row["id"]] = row
            return deepcopy(row)
        if name == "chat__get_approval":
            return deepcopy(self.approvals[args["approval_id"]])
        if name == "inventory__list_replacements_for_order":
            return deepcopy(self.replacements)
        if name == "inventory__create_replacement_order":
            row = {"id": len(self.replacements) + 1, **args, "status": "pending"}
            self.replacements.append(row)
            return deepcopy(row)
        raise AssertionError(f"Unexpected tool: {name}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "RUNS_DIR", tmp_path)
    hub = MemoryHub()
    state = RunState(ticket_id="1", run_id="regression", plan=[Step(id=1, goal="Resolve", done_when="verified", status="active")])
    loop = AgentLoop(ScriptedLLM(), hub, PolicyEngine(), max_steps=5)
    return loop, hub, state, ActionLog(tmp_path / state.run_id), Tracer(tmp_path / state.run_id)


def connector(env, name, args):
    loop, _, state, log, tracer = env
    return asyncio.run(loop._handle_connector_tool(state, name, args, log, tracer))


def test_wal_replays_actions_missing_from_snapshot(tmp_path):
    state = RunState(ticket_id="1")
    state.save(tmp_path)
    log = ActionLog(tmp_path)
    log.record_start(state, "1:1:create_refund", "payments", "create_refund", REFUND)
    log.record_success(state, "1:1:create_refund", {"id": 3})
    loaded = RunState.load(tmp_path)
    assert len(loaded.actions) == 1
    assert loaded.actions[0].status == "succeeded"
    assert loaded.actions[0].result == {"id": 3}


def test_retry_has_one_authoritative_record(tmp_path):
    state = RunState()
    log = ActionLog(tmp_path)
    log.record_start(state, "key", "payments", "create_refund", REFUND)
    log.record_failure(state, "key", "Not applied")
    log.record_start(state, "key", "payments", "create_refund", REFUND)
    log.record_success(state, "key", {"id": 1})
    assert len(state.actions) == 1
    action = state.has_action_with_key("key")
    assert action is not None
    assert action.status == "succeeded"
    with pytest.raises(ValueError, match="different arguments"):
        log.record_start(state, "key", "payments", "create_refund", {**REFUND, "amount": 10})


def test_torn_final_wal_append_can_recover_and_append(tmp_path):
    log = ActionLog(tmp_path)
    state = RunState()
    log.record_start(state, "key", "payments", "create_refund", REFUND)
    with log.log_file.open("a", encoding="utf-8") as stream:
        stream.write('{"key":')
    recovered = RunState()
    log.replay(recovered)
    log.record_success(recovered, "key", {"id": 1})
    log.replay(recovered)
    assert recovered.actions[0].status == "succeeded"
    assert all(json.loads(line) for line in log.log_file.read_text().splitlines())


@pytest.mark.parametrize("suffix", [b'{"key":', b'{"key":"\xe2\x82'])
def test_wal_truncation_preserves_crlf_offsets_and_partial_utf8(tmp_path, suffix):
    log = ActionLog(tmp_path)
    records = [ActionRecord(key=f"key-{i}", connector="payments", tool="create_refund", args=REFUND) for i in range(3)]
    log.log_file.write_bytes(b"".join(record.model_dump_json().encode("utf-8") + b"\r\n" for record in records) + suffix)
    state = RunState()
    log.replay(state)
    log.record_success(state, "key-2", {"id": 1})
    log.replay(state)
    assert len(state.actions) == 3
    assert state.actions[-1].status == "succeeded"


def test_complete_final_wal_record_without_newline_can_append(tmp_path):
    log = ActionLog(tmp_path)
    record = ActionRecord(key="key", connector="payments", tool="create_refund", args=REFUND)
    log.log_file.write_bytes(record.model_dump_json().encode("utf-8"))
    state = RunState()
    log.replay(state)
    log.record_success(state, "key", {"id": 1})
    log.replay(state)
    assert state.actions[0].status == "succeeded"


def test_corrupt_middle_wal_stops_recovery(tmp_path):
    log = ActionLog(tmp_path)
    log.log_file.write_text('{"key":\n{}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        log.replay(RunState())


def test_atomic_snapshot_keeps_previous_state_on_replace_failure(tmp_path, monkeypatch):
    state = RunState(ticket_id="1")
    state.save(tmp_path)
    old = (tmp_path / "state.json").read_text()
    state.status = "failed"
    def fail_replace(*args):
        raise OSError("Interrupted replacement")
    monkeypatch.setattr("agent.state.os.replace", fail_replace)
    with pytest.raises(OSError):
        state.save(tmp_path)
    assert (tmp_path / "state.json").read_text() == old
    assert not list(tmp_path.glob("*.tmp"))


def test_narration_and_exhaustion_do_not_complete_steps(env):
    loop, _, state, log, tracer = env
    loop.max_steps = 2
    loop.llm = ScriptedLLM(LLMResponse(content="I refunded the order"), LLMResponse(content="All done"))
    asyncio.run(loop._execute_plan(state, log, tracer, log.run_dir))
    assert state.status == "failed"
    assert state.plan[0].status == "active"
    assert not state.actions


def test_read_results_are_observed_without_auto_advancing(env):
    loop, _, state, log, tracer = env
    def observe(messages):
        tool_results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        assert {"id": 1042, "customer_email": "customer@example.com"} in tool_results
        assert state.plan[0].status == "active"
        return response(("finish", {"summary": "Observation tested"}))
    loop.llm = ScriptedLLM(response(("shop__get_order", {"order_id": 1042})), observe)
    asyncio.run(loop._execute_plan(state, log, tracer, log.run_dir))
    assert state.status == "verifying"


def test_finish_without_real_execution_does_not_resolve(env):
    loop, _, _, _, _ = env
    loop.llm = ScriptedLLM(plan(), response(("finish", {"summary": "Pretend success"})))
    state = asyncio.run(loop.run_ticket("1"))
    assert state.status == "failed"
    report = json.loads((runtime.RUNS_DIR / state.run_id / "verification.json").read_text())
    assert any(not result["passed"] for result in report)


def test_verified_end_to_end_outcome(env):
    loop, hub, _, _, _ = env
    loop.llm = ScriptedLLM(plan(), response(("payments__create_refund", REFUND)), response(
        ("helpdesk__add_reply", REPLY), ("helpdesk__set_status", {"ticket_id": 1, "status": "resolved"}),
        ("finish", {"summary": "Refund verified and customer replied"})))
    state = asyncio.run(loop.run_ticket("1"))
    assert state.status == "resolved"
    assert len(hub.refunds) == 1
    assert len(hub.ticket["replies"]) == 1
    assert RunState.load(runtime.RUNS_DIR / state.run_id).status == "resolved"
    # Terminal resumes must never execute more tools or call the model.
    before = len(hub.calls)
    assert asyncio.run(loop.run_ticket("1", state.run_id)).status == "resolved"
    assert all(hub.get_tool_info(name).is_read_only for name, _, _ in hub.calls[before:])


def test_runtime_executes_and_verifies_disposable_sqlite_connectors(env, monkeypatch, tmp_path):
    from connectors.common import db
    from connectors.helpdesk import seed as helpdesk_seed
    from connectors.helpdesk import server as helpdesk
    from connectors.payments import seed as payment_seed
    from connectors.payments import server as payments
    from connectors.shop import seed as shop_seed
    from connectors.shop import server as shop

    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "databases")
    for seed in (shop_seed, payment_seed, helpdesk_seed):
        seed.init_db()
        seed.seed_data()
    for module in (shop, payments, helpdesk):
        monkeypatch.setattr(module.chaos, "enabled", False)
    loop, hub, _, _, _ = env
    modules = {"shop": shop, "payments": payments, "helpdesk": helpdesk}
    async def call_real_tool(name, args, idempotency_key=None):
        hub.calls.append((name, deepcopy(args), idempotency_key))
        connector_name, tool_name = hub.parse_tool_name(name)
        return getattr(modules[connector_name], tool_name)(**args)
    monkeypatch.setattr(hub, "call_tool", call_real_tool)
    loop.llm = ScriptedLLM(plan(), response(("payments__create_refund", REFUND)), response(
        ("helpdesk__add_reply", REPLY), ("helpdesk__set_status", {"ticket_id": 1, "status": "resolved"}),
        ("finish", {"summary": "Actual SQLite refund and reply"})))
    state = asyncio.run(loop.run_ticket("1"))
    assert state.status == "resolved"
    refunds = payments.list_refunds(1)
    assert len(refunds) == 1 and refunds[0]["amount"] == 3200
    assert refunds[0]["idempotency_key"] == state.actions[0].key
    ticket = helpdesk.get_ticket(1)
    assert ticket["status"] == "resolved"
    assert any(reply["body"] == REPLY["body"] and not reply["is_internal"] for reply in ticket["replies"])


def test_final_verification_failure_does_not_become_resolved(env):
    loop, hub, _, _, _ = env
    hub.overrides["helpdesk__set_status"] = [{"id": 1, "status": "resolved"}]
    loop.llm = ScriptedLLM(plan(), response(("helpdesk__add_reply", REPLY),
                                           ("helpdesk__set_status", {"ticket_id": 1, "status": "resolved"}),
                                           ("finish", {"summary": "Claims resolved"})))
    state = asyncio.run(loop.run_ticket("1"))
    assert state.status == "failed"
    assert hub.ticket["status"] == "open"


def test_finish_stops_later_calls_in_same_batch(env):
    loop, hub, state, log, tracer = env
    asyncio.run(loop._process_tool_calls(state, response(("finish", {"summary": "Done"}),
                                                        ("payments__create_refund", REFUND)), tracer, log))
    assert state.status == "verifying"
    assert not hub.calls


def test_crash_after_commit_recovers_wal_and_tool_history(env):
    loop, hub, _, _, _ = env
    class SimulatedCrash(BaseException):
        pass
    def crash_after_commit(args):
        hub.refunds.append({"id": 1, **args, "status": "succeeded"})
        raise SimulatedCrash()
    hub.overrides["payments__create_refund"] = [crash_after_commit]
    loop.llm = ScriptedLLM(plan(), response(("payments__create_refund", REFUND), ("helpdesk__add_reply", REPLY)))
    with pytest.raises(SimulatedCrash):
        asyncio.run(loop.run_ticket("1", run_id=None))
    run_dir = next(path for path in runtime.RUNS_DIR.iterdir() if (path / "state.json").exists())
    crashed = RunState.load(run_dir)
    assert crashed.actions[0].status == "started"
    assert not hub.ticket["replies"]
    loop.llm = ScriptedLLM(response(("payments__create_refund", REFUND)), response(
        ("helpdesk__add_reply", REPLY), ("helpdesk__set_status", {"ticket_id": 1, "status": "resolved"}),
        ("finish", {"summary": "Recovered outcome"})))
    resumed = asyncio.run(loop.run_ticket("1", crashed.run_id))
    assert resumed.status == "resolved"
    assert len(hub.refunds) == 1
    assert sum(name == "payments__create_refund" for name, _, _ in hub.calls) == 1
    assert "Interrupted by crash" in json.dumps(loop.llm.messages)


def test_failed_batch_skips_dependent_success_claims(env):
    loop, hub, state, log, tracer = env
    hub.overrides["payments__create_refund"] = [{"error": "Rejected"}]
    result = asyncio.run(loop._process_tool_calls(state, response(
        ("payments__create_refund", REFUND), ("helpdesk__add_reply", REPLY), ("finish", {"summary": "Success"})), tracer, log))
    assert not result
    assert state.status == "running"
    assert state.plan[0].status == "active"
    assert not hub.ticket["replies"]
    assert state.actions[0].status == "failed"
    assert len([message for message in state.messages if message["role"] == "tool"]) == 3


def test_acknowledged_but_missing_refund_blocks_reply(env):
    loop, hub, state, log, tracer = env
    hub.overrides["payments__create_refund"] = [{"id": 1, **REFUND, "status": "succeeded"}]
    asyncio.run(loop._process_tool_calls(state, response(("payments__create_refund", REFUND), ("helpdesk__add_reply", REPLY)), tracer, log))
    assert not hub.ticket["replies"]
    assert state.actions[0].status == "unknown"


@pytest.mark.parametrize("changed", ["identity", "window", "history", "charge"])
def test_refund_safety_uses_fresh_evidence_not_remembered_facts(env, changed):
    _, hub, state, _, _ = env
    state.facts.update(ticket_email="customer@example.com", order_customer_email="customer@example.com", days_since_delivery=0, existing_refund_count=0)
    if changed == "identity":
        hub.ticket["customer_email"] = "attacker@example.com"
    elif changed == "window":
        hub.shipment["days_since_delivery"] = 45
    elif changed == "history":
        hub.refunds.append({"id": 10, "idempotency_key": "old", "amount": 3200})
    else:
        hub.charge["id"] = 999
    assert "error" in connector(env, "payments__create_refund", REFUND)
    assert not any(name == "payments__create_refund" for name, _, _ in hub.calls)


def test_replans_do_not_recycle_action_identity(env):
    loop, _, state, log, _ = env
    log.record_start(state, "1:9:create_refund", "payments", "create_refund", REFUND)
    log.record_failure(state, "1:9:create_refund", "Absent")
    loop._handle_update_plan(state, {"steps": [{"goal": "Retry appropriately", "done_when": "verified"}]})
    assert state.get_active_step().id == 10


def test_conflicting_arguments_do_not_return_cached_success(env):
    assert "error" not in connector(env, "payments__create_refund", REFUND)
    assert "error" in connector(env, "payments__create_refund", {**REFUND, "amount": 100})
    assert len(env[1].refunds) == 1


def test_approval_is_durable_and_pending_resume_does_not_duplicate(env):
    loop, hub, _, _, _ = env
    high = {**REFUND, "amount": 18000}
    loop.llm = ScriptedLLM(plan(), response(("payments__create_refund", high), ("helpdesk__add_reply", REPLY)))
    state = asyncio.run(loop.run_ticket("1"))
    assert state.status == "waiting_human"
    assert not hub.refunds and not hub.ticket["replies"]
    assert state.pending_human.args["amount"] == 18000
    saved_pending = RunState.load(runtime.RUNS_DIR / state.run_id).pending_human
    assert saved_pending is not None
    assert saved_pending.approval_id == 1
    resumed = asyncio.run(loop.run_ticket("1", state.run_id))
    assert resumed.status == "waiting_human"
    assert len(hub.approvals) == 1


@pytest.mark.parametrize("decision", ["approved", "denied"])
def test_approval_resume_reuses_exact_human_decision(env, decision):
    loop, hub, _, _, _ = env
    high = {**REFUND, "amount": 18000}
    loop.llm = ScriptedLLM(plan(), response(("payments__create_refund", high)))
    state = asyncio.run(loop.run_ticket("1"))
    hub.approvals[1]["status"] = decision
    if decision == "approved":
        loop.llm = ScriptedLLM(response(("payments__create_refund", high)), response(
            ("helpdesk__add_reply", REPLY), ("helpdesk__set_status", {"ticket_id": 1, "status": "resolved"}), ("finish", {"summary": "Approved refund"})))
    else:
        loop.llm = ScriptedLLM(response(("payments__create_refund", high)), response(("escalate", {"reason": "Human denied refund"})))
    resumed = asyncio.run(loop.run_ticket("1", state.run_id))
    first_decision_messages = loop.llm.messages[0]
    assert any(message.get("role") == "user"
               and "Runtime observation: the human approval decision is " + decision in message.get("content", "")
               and "payments__create_refund" in message["content"]
               and '"amount": 18000' in message["content"]
               for message in first_decision_messages)
    assert resumed.status == ("resolved" if decision == "approved" else "escalated")
    assert len(hub.refunds) == (1 if decision == "approved" else 0)
    assert len(hub.approvals) == 1


def test_changed_action_cannot_use_existing_approval(env):
    loop, hub, state, _, tracer = env
    bound = {**REFUND, "amount": 18000}
    assert not asyncio.run(loop._request_approval(state, "payments__create_refund", bound, "High value", tracer))
    hub.approvals[1]["status"] = "approved"
    assert not asyncio.run(loop._request_approval(state, "payments__create_refund", {**bound, "amount": 20000}, "High value", tracer))
    assert len(hub.approvals) == 1


def test_agent_cannot_approve_itself_or_impersonate_customer(env):
    assert "error" in connector(env, "chat__decide_approval", {"approval_id": 1, "decision": "approve"})
    assert "error" in connector(env, "helpdesk__add_reply", {**REPLY, "author": "customer"})
    assert "error" in connector(env, "helpdesk__set_status", {"ticket_id": 99, "status": "resolved"})
    assert not env[1].calls


def test_waiting_customer_stays_paused_until_new_reply(env):
    loop, hub, _, _, _ = env
    loop.llm = ScriptedLLM(plan(), response(("helpdesk__add_reply", {"ticket_id": 1, "body": "Please provide your order number."}),
                                           ("helpdesk__set_status", {"ticket_id": 1, "status": "waiting_customer"})))
    state = asyncio.run(loop.run_ticket("1"))
    assert state.status == "waiting_customer"
    assert asyncio.run(loop.run_ticket("1", state.run_id)).status == "waiting_customer"
    hub.ticket["replies"].append({"id": 2, "author": "customer", "body": "Order 1042", "is_internal": False})
    loop.llm = ScriptedLLM(response(("helpdesk__add_reply", {"ticket_id": 1, "body": "Thanks, your issue is resolved."})),
                          response(("helpdesk__set_status", {"ticket_id": 1, "status": "resolved"}), ("finish", {"summary": "Resolved"})))
    # A new reply is a new operation: replan rather than recycling the old reply key.
    loop.llm.responses.insert(0, response(("update_plan", {"steps": [{"goal": "Process customer reply", "done_when": "Resolved"}]})))
    resumed = asyncio.run(loop.run_ticket("1", state.run_id))
    assert resumed.status == "resolved"
    assert "Order 1042" in json.dumps(loop.llm.messages)


def test_fast_customer_reply_is_not_lost_in_pause_snapshot(env):
    loop, hub, _, _, _ = env
    def reply_during_pause(args):
        hub.ticket["status"] = "waiting_customer"
        hub.ticket["replies"].append({"id": 2, "author": "customer", "body": "Order 1042", "is_internal": False})
        return hub.ticket
    hub.overrides["helpdesk__set_status"] = [reply_during_pause]
    loop.llm = ScriptedLLM(plan(), response(("helpdesk__add_reply", {"ticket_id": 1, "body": "Order number?"}),
                                           ("helpdesk__set_status", {"ticket_id": 1, "status": "waiting_customer"})))
    state = asyncio.run(loop.run_ticket("1"))
    assert state.status == "waiting_customer"
    assert not any(reply.get("author") == "customer" for reply in state.facts["ticket"]["replies"])
    loop.llm = ScriptedLLM(response(("update_plan", {"steps": [{"goal": "Handle reply", "done_when": "Resolved"}]})), response(
        ("helpdesk__add_reply", {"ticket_id": 1, "body": "Thanks, resolved."}),
        ("helpdesk__set_status", {"ticket_id": 1, "status": "resolved"}), ("finish", {"summary": "Resolved"})))
    assert asyncio.run(loop.run_ticket("1", state.run_id)).status == "resolved"


def test_unknown_transport_failure_reconciles_before_retry(env):
    _, hub, state, _, _ = env
    def committed_then_failed(args):
        hub.refunds.append({"id": 1, **args, "status": "succeeded"})
        raise RuntimeError("Connection lost after commit")
    hub.overrides["payments__create_refund"] = [committed_then_failed]
    assert "error" in connector(env, "payments__create_refund", REFUND)
    assert state.actions[0].status == "unknown"
    assert "error" not in connector(env, "payments__create_refund", REFUND)
    assert state.actions[0].status == "succeeded"
    assert sum(name == "payments__create_refund" for name, _, _ in hub.calls) == 1


def test_readback_error_does_not_authorize_retry(env):
    _, hub, state, log, _ = env
    key = log.make_key("1", 1, "create_refund")
    args = {**REFUND, "idempotency_key": key}
    log.record_start(state, key, "payments", "create_refund", args)
    hub.overrides["payments__list_refunds"] = [{"error": "Unavailable"}]
    assert "error" in connector(env, "payments__create_refund", REFUND)
    assert state.actions[0].status == "started"
    assert not any(name == "payments__create_refund" for name, _, _ in hub.calls)


def test_unhandled_unknown_write_is_not_assumed_unapplied(env):
    loop, _, state, log, tracer = env
    log.record_start(state, "1:1:custom_write", "new_service", "custom_write", {})
    asyncio.run(loop._reconcile(state, log, tracer))
    assert state.actions[0].status == "started"
    assert "error" in connector(env, "helpdesk__add_reply", REPLY)


def test_refund_verification_checks_amount_charge_and_status(env):
    _, hub, state, log, _ = env
    action = ActionRecord(key="key", connector="payments", tool="create_refund", args={**REFUND, "idempotency_key": "key"}, status="succeeded")
    for bad in ({"amount": 1}, {"charge_id": 999}, {"status": "failed"}):
        hub.refunds = [{**REFUND, "status": "succeeded", "idempotency_key": "key", **bad}]
        result = asyncio.run(Verifier(log.run_dir).verify_action(action, hub, state))
        assert not result.passed


@pytest.mark.parametrize("bad_reply", [
    {"body": "Your refund has been issued. Different ending", "is_internal": False},
    {"body": REPLY["body"], "is_internal": True},
])
def test_reply_verification_rejects_prefix_and_private_note(env, bad_reply):
    _, hub, state, log, _ = env
    action = ActionRecord(key="key", connector="helpdesk", tool="add_reply", args=REPLY, result={"id": 1}, status="succeeded")
    hub.ticket["replies"] = [{"id": 1, "author": "agent", **bad_reply}]
    assert not asyncio.run(Verifier(log.run_dir).verify_action(action, hub, state)).passed


def test_old_identical_reply_does_not_reconcile_new_write(env):
    loop, hub, state, log, tracer = env
    hub.ticket["replies"] = [{"id": 1, "author": "agent", "is_internal": False, **REPLY}]
    log.record_start(state, "key", "helpdesk", "add_reply", REPLY, before={"ids": [1]})
    asyncio.run(loop._reconcile(state, log, tracer))
    assert state.actions[0].status == "failed"


def test_superseded_ticket_updates_verify_current_assignment(env):
    _, hub, state, log, _ = env
    state.actions = [ActionRecord(key=f"key-{i}", connector="helpdesk", tool="set_status", args={"ticket_id": 1, "status": status}, status="succeeded")
                     for i, status in enumerate(("pending", "resolved"))]
    hub.ticket["status"] = "resolved"
    verifier = Verifier(log.run_dir)
    results = asyncio.run(verifier.verify_actions(state, hub))
    assert len(results) == 1 and verifier.all_passed


def test_tags_are_compared_as_exact_tokens(env):
    _, hub, state, log, _ = env
    action = ActionRecord(key="key", connector="helpdesk", tool="set_tags", args={"ticket_id": 1, "tags": "refund"}, status="succeeded")
    hub.ticket["tags"] = "no-refund"
    assert not asyncio.run(Verifier(log.run_dir).verify_action(action, hub, state)).passed


def test_replacement_and_internal_note_are_verified(env):
    _, hub, state, log, _ = env
    assert "error" not in connector(env, "inventory__create_replacement_order", {"original_order_id": 1042, "product_id": 5})
    assert "error" not in connector(env, "helpdesk__add_internal_note", {"ticket_id": 1, "body": "Replacement evidence"})
    verifier = Verifier(log.run_dir)
    results = asyncio.run(verifier.verify_actions(state, hub))
    assert len(results) == 2 and verifier.all_passed
    hub.replacements.clear()
    asyncio.run(verifier.verify_actions(state, hub))
    assert not verifier.all_passed


def test_unknown_succeeded_write_fails_verification(env):
    _, hub, state, log, _ = env
    state.actions = [ActionRecord(key="key", connector="custom", tool="write", args={}, status="succeeded")]
    verifier = Verifier(log.run_dir)
    asyncio.run(verifier.verify_actions(state, hub))
    assert not verifier.all_passed


def test_legacy_false_terminal_label_is_reverified_without_execution(env):
    loop, hub, state, log, _ = env
    state.status = "resolved"
    state.save(log.run_dir)
    resumed = asyncio.run(loop.run_ticket("1", state.run_id))
    assert resumed.status == "failed"
    assert not hub.refunds
    assert all(hub.get_tool_info(name).is_read_only for name, _, _ in hub.calls)


@pytest.mark.parametrize("tool,args", [
    ("helpdesk__set_status", {"ticket_id": 1, "status": "resolved"}),
    ("helpdesk__set_tags", {"ticket_id": 1, "tags": "refund"}),
])
def test_ambiguous_absolute_update_can_retry_after_readback(env, tool, args):
    _, hub, state, _, _ = env
    hub.overrides[tool] = [TimeoutError("Not committed")]
    assert "error" in connector(env, tool, args)
    assert state.actions[0].status == "unknown"
    assert "error" not in connector(env, tool, args)
    assert state.actions[0].status == "succeeded"
    assert sum(name == tool for name, _, _ in hub.calls) == 2


def test_missing_resume_never_creates_new_run(env):
    with pytest.raises(FileNotFoundError):
        asyncio.run(env[0].run_ticket("1", "missing"))
