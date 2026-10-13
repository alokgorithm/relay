"""Durable approval continuations against isolated systems of record, never live APIs."""
import asyncio
import json
from copy import deepcopy

import pytest
from test_runtime_regressions import MemoryHub, ScriptedLLM, plan, response

import agent.loop as runtime
from agent.action_log import ActionLog
from agent.loop import AgentLoop
from agent.policy import PolicyEngine
from agent.state import RunState, Step

HIGH_REFUND = {"charge_id": 1, "amount": 18000, "reason": "Damaged"}


@pytest.fixture
def paused(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "RUNS_DIR", tmp_path)
    hub = MemoryHub()
    loop = AgentLoop(ScriptedLLM(plan(), response(("payments__create_refund", HIGH_REFUND))),
                     hub, PolicyEngine(), max_steps=3)
    state = asyncio.run(loop.run_ticket("1"))
    assert state.status == "waiting_human"
    assert not hub.refunds
    assert state.pending_human is not None
    assert state.pending_human.args == {**HIGH_REFUND, "idempotency_key": "1:1:create_refund"}
    loop.llm = ScriptedLLM()  # Any unexpected model call fails the test.
    return loop, hub, state, tmp_path / state.run_id


def resume(paused):
    loop, _, state, _ = paused
    return asyncio.run(loop.run_ticket("1", state.run_id))


def approve(paused):
    paused[1].approvals[1]["status"] = "approved"


def refund_calls(hub):
    return [call for call in hub.calls if call[0] == "payments__create_refund"]


def assert_balanced_history(messages):
    pending = set()
    for message in messages:
        if message["role"] == "assistant":
            assert not pending
            pending.update(call["id"] for call in message.get("tool_calls", []))
        elif message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
        else:
            assert not pending
    assert not pending


def test_pending_resume_never_refunds_or_calls_model(paused):
    loop, hub, state, run_dir = paused
    pending = state.pending_human.model_dump()
    for _ in range(2):
        result = resume(paused)
        assert result.status == "waiting_human"
        assert result.pending_human.model_dump() == pending
    assert not refund_calls(hub)
    assert not loop.llm.messages
    assert len(hub.approvals) == 1
    assert RunState.load(run_dir).pending_human == state.pending_human
    assert not any(name == "chat__decide_approval" for name, _, _ in hub.calls)


def test_approved_action_executes_without_model_even_at_exhausted_budget(paused):
    loop, hub, state, run_dir = paused
    approve(paused)
    state.facts["_llm_usage"]["calls"] = 20
    state.save(run_dir)
    loop.max_llm_calls = 20
    result = resume(paused)
    assert not loop.llm.messages
    assert refund_calls(hub) == [("payments__create_refund", state.pending_human.args, "1:1:create_refund")]
    assert len(hub.refunds) == 1
    assert result.pending_human is None
    # A refund is not ticket completion; replay must not override the budget.
    assert result.status == "failed"
    assert result.facts["runtime_error"] == "LLM call budget exhausted without verified completion"
    assert result.facts["_llm_usage"]["calls"] == 20
    persisted = RunState.load(run_dir)
    refund = persisted.has_action_with_key("1:1:create_refund")
    assert refund is not None
    assert refund.status == "succeeded"
    assert refund.args == state.pending_human.args
    assert refund.result == hub.refunds[0]
    observations = [message["content"] for message in persisted.messages if message["role"] == "user"
                    and message["content"].startswith("Runtime observation:")]
    assert len(observations) == 1
    assert "the human approval decision is approved" in observations[0]
    assert '"amount": 18000' in observations[0]
    assert "Runtime replay result: " + json.dumps(hub.refunds[0]) in observations[0]
    assert_balanced_history(persisted.messages)


def test_replay_preserves_remaining_five_calls_without_forcing_success(paused):
    loop, hub, state, run_dir = paused
    approve(paused)
    state.facts["_llm_usage"]["calls"] = 15
    state.save(run_dir)
    loop.max_llm_calls = 20
    loop.max_steps = 10
    loop.llm = ScriptedLLM(*(response(("payments__list_refunds", {"charge_id": 1})) for _ in range(5)))
    result = resume(paused)
    assert len(loop.llm.messages) == 5
    assert result.facts["_llm_usage"]["calls"] == 20
    assert result.status == "failed"
    assert result.facts["runtime_error"] == "LLM call budget exhausted without verified completion"
    assert len(refund_calls(hub)) == len(hub.refunds) == 1
    assert result.pending_human is None
    assert result.get_active_step().id == 1
    assert_balanced_history(result.messages)


