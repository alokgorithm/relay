"""Unit tests for ActionLog, RunState persistence, and ConnectorHub."""

import asyncio
from pathlib import Path
import pytest
import shutil

from agent.state import RunState, Step, ActionRecord
from agent.action_log import ActionLog
from agent.hub import ConnectorHub

TMP_RUN_DIR = Path(__file__).resolve().parent.parent / "runs" / "test_run_unit"


def setup_function():
    if TMP_RUN_DIR.exists():
        shutil.rmtree(TMP_RUN_DIR)
    TMP_RUN_DIR.mkdir(parents=True, exist_ok=True)


def teardown_function():
    if TMP_RUN_DIR.exists():
        shutil.rmtree(TMP_RUN_DIR)


def test_run_state_lifecycle():
    state = RunState(ticket_id="101", run_id="test_run_unit")
    state.plan = [
        Step(id=1, goal="Check order", done_when="order found", status="active"),
        Step(id=2, goal="Issue refund", done_when="refund created", status="pending"),
    ]
    state.facts = {"order_id": 1042, "amount": 3200}

    # Save and reload
    state.save(TMP_RUN_DIR)
    loaded = RunState.load(TMP_RUN_DIR)
    assert loaded.ticket_id == "101"
    assert len(loaded.plan) == 2
    assert loaded.get_active_step().id == 1

    # Advance step
    next_step = state.advance_step()
    assert next_step.id == 2
    assert state.plan[0].status == "done"
    assert state.plan[1].status == "active"


def test_action_log_write_ahead_and_reconciliation():
    log = ActionLog(TMP_RUN_DIR)
    state = RunState(ticket_id="101", run_id="test_run_unit")

    key = log.make_key("101", 1, "create_refund")
    assert key == "101:1:create_refund"

    # Write-ahead start record
    rec = log.record_start(state, key, "payments", "create_refund", {"charge_id": 1, "amount": 3200})
    assert rec.status == "started"
    assert len(log.get_actions_needing_reconciliation(state)) == 1

    # Record timeout/unknown
    log.record_unknown(state, key)
    assert state.actions[0].status == "unknown"
    assert len(log.get_actions_needing_reconciliation(state)) == 1

    # Record success
    log.record_success(state, key, {"refund_id": 42})
    assert state.actions[0].status == "succeeded"
    assert len(log.get_actions_needing_reconciliation(state)) == 0


def test_connector_hub_discovery_and_filtering():
    async def _test():
        hub = ConnectorHub()
        hub.load_config()
        await hub.start_all()
        try:
            assert len(hub.connectors) >= 4
            assert len(hub.tools) >= 20

            # Check scope enforcement: shop should only have read-only tools
            shop_tools = [t for t in hub.tools.values() if t.connector_name == "shop"]
            assert all(t.is_read_only for t in shop_tools)

            # Check tool filtering for playbook
            filtered = hub.get_tools_for_playbook(["shop", "helpdesk"])
            filtered_names = [f["function"]["name"] for f in filtered]
            assert any(n.startswith("shop__") for n in filtered_names)
            assert any(n.startswith("helpdesk__") for n in filtered_names)
            assert not any(n.startswith("payments__") for n in filtered_names)
        finally:
            await hub.shutdown()

    asyncio.run(_test())
