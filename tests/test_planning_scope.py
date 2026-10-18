"""Planning should not send the entire business tool catalog to the provider."""
import asyncio
from typing import Any

from agent.hub import ConnectorConfig, ConnectorHub
from agent.llm.base import LLMResponse, ToolCall
from agent.loop import AgentLoop
from agent.policy import PolicyEngine
from agent.state import RunState
from agent.tracer import Tracer


def test_understanding_offers_only_memory_and_planning(tmp_path, monkeypatch):
    import agent.loop as runtime
    monkeypatch.setattr(runtime, "RUNS_DIR", tmp_path)

    class Hub(ConnectorHub):
        def __init__(self):
            super().__init__()
            self.connectors["helpdesk"] = ConnectorConfig("helpdesk", [], ["read"])

        async def call_tool(self, qualified_name: str, args: dict[str, Any], idempotency_key: str | None = None) -> Any:
            assert qualified_name == "helpdesk__get_ticket"
            return {"id": 1, "body": "Order 1042 arrived damaged", "customer_email": "test@example.com"}

        def get_all_tools(self):
            raise AssertionError("Full catalog should not be requested during planning")

    class Model:
        def chat(self, messages, tools):
            assert {tool["function"]["name"] for tool in tools} == {"remember", "update_plan"}
            return LLMResponse(tool_calls=[ToolCall(id="plan", name="update_plan", arguments={
                "steps": [{"goal": "Inspect the order", "done_when": "Order evidence read"}],
            })])

    tracer = Tracer(tmp_path)
    state = RunState(ticket_id="1")
    asyncio.run(AgentLoop(Model(), Hub(), PolicyEngine())._understand(state, tracer))
    assert state.plan[0].goal == "Inspect the order"


def test_decision_messages_preserve_all_observations_and_latest_snapshot():
    from copy import deepcopy

    from agent.prompts.templates import ACT_PROMPT

    prefix = ACT_PROMPT.split("{active_step}", 1)[0]
    messages = [
        {"role": "system", "content": "Policies"},
        {"role": "user", "content": "Original ticket"},
        {"role": "user", "content": prefix + "old plan"},
        {"role": "assistant", "tool_calls": [{"id": "read"}]},
        {"role": "tool", "tool_call_id": "read", "content": "Authoritative read result"},
        {"role": "user", "content": "New customer clarification"},
        {"role": "user", "content": prefix + "current plan"},
    ]
    original = deepcopy(messages)
    assert AgentLoop._decision_messages(messages) == messages[:2] + messages[3:]
    assert messages == original
