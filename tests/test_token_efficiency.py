"""Offline context byte measurements, not tokenizer or provider billing estimates."""
import asyncio
import json
from copy import deepcopy

import pytest
from test_runtime_regressions import MemoryHub, ScriptedLLM, plan, response

import agent.loop as runtime
from agent.action_log import ActionLog
from agent.llm.base import LLMResponse
from agent.loop import CONTROL_TOOLS, AgentLoop
from agent.policy import PolicyEngine
from agent.prompts.templates import ACT_PROMPT, UNDERSTAND_PROMPT
from agent.state import ActionRecord, RunState, Step
from agent.tracer import Tracer


def serialized_bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "RUNS_DIR", tmp_path)
    hub = MemoryHub()
    state = RunState(ticket_id="1", run_id="efficiency", plan=[
        Step(id=1, goal="Inspect order", done_when="Order read", status="active"),
    ])
    loop = AgentLoop(ScriptedLLM(), hub, PolicyEngine(), max_steps=5)
    directory = tmp_path / state.run_id
    return loop, hub, state, ActionLog(directory), Tracer(directory)


def execute(env):
    loop, _, state, log, tracer = env
    asyncio.run(loop._execute_plan(state, log, tracer, log.run_dir))


def names(tools):
    return {tool["function"]["name"] for tool in tools}


def test_planning_catalog_and_serialized_context_savings(env):
    loop, hub, state, _, tracer = env
    loop.llm = ScriptedLLM(response(("remember", {"key": "playbook", "value": "order_status"})), plan())
    asyncio.run(loop._understand(state, tracer))
    books = loop._load_playbooks()
    prompt = loop.llm.messages[0][1]["content"]
    assert all(slug in prompt and book["description"] in prompt for slug, book in books.items())
    assert all(book["content"] not in prompt for book in books.values())
    assert "key='playbook'" in prompt and "exact" in prompt
    baseline = deepcopy(loop.llm.messages[0])
    baseline[1]["content"] = UNDERSTAND_PROMPT.format(
        ticket_id=state.ticket_id, subject=hub.ticket["subject"], body=hub.ticket["body"],
        customer_email=hub.ticket["customer_email"], status=hub.ticket["status"],
        playbooks_list="\n\n".join(f"### {slug}\n{pb['content']}" for slug, pb in books.items()),
    )
    old_bytes = serialized_bytes(baseline)
    new_bytes = serialized_bytes(loop.llm.messages[0])
    assert new_bytes < old_bytes
    print(f"Planning context bytes: baseline={old_bytes}, optimized={new_bytes}")

    assert state.playbook == "order_status"


def test_execution_context_and_tool_bytes_preserve_exact_history(env):
    loop, _, state, log, tracer = env
    loop.llm = ScriptedLLM(plan())
    asyncio.run(loop._understand(state, tracer))
    books = loop._load_playbooks()
    state.playbook = "order_status"
    payload = {"id": 12, "details": "Exact evidence " * 1000}
    state.actions.append(ActionRecord(key="1:1:example", connector="helpdesk", tool="example",
                                     args={"body": "Original argument " * 500}, status="succeeded", result=payload))
    pair = [
        {"role": "assistant", "tool_calls": [{"id": "read", "type": "function", "function": {
            "name": "shop__get_order", "arguments": '{"order_id":1042}'}}]},
        {"role": "tool", "tool_call_id": "read", "content": json.dumps(payload)},
    ]
    state.messages.extend(deepcopy(pair))
    state.facts["_llm_usage"]["prompt_tokens"] = 447000
    state.facts["_internal"] = "Not model evidence " * 1000
    loop.llm = ScriptedLLM(response(("finish", {"summary": "Trigger verifier, not claim completion"})))
    original_history = deepcopy(state.messages)
    original_facts = deepcopy(state.facts)
    execute(env)
    optimized = loop.llm.messages[0]
    assert state.messages[:len(original_history)] == original_history
    assert all(message in optimized for message in pair)
    assert f"order_status: {books['order_status']['description']}" in optimized[-1]["content"]
    assert books["order_status"]["content"] not in optimized[-1]["content"]
    assert books["replacement"]["content"] not in optimized[-1]["content"]
    assert '"_llm_usage"' not in optimized[-1]["content"]
    assert '"_internal"' not in optimized[-1]["content"]
    assert "1:1:example helpdesk.example: succeeded" in optimized[-1]["content"]
    assert payload["details"] not in optimized[-1]["content"]
    assert state.facts["_internal"] == original_facts["_internal"]
    baseline = deepcopy(optimized)
    baseline[-1]["content"] = ACT_PROMPT.format(
        plan_display="Inspect order", facts=json.dumps(original_facts, indent=2),
        recent_actions="\n".join(f"{a.connector}.{a.tool}({a.args}) → {a.status}: {json.dumps(a.result)}" for a in state.actions),
        active_step="Inspect order", ticket_id="1", step_id=1, tool_name="<tool>",
        workflow=books["order_status"]["content"],
    )
    _, tools = loop._execution_context(state, books)
    all_tools = loop.hub.get_all_tools() + CONTROL_TOOLS
    context_baseline, context_optimized = serialized_bytes(baseline), serialized_bytes(optimized)
    tools_baseline, tools_optimized = serialized_bytes(all_tools), serialized_bytes(tools)
    print(f"Execution context bytes: baseline={context_baseline}, optimized={context_optimized}")
    print(f"Tool schema bytes: baseline={tools_baseline}, optimized={tools_optimized}")
    assert context_optimized < context_baseline
    assert tools_optimized < tools_baseline
    persisted = RunState.load(log.run_dir)
    assert all(message in persisted.messages for message in pair)
    assert persisted.actions[0].result == payload