def test_model_replans_only_after_observing_original_action_result(paused):
    loop, hub, state, _ = paused
    approve(paused)
    original = deepcopy(state.pending_human.args)

    def replan(messages):
        assert len(refund_calls(hub)) == 1
        assert refund_calls(hub)[0][1] == original
        assert_balanced_history(messages)
        assert any("Runtime replay result:" in message.get("content", "")
                   for message in messages if message["role"] == "user")
        return response(("update_plan", {"steps": [{"goal": "Respond", "done_when": "Verified"}]}))

    loop.max_steps = 1
    loop.llm = ScriptedLLM(replan)
    result = resume(paused)
    assert result.get_active_step().id == 2
    assert result.has_action_with_key("1:1:create_refund").status == "succeeded"
    assert result.status == "failed"  # Replanning alone never forces completion.
    assert len(hub.refunds) == 1


def test_persisted_binding_survives_changed_plan_identity(paused):
    loop, hub, state, run_dir = paused
    approve(paused)
    state.plan = [Step(id=9, goal="Changed plan", done_when="Verified", status="active")]
    state.save(run_dir)
    loop.max_llm_calls = 0
    result = resume(paused)
    assert refund_calls(hub)[0][2] == "1:1:create_refund"
    assert result.has_action_with_key("1:1:create_refund").status == "succeeded"
    assert result.has_action_with_key("1:9:create_refund") is None


def test_legacy_pause_without_explicit_binding_replays_original_step(paused):
    loop, hub, state, run_dir = paused
    approve(paused)
    state.facts.pop("_approval_action_key")
    state.save(run_dir)
    loop.max_llm_calls = 0
    result = resume(paused)
    assert refund_calls(hub)[0][2] == "1:1:create_refund"
    assert result.pending_human is None


@pytest.mark.parametrize("field,value", [("id", 99), ("summary", "Different action"), ("amount", 100)])
def test_changed_approval_record_fails_closed(paused, field, value):
    loop, hub, state, run_dir = paused
    approve(paused)
    pending = state.pending_human.model_dump()
    hub.approvals[1][field] = value
    result = resume(paused)
    assert result.status == "failed"
    assert result.pending_human is not None
    assert result.pending_human.model_dump() == pending
    assert RunState.load(run_dir).pending_human == state.pending_human
    assert not refund_calls(hub)
    assert not loop.llm.messages
    assert len(hub.approvals) == 1


@pytest.mark.parametrize("field,value", [("reason", "Different reason"), ("idempotency_key", "1:9:create_refund")])
def test_changed_saved_arguments_do_not_match_original_full_approval(paused, field, value):
    loop, hub, state, run_dir = paused
    approve(paused)
    state.pending_human.args[field] = value
    state.save(run_dir)
    result = resume(paused)
    assert result.status == "failed"
    assert result.pending_human.args[field] == value
    assert not refund_calls(hub)
    assert not loop.llm.messages


@pytest.mark.parametrize("changed", ["identity", "window", "history", "record", "policy"])
def test_replay_checks_fresh_policy_and_system_of_record(paused, changed):
    loop, hub, state, run_dir = paused
    approve(paused)
    pending = state.pending_human.model_dump()
    if changed == "identity":
        hub.ticket["customer_email"] = "other@example.com"
    elif changed == "window":
        hub.shipment["days_since_delivery"] = 45
    elif changed == "history":
        hub.refunds.append({"id": 10, "amount": 18000, "idempotency_key": "other"})
    elif changed == "record":
        hub.charge["id"] = 999
    else:
        loop.policy.policies["blocked_actions"] = ["payments.create_refund"]
    hub.calls.clear()
    result = resume(paused)
    assert result.status == "failed"
    assert result.pending_human is not None
    assert result.pending_human.model_dump() == pending
    assert RunState.load(run_dir).pending_human == state.pending_human
    assert not refund_calls(hub)
    assert not loop.llm.messages
    assert {name for name, _, _ in hub.calls} >= {
        "helpdesk__get_ticket", "payments__get_charge", "shop__get_order",
        "shop__get_shipment", "payments__list_refunds",
    }
    assert any("Runtime replay result:" in message.get("content", "")
               and "error" in message["content"] for message in result.messages if message["role"] == "user")
    assert_balanced_history(result.messages)


def test_changed_saved_tool_cannot_reuse_original_approval(paused):
    loop, hub, state, run_dir = paused
    approve(paused)
    state.pending_human.tool = "chat__post_message"
    state.save(run_dir)
    result = resume(paused)
    assert result.status == "failed"
    assert result.pending_human.tool == "chat__post_message"
    assert not refund_calls(hub)
    assert not hub.messages
    assert not loop.llm.messages


def test_approval_is_rechecked_even_if_fresh_policy_no_longer_requires_it(paused):
    loop, hub, state, _ = paused
    approve(paused)
    loop.policy.policies["refunds"]["auto_approve_max"] = 20000
    valid = deepcopy(hub.approvals[1])
    hub.overrides["chat__get_approval"] = [valid, {**valid, "summary": "Changed during replay"}]
    result = resume(paused)
    assert result.status == "failed"
    assert result.pending_human == state.pending_human
    assert not refund_calls(hub)
    assert not loop.llm.messages


