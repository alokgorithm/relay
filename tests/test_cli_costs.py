"""CLI cost controls are forwarded without making provider requests."""
import asyncio
import sys
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

import agent.hub
import agent.loop
import cli
from agent.state import RunState


@pytest.mark.parametrize("budget", [7, None])
@pytest.mark.parametrize("shift", [False, True])
def test_cli_forwards_call_budget_and_reports_usage(tmp_path, monkeypatch, capsys, budget, shift):
    received = {}
    capture = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=capture, force_terminal=False, color_system=None, highlight=False, width=200))

    class Hub:
        def __init__(self):
            self.tools = {}
            self.connectors = {}

        def load_config(self):
            return None

        async def start_all(self):
            return None

        async def shutdown(self):
            return None

        async def call_tool(self, name, args):
            assert name == "helpdesk__list_open_tickets"
            return [{"id": 1, "subject": "Test"}]

    def make_loop(**kwargs):
        received.update(kwargs)

        async def run_ticket(*args, **kwargs):
            return RunState(ticket_id="1", status="resolved", facts={
                "_llm_usage": {"calls": 3, "prompt_tokens": 1234, "completion_tokens": 50},
            })

        return SimpleNamespace(run_ticket=run_ticket)

    monkeypatch.setattr(agent.hub, "ConnectorHub", Hub)
    monkeypatch.setattr(agent.loop, "AgentLoop", make_loop)
    monkeypatch.setattr(cli, "create_llm", lambda config: object())
    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path)
    config = {"agent": {"max_llm_calls": budget}}
    if shift:
        asyncio.run(cli._run_shift(config))
    else:
        asyncio.run(cli._run_ticket("1", config))
        output = capture.getvalue()
        assert "3 calls" in output and "1,234 input tokens" in output
    print("RECEIVED IN TEST IS:", received)
    assert received["max_llm_calls"] == budget
