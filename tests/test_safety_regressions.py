"""Safety regressions; all connector writes use disposable databases."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from agent.hub import ConnectorConfig, ConnectorHub, ToolInfo
from agent.llm.base import LLMResponse
from agent.llm.json_mode import JsonModeLLM
from agent.llm.openai_compat import OpenAICompatLLM
from agent.policy import PolicyEngine
from connectors.chat import seed as chat_seed
from connectors.chat import server as chat
from connectors.common import db
from connectors.inventory import seed as inventory_seed
from connectors.inventory import server as inventory
from connectors.payments import seed as payment_seed
from connectors.payments import server as payments


@pytest.fixture
def databases(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(payments.chaos, "enabled", False)
    payment_seed.init_db()
    payment_seed.seed_data()
    chat_seed.init_db()
    chat_seed.seed_data()
    inventory_seed.init_db()
    inventory_seed.seed_data()
    return tmp_path


@pytest.fixture
def refund_facts():
    return {
        "ticket_email": "ananya@example.com",
        "order_customer_email": "ananya@example.com",
        "days_since_delivery": 4,
        "existing_refund_count": 0,
    }


@pytest.mark.parametrize("field", ["ticket_email", "order_customer_email", "days_since_delivery", "existing_refund_count"])
def test_refund_policy_requires_evidence(refund_facts, field):
    del refund_facts[field]
    assert PolicyEngine().check_action("payments", "create_refund", {"amount": 3200}, refund_facts).action == "deny"


@pytest.mark.parametrize("amount", [None, 0, -1, True, 3.2, "3200", float("nan"), float("inf")])
def test_policy_rejects_invalid_amounts(refund_facts, amount):
    assert PolicyEngine().check_action("payments", "create_refund", {"amount": amount}, refund_facts).action == "deny"


@pytest.mark.parametrize("field,value", [
    ("ticket_email", []), ("order_customer_email", "  "),
    ("days_since_delivery", True), ("days_since_delivery", -1),
    ("days_since_delivery", "4"), ("days_since_delivery", float("nan")),
    ("existing_refund_count", False), ("existing_refund_count", -1),
])
def test_policy_rejects_invalid_evidence(refund_facts, field, value):
    refund_facts[field] = value
    assert PolicyEngine().check_action("payments", "create_refund", {"amount": 3200}, refund_facts).action == "deny"


def test_identity_normalization(refund_facts):
    refund_facts["ticket_email"] = " Ananya@Example.com "
    assert PolicyEngine().check_action("payments", "create_refund", {"amount": 3200}, refund_facts).action == "allow"


def test_agent_cannot_decide_human_approvals():
    assert PolicyEngine().check_action("chat", "decide_approval", {"decision": "approved"}, {}).action == "deny"
    hub = ConnectorHub()
    hub.tools["chat__decide_approval"] = ToolInfo("decide_approval", "", {}, "chat", {"readOnlyHint": False})
    assert hub.get_all_tools() == []
    assert hub.get_tools_for_playbook(["chat"]) == []
    # Keep the documented human CLI route available.
    assert hub.get_tool_info("chat__decide_approval") is not None


def test_refund_key_normalization_and_binding(databases):
    charge = payments.get_charge_by_order(1042)
    first = payments.create_refund(charge["id"], 1000, "damage", " key ")
    assert payments.create_refund(charge["id"], 1000, "new description", "key")["id"] == first["id"]
    with pytest.raises(ValueError, match="different charge or amount"):
        payments.create_refund(charge["id"], 2000, "damage", "key")
    other = payments.get_charge_by_order(1043)
    with pytest.raises(ValueError, match="different charge or amount"):
        payments.create_refund(other["id"], 1000, "damage", "key")
    assert len(payments.list_refunds(charge["id"])) == 1
    assert payments.list_refunds(other["id"]) == []


@pytest.mark.parametrize("amount", [True, 1.5, "1000", 0, -1])
def test_connector_rejects_invalid_amount(databases, amount):
    with pytest.raises(ValueError, match="positive integer"):
        payments.create_refund(1, amount, "damage", "bad-amount")


def test_pending_refunds_reserve_balance(databases):
    conn = db.get_connection("payments")
    with conn:
        conn.execute("INSERT INTO refunds (charge_id, amount, currency, status, reason, idempotency_key) VALUES (1, 3000, 'INR', 'pending', 'damage', 'pending')")
    conn.close()
    with pytest.raises(ValueError, match="remaining refundable amount 200"):
        payments.create_refund(1, 300, "damage", "new")
    payments.create_refund(1, 200, "damage", "new")
    # Reserved pending funds are not yet settled refunds.
    assert payments.get_charge(1)["status"] == "partially_refunded"


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_refunds_are_serialized(databases, same_key):
    def refund(index):
        try:
            return payments.create_refund(1, 2000, "damage", "shared" if same_key else f"key-{index}")
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(refund, range(2)))
    rows = payments.list_refunds(1)
    assert len(rows) == 1
    assert sum(row["amount"] for row in rows) == 2000
    if same_key:
        first, second = results
        assert first is not None
        assert second is not None
        assert first["id"] == second["id"]
    else:
        assert sum(result is None for result in results) == 1


def test_approval_decision_is_immutable_and_idempotent(databases):
    approval = chat.request_approval("approvals", "Refund ticket #1, charge #2", 18000)
    first = chat.decide_approval(approval["id"], "approve", "alice")
    assert chat.decide_approval(approval["id"], "approve", "alice") == first
    with pytest.raises(ValueError, match="already been approved"):
        chat.decide_approval(approval["id"], "deny", "bob")
    messages = chat.get_messages("approvals")
    assert sum("was approved" in message["body"] for message in messages) == 1


def test_concurrent_approval_decisions_cannot_overwrite(databases):
    approval = chat.request_approval("approvals", "Refund", 1000)
    def decide(decision):
        try:
            return chat.decide_approval(approval["id"], decision, decision)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ["approve", "deny"]))
    assert sum(result is None for result in results) == 1
    assert len(chat.get_messages("approvals")) == 2


def test_concurrent_replacements_do_not_oversell(databases):
    conn = db.get_connection("inventory")
    with conn:
        conn.execute("UPDATE products SET stock_quantity = 1 WHERE id = 1")
    conn.close()
    def replace(index):
        try:
            return inventory.create_replacement_order(1042 + index, 1)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(replace, range(2)))
    assert sum(result is None for result in results) == 1
    assert inventory.get_stock(1)["stock_quantity"] == 0


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def result(value, error=False, structured=None):
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(value))], isError=error, structuredContent=structured)


def fake_hub(monkeypatch, outcomes, *, read_only=False, idempotent=False, accepts_key=False, scopes=None):
    hub = ConnectorHub()
    hub.connectors["test"] = ConnectorConfig("test", [], scopes if scopes is not None else ["read", "write"], retry={"max": 2, "on": ["timeout", "unavailable"]})
    schema = {"properties": {"idempotency_key": {"type": "string"}} if accepts_key else {}}
    hub.tools["test__action"] = ToolInfo("action", "", schema, "test", {"readOnlyHint": read_only, "idempotentHint": idempotent})
    session = FakeSession(outcomes)
    monkeypatch.setattr(hub, "_sessions", {"test": session})
    return hub, session


@pytest.mark.parametrize("name", ["test__missing", "missing__action"])
def test_hub_rejects_undiscovered_tools(name, monkeypatch):
    hub, session = fake_hub(monkeypatch, [result({"status": "succeeded"})])
    with pytest.raises(ValueError, match="Unknown"):
        asyncio.run(hub.call_tool(name, {}))
    assert session.calls == []


@pytest.mark.parametrize("read_only,scopes", [(False, ["read"]), (True, ["write"]), (True, []), (False, [])])
def test_hub_enforces_scopes_at_execution(read_only, scopes, monkeypatch):
    hub, session = fake_hub(monkeypatch, [result({})], read_only=read_only, scopes=scopes)
    with pytest.raises(PermissionError):
        asyncio.run(hub.call_tool("test__action", {}))
    assert session.calls == []


def test_tool_names_and_non_boolean_hints_do_not_grant_read_scope():
    assert not ToolInfo("get_and_delete", "", {}, "test").is_read_only
    assert not ToolInfo("get_and_delete", "[READ]", {}, "test", {"readOnlyHint": "false"}).is_read_only
    assert not ToolInfo("create", "", {}, "test").is_idempotent


@pytest.mark.parametrize("error", [TimeoutError("after send"), RuntimeError("503 unavailable")])
def test_non_idempotent_writes_are_not_retried(error, monkeypatch):
    hub, session = fake_hub(monkeypatch, [error, result({})])
    with pytest.raises((TimeoutError, RuntimeError)):
        asyncio.run(hub.call_tool("test__action", {}, "local-log-key"))
    assert len(session.calls) == 1


def test_idempotent_write_needs_remote_key_for_retry(monkeypatch):
    hub, session = fake_hub(monkeypatch, [TimeoutError(), result({})], idempotent=True, accepts_key=True)
    with pytest.raises(TimeoutError):
        asyncio.run(hub.call_tool("test__action", {}))
    assert len(session.calls) == 1


def test_safe_refund_retry_uses_identical_key_and_args(monkeypatch):
    hub, session = fake_hub(monkeypatch, [TimeoutError(), result({"id": 1})], idempotent=True, accepts_key=True)
    args = {"amount": 1000, "idempotency_key": "model-key"}
    assert asyncio.run(hub.call_tool("test__action", args, "host-key")) == {"id": 1}
    assert session.calls[0] == session.calls[1]
    assert session.calls[0][1]["idempotency_key"] == "host-key"
    assert args["idempotency_key"] == "model-key"


def test_read_retry_is_safe(monkeypatch):
    hub, session = fake_hub(monkeypatch, [TimeoutError(), result({"found": True})], read_only=True)
    assert asyncio.run(hub.call_tool("test__action", {})) == {"found": True}
    assert len(session.calls) == 2


def test_mcp_tool_error_is_not_a_success(monkeypatch):
    hub, _ = fake_hub(monkeypatch, [result("invalid charge", error=True)])
    with pytest.raises(RuntimeError, match="invalid charge"):
        asyncio.run(hub.call_tool("test__action", {}))


def test_mcp_in_band_timeout_is_ambiguous(monkeypatch):
    hub, session = fake_hub(monkeypatch, [result("timed out after send", error=True)])
    with pytest.raises(TimeoutError, match="may have succeeded"):
        asyncio.run(hub.call_tool("test__action", {}))
    assert len(session.calls) == 1


def test_hub_preserves_structured_list_results(monkeypatch):
    rows = [{"id": 1}, {"id": 2}]
    hub, _ = fake_hub(monkeypatch, [result(rows[0], structured={"result": rows})], read_only=True)
    assert asyncio.run(hub.call_tool("test__action", {})) == rows


def test_discovery_transmits_real_annotations():
    async def check():
        for server in (payments, chat, inventory):
            tools = {tool.name: tool for tool in await server.mcp.list_tools()}
            for name, annotations in server.TOOL_ANNOTATIONS.items():
                actual = tools[name].annotations
                assert actual is not None
                assert actual.readOnlyHint == annotations.read_only
                assert actual.idempotentHint == annotations.idempotent
        tools = {tool.name: tool for tool in await chat.mcp.list_tools()}
        post_annotations = tools["post_message"].annotations
        approval_annotations = tools["request_approval"].annotations
        assert post_annotations is not None
        assert approval_annotations is not None
        assert not post_annotations.idempotentHint
        assert not approval_annotations.idempotentHint
    asyncio.run(check())


def test_mcp_does_not_coerce_invalid_refund_amount(databases):
    async def check():
        for amount in (True, 1.5, "1000"):
            try:
                response = await payments.mcp.call_tool("create_refund", {"charge_id": 1, "amount": amount, "reason": "damage", "idempotency_key": "bad"})
            except ToolError as error:
                assert "validation" in str(error).lower() or "integer" in str(error).lower()
            else:
                pytest.fail(f"Invalid amount was accepted: {response}")
    asyncio.run(check())
    assert payments.list_refunds(1) == []


TOOLS = [{"type": "function", "function": {"name": "test__action", "parameters": {"type": "object", "properties": {}}}}]


class StubLLM:
    def __init__(self, contents):
        self.contents = iter(contents)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages.copy(), kwargs))
        return LLMResponse(content=next(self.contents))


def test_json_mode_prompt_and_unique_call_ids():
    inner = StubLLM(['{"tool":"test__action","args":{}}'] * 2)
    llm = JsonModeLLM(inner)
    first = llm.chat([], tools=TOOLS)
    second = llm.chat([], tools=TOOLS)
    assert first.tool_calls[0].name == "test__action"
    assert first.tool_calls[0].id != second.tool_calls[0].id
    assert '{"thought"' in inner.calls[0][0][0]["content"]


@pytest.mark.parametrize("bad", ["[]", "null", "1", '{"tool":"unoffered","args":{}}', '{"tool":"test__action","args":[]}'])
def test_json_mode_repairs_invalid_envelopes(bad):
    inner = StubLLM([bad, '{"tool":"test__action","args":{}}'])
    response = JsonModeLLM(inner).chat([], tools=TOOLS)
    assert response.tool_calls[0].arguments == {}
    assert len(inner.calls) == 2


def test_json_mode_forwards_structured_response_request():
    inner = StubLLM(['{"ok":true}'])
    schema = {"type": "object"}
    JsonModeLLM(inner).chat([], json_schema=schema)
    assert inner.calls[0][1]["json_schema"] == schema


def native_response(arguments, finish_reason="tool_calls"):
    tc = SimpleNamespace(id="call_1", function=SimpleNamespace(name="test__action", arguments=arguments))
    message = SimpleNamespace(content=None, tool_calls=[tc])
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)], usage=None)


@pytest.mark.parametrize("arguments", ["invalid", "[]", "null", "1"])
def test_native_mode_rejects_invalid_arguments(arguments):
    llm = OpenAICompatLLM.__new__(OpenAICompatLLM)
    with pytest.raises(ValueError):
        llm._parse_response(native_response(arguments))


def test_native_mode_rejects_incomplete_tool_calls():
    llm = OpenAICompatLLM.__new__(OpenAICompatLLM)
    with pytest.raises(ValueError, match="incomplete"):
        llm._parse_response(native_response('{}', finish_reason="length"))


def test_native_mode_rejects_unoffered_tools(monkeypatch):
    llm = OpenAICompatLLM.__new__(OpenAICompatLLM)
    llm.model = "test"
    llm.tool_mode = "native"
    llm.max_retries = 1
    monkeypatch.setattr(llm, "client", SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: native_response('{}')))), raising=False)
    with pytest.raises(ValueError, match="not offered"):
        llm.chat([], tools=[])


def test_native_mode_does_not_sleep_after_last_failure(monkeypatch):
    import httpx2 as httpx
    from openai import APITimeoutError

    from agent.llm import openai_compat

    def fail(**kwargs):
        raise APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))

    llm = OpenAICompatLLM.__new__(OpenAICompatLLM)
    llm.model = "test"
    llm.tool_mode = "native"
    llm.max_retries = 1
    monkeypatch.setattr(llm, "client", SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail))), raising=False)
    sleeps = []
    monkeypatch.setattr(openai_compat.time, "sleep", sleeps.append)
    with pytest.raises(RuntimeError, match="after 1 attempts"):
        llm.chat([])
    assert sleeps == []


def test_mcp_in_band_unavailable_is_retried_only_when_safe(monkeypatch):
    async def no_sleep(seconds):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    hub, session = fake_hub(monkeypatch, [result("503 unavailable", error=True), result({"id": 1})], idempotent=True, accepts_key=True)
    assert asyncio.run(hub.call_tool("test__action", {}, "host-key")) == {"id": 1}
    assert session.calls[0] == session.calls[1]


def test_retry_max_zero_still_executes_once(monkeypatch):
    hub, session = fake_hub(monkeypatch, [result({"id": 1})])
    hub.connectors["test"].retry["max"] = 0
    assert asyncio.run(hub.call_tool("test__action", {})) == {"id": 1}
    assert len(session.calls) == 1


def test_fully_refunded_charge_is_not_refundable(databases):
    with pytest.raises(ValueError, match="not refundable"):
        payments.create_refund(3, 1, "damage", "new-refund")