def test_denied_resume_explains_to_model_without_runtime_write(paused):
    loop, hub, state, _ = paused
    hub.approvals[1]["status"] = "denied"
    loop.policy.policies["refunds"]["auto_approve_max"] = 20000

    def explain(messages):
        assert not refund_calls(hub)
        assert any("the human approval decision is denied" in message.get("content", "")
                   and "payments__create_refund" in message["content"]
                   for message in messages if message["role"] == "user")
        # Even if policy becomes less restrictive, denial still binds the action.
        return response(("payments__create_refund", HIGH_REFUND))

    loop.llm = ScriptedLLM(explain)
    loop.max_steps = 1
    result = resume(paused)
    assert result.pending_human == state.pending_human
    assert not refund_calls(hub)
    assert len(loop.llm.messages) == 1
    assert_balanced_history(result.messages)


def test_retry_reconciles_committed_timeout_without_second_refund(paused):
    loop, hub, state, run_dir = paused
    approve(paused)
    loop.max_llm_calls = 0

    def commit_then_timeout(args):
        hub.refunds.append({"id": 1, **args, "status": "succeeded"})
        raise TimeoutError("Response lost after commit")

    hub.overrides["payments__create_refund"] = [commit_then_timeout]
    first = resume(paused)
    assert first.status == "failed"
    assert first.pending_human == state.pending_human
    assert first.has_action_with_key("1:1:create_refund").status == "unknown"
    assert RunState.load(run_dir).pending_human == state.pending_human
    second = resume(paused)
    assert second.has_action_with_key("1:1:create_refund").status == "succeeded"
    assert second.pending_human is None
    assert second.status == "failed"  # Remaining work still needs model budget.
    third = resume(paused)
    assert third.status == "failed"
    assert len(refund_calls(hub)) == len(hub.refunds) == 1
    assert not loop.llm.messages
    assert_balanced_history(second.messages)


def test_explicit_replay_error_preserves_request_and_failed_action_for_retry(paused):
    loop, hub, state, run_dir = paused
    approve(paused)
    hub.overrides["payments__create_refund"] = [{"error": "Refund rejected before commit"}]
    first = resume(paused)
    assert first.status == "failed"
    assert first.pending_human == state.pending_human
    assert first.has_action_with_key("1:1:create_refund").status == "failed"
    assert RunState.load(run_dir).pending_human == state.pending_human
    assert not hub.refunds
    assert not loop.llm.messages
    loop.max_llm_calls = 0
    second = resume(paused)
    assert second.pending_human is None
    assert second.has_action_with_key("1:1:create_refund").status == "succeeded"
    assert len(hub.refunds) == 1
    assert {call[2] for call in refund_calls(hub)} == {"1:1:create_refund"}


def test_ambiguous_replay_preserves_request_and_log_and_blocks_model(paused):
    loop, hub, state, run_dir = paused
    approve(paused)
    hub.overrides["payments__create_refund"] = [TimeoutError("Unknown outcome")]
    first = resume(paused)
    assert first.has_action_with_key("1:1:create_refund").status == "unknown"
    log_before = (run_dir / "actions.jsonl").read_bytes()
    hub.overrides["payments__list_refunds"] = [RuntimeError("Read-back unavailable")]
    second = resume(paused)
    assert second.status == "failed"
    assert second.pending_human == state.pending_human
    assert second.has_action_with_key("1:1:create_refund").status == "unknown"
    assert (run_dir / "actions.jsonl").read_bytes() == log_before
    assert len(refund_calls(hub)) == 1
    assert not loop.llm.messages


def test_crash_during_replay_recovers_running_snapshot_without_model(paused, monkeypatch):
    loop, hub, state, run_dir = paused
    approve(paused)
    loop.max_llm_calls = 0

    class SimulatedCrash(BaseException):
        pass

    def crash_after_commit(*args, **kwargs):
        raise SimulatedCrash()

    with monkeypatch.context() as patch:
        patch.setattr(ActionLog, "record_success", crash_after_commit)
        with pytest.raises(SimulatedCrash):
            resume(paused)
    interrupted = RunState.load(run_dir)
    assert interrupted.status == "running"
    assert interrupted.pending_human == state.pending_human
    interrupted_refund = interrupted.has_action_with_key("1:1:create_refund")
    assert interrupted_refund is not None
    assert interrupted_refund.status == "started"
    recovered = resume(paused)
    assert recovered.pending_human is None
    assert recovered.has_action_with_key("1:1:create_refund").status == "succeeded"
    assert len(refund_calls(hub)) == len(hub.refunds) == 1
    assert not loop.llm.messages
    assert_balanced_history(recovered.messages)