def test_decision_context_compacts_old_complete_tool_batches(env):
    loop, _, state, _, _ = env
    state.messages = [{"role": "system", "content": "Stable policy"}]
    prefix = ACT_PROMPT.split("{active_step}", 1)[0]
    for index in range(5):
        state.messages.extend([
            {"role": "user", "content": prefix + f"step {index}"},
            {"role": "assistant", "tool_calls": [{"id": str(index), "function": {"name": "read"}}]},
            {"role": "tool", "tool_call_id": str(index), "content": json.dumps({"large": "evidence " * 300})},
        ])
    before = deepcopy(state.messages)
    compact = loop._decision_messages(state)
    assert state.messages == before
    assert compact[0] == before[0]
    assert "Omitted complete tool batches: 3" in compact[1]["content"]
    assert {message.get("tool_call_id") for message in compact if message["role"] == "tool"} == {"3", "4"}
    assert serialized_bytes(compact) < serialized_bytes(before) / 2


@pytest.mark.parametrize("slug,connectors", [
    ("order_status", {"shop", "helpdesk"}),
    ("damaged_item_refund", {"shop", "payments", "helpdesk", "chat"}),
    ("replacement", {"shop", "inventory", "helpdesk", "chat"}),
])
def test_selected_workflow_tools_use_hub_filter(env, monkeypatch, slug, connectors):
    loop, hub, state, _, _ = env
    books = loop._load_playbooks()
    state.playbook = slug
    selected = []
    original = hub.get_tools_for_playbook

    def filtered(required):
        selected.append(set(required))
        return original(required)

    monkeypatch.setattr(hub, "get_tools_for_playbook", filtered)
    workflow, tools = loop._execution_context(state, books)
    assert selected == [connectors]
    assert workflow.startswith(f"{slug}: {books[slug]['description']}")
    assert books[slug]["content"] not in workflow
    assert {name.split("__")[0] for name in names(tools) if "__" in name} == connectors
    assert names(CONTROL_TOOLS) <= names(tools)
    assert not {"chat__request_approval", "chat__decide_approval"} & names(tools)
    assert hub.get_tool_info("chat__request_approval") is not None


@pytest.mark.parametrize("slug", [None, "unknown", "Order Status Inquiry"])
def test_no_exact_selection_falls_back_to_catalog_all_business_tools(env, slug):
    loop, hub, state, _, _ = env
    state.playbook = slug
    books = loop._load_playbooks()
    workflow, tools = loop._execution_context(state, books)
    assert loop._playbook_catalog(books) in workflow
    assert all(book["content"] not in workflow for book in books.values())
    assert names(tools) == (names(hub.get_all_tools()) - {"chat__request_approval", "chat__decide_approval"}) | names(CONTROL_TOOLS)


@pytest.mark.parametrize("declarations", ["", "- `missing`: Unknown", "- `shop`: Order\n- `payments`", "- other syntax"])
def test_malformed_or_unconfigured_connectors_fall_back_safely(env, declarations):
    loop, hub, state, _, _ = env
    state.playbook = "custom"
    book = {"custom": {"description": "Custom", "content": f"## Connectors Needed\n{declarations}\n\n## Steps\nInspect"}}
    _, tools = loop._execution_context(state, book)
    assert "inventory__create_replacement_order" in names(tools)
    assert names(hub.get_all_tools()) - {"chat__request_approval"} <= names(tools)


def test_workflow_switch_and_replan_refresh_next_decision(env):
    loop, _, state, _, _ = env
    state.playbook = "replacement"
    seen = []

    class Model:
        def chat(self, messages, tools):
            seen.append((deepcopy(messages), names(tools)))
            if len(seen) == 1:
                return response(("remember", {"key": "playbook", "value": "damaged_item_refund"}),
                                ("update_plan", {"steps": [{"goal": "Inspect refund", "done_when": "Eligibility known"}]}))
            return response(("finish", {"summary": "Verify"}))

    loop.llm = Model()
    execute(env)
    books = loop._load_playbooks()
    assert "inventory__create_replacement_order" in seen[0][1]
    assert "payments__create_refund" not in seen[0][1]
    assert "payments__create_refund" in seen[1][1]
    assert "inventory__create_replacement_order" not in seen[1][1]
    assert f"damaged_item_refund: {books['damaged_item_refund']['description']}" in seen[1][0][-1]["content"]
    assert books["damaged_item_refund"]["content"] not in seen[1][0][-1]["content"]
    assert books["replacement"]["content"] not in seen[1][0][-1]["content"]
    assert "Inspect refund" in seen[1][0][-1]["content"]
    assert any(books["replacement"]["description"] in (message.get("content") or "") for message in state.messages)


def test_compact_facts_keep_changed_ticket_and_all_noninternal_values(env):
    loop, _, state, _, _ = env
    state.facts = {"ticket_subject": "Original", "ticket_body": "Original body", "ticket_email": "person@example.com",
                   "ticket": {"id": 1, "subject": "Original", "body": "Original body", "customer_email": "person@example.com",
                              "status": "open", "replies": [{"id": 9, "body": "New clarification"}]},
                   "order": {"details": "Unabridged " * 1000}, "_llm_usage": {"calls": 3}}
    before = deepcopy(state.facts)
    snapshot = loop._snapshot_facts(state)
    assert snapshot["ticket"] == {"id": 1, "status": "open", "replies": before["ticket"]["replies"]}
    assert snapshot["order"]["details"].endswith("…[truncated]")
    assert len(snapshot["order"]["details"]) < len(before["order"]["details"])
    assert snapshot["ticket_email"] == before["ticket_email"]
    assert state.facts == before
    state.facts["ticket"]["body"] = "Updated body"
    assert loop._snapshot_facts(state)["ticket"]["body"] == "Updated body"


def test_returned_usage_counts_planning_repair_execution_and_trace(env):
    loop, _, state, log, tracer = env
    state.plan = []
    first = LLMResponse(content="Analyzing", usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})
    second = plan()
    second.usage = {"prompt_tokens": 200, "completion_tokens": 30, "total_tokens": 230}
    third = response(("finish", {"summary": "Verify"}))
    third.usage = {"prompt_tokens": 300, "completion_tokens": 40, "total_tokens": 340}
    loop.llm = ScriptedLLM(first, second, third)
    asyncio.run(loop._understand(state, tracer))
    execute(env)
    assert state.facts["_llm_usage"] == {"calls": 3, "prompt_tokens": 600, "completion_tokens": 90, "total_tokens": 690}
    assert RunState.load(log.run_dir).facts["_llm_usage"] == state.facts["_llm_usage"]
    events = [json.loads(line) for line in tracer.trace_file.read_text().splitlines()]
    usage_events = [event["data"] for event in events if event["type"] == "llm_usage"]
    assert [event["phase"] for event in usage_events] == ["planning", "planning_repair", "execution"]
    assert usage_events[-1]["cumulative"] == state.facts["_llm_usage"]
    assert len([event for event in events if event["type"] == "llm_call"]) == 3


def test_missing_usage_and_adapter_failure_do_not_invent_tokens_or_billing(env):
    loop, _, state, _, tracer = env
    loop.llm = ScriptedLLM(LLMResponse(content="No usage returned"))
    loop._call_llm(state, tracer, [], [], "planning")
    assert state.facts["_llm_usage"] == {"calls": 1}

    class FailingModel:
        def chat(self, messages, tools):
            raise RuntimeError("Adapter failed after unknown retries")

    loop.llm = FailingModel()
    with pytest.raises(RuntimeError, match="Adapter failed"):
        loop._call_llm(state, tracer, [], [], "execution")
    assert state.facts["_llm_usage"] == {"calls": 1}
    assert loop._handle_remember(state, {"key": "_llm_usage", "value": "{}"}).get("error")


@pytest.mark.parametrize("budget,expected_calls", [(0, 0), (1, 1), (2, 2)])
def test_budget_checked_before_planning_repair_and_execution(env, budget, expected_calls):
    loop, _, state, _, tracer = env
    state.plan = []
    loop.max_llm_calls = budget
    loop.llm = ScriptedLLM(LLMResponse(content="No plan"), plan())
    asyncio.run(loop._understand(state, tracer))
    if state.status == "running":
        execute(env)
    assert len(loop.llm.messages) == expected_calls
    assert state.facts["_llm_usage"]["calls"] == expected_calls
    assert state.status == "failed"
    assert "LLM call budget exhausted" in state.facts["runtime_error"]


def test_budget_survives_run_ticket_resume_without_dispatch(env):
    loop, _, state, log, _ = env
    state.messages = [{"role": "system", "content": "Policies"}]
    state.facts["_llm_usage"] = {"calls": 2, "total_tokens": 123}
    state.save(log.run_dir)
    loop.max_llm_calls = 2
    first = asyncio.run(loop.run_ticket("1", state.run_id))
    second = asyncio.run(loop.run_ticket("1", state.run_id))
    assert first.status == second.status == "failed"
    assert loop.llm.messages == []
    assert second.facts["_llm_usage"] == {"calls": 2, "total_tokens": 123}


@pytest.mark.parametrize("invalid", [-1, True, 1.5, "2"])
def test_constructor_rejects_invalid_budget(env, invalid):
    loop, hub, _, _, _ = env
    with pytest.raises(ValueError, match="max_llm_calls"):
        AgentLoop(loop.llm, hub, loop.policy, max_llm_calls=invalid)
    assert AgentLoop(loop.llm, hub, loop.policy).max_llm_calls is None


def test_identical_error_batches_stop_without_losing_pairs(env):
    loop, _, state, _, _ = env
    loop.max_steps = 10
    loop.llm = ScriptedLLM(*(response(("nonexistent_tool", {"id": 1})) for _ in range(3)))
    execute(env)
    assert len(loop.llm.messages) == 3
    assert state.status == "failed"
    assert "Repeated identical tool errors" in state.facts["runtime_error"]
    assert len([message for message in state.messages if message["role"] == "tool"]) == 3
    assert len([message for message in state.messages if message["role"] == "assistant"]) == 3


def test_identical_successful_reads_are_not_stalls(env):
    loop, _, state, _, _ = env
    loop.llm = ScriptedLLM(*(response(("shop__get_order", {"order_id": 1042})) for _ in range(3)),
                           response(("finish", {"summary": "Verify"})))
    execute(env)
    assert len(loop.llm.messages) == 4
    assert state.status == "verifying"


def test_selected_refund_still_requests_runtime_approval_and_fresh_reads(env):
    loop, hub, state, _, _ = env
    state.playbook = "damaged_item_refund"
    hub.charge["amount"] = 18000
    loop.llm = ScriptedLLM(response(("payments__create_refund", {"charge_id": 1, "amount": 18000, "reason": "Damaged"})))
    execute(env)
    assert state.status == "waiting_human"
    assert state.pending_human.args["amount"] == 18000
    called = {name for name, _, _ in hub.calls}
    assert {"helpdesk__get_ticket", "payments__get_charge", "shop__get_order", "shop__get_shipment",
            "payments__list_refunds", "chat__request_approval"} <= called
    assert "payments__create_refund" not in called
    assert len(hub.approvals) == 1
    assert hub.refunds == []
    assert any(message["role"] == "tool" and "Approval denied or pending" in message["content"] for message in state.messages)
