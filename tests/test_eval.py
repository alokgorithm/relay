"""Regressions for the offline evaluator; never seed the user's databases."""

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest
import yaml

from agent.llm.base import ToolCall
from connectors.common import db
from eval import run_eval as evaluation

BENCHMARKS = [
    evaluation.evaluate_ticket_1_end_to_end,
    evaluation.evaluate_idempotency_under_fault,
    evaluation.evaluate_high_value_approval,
    evaluation.evaluate_policy_defenses,
    evaluation.evaluate_live_generalization,
]


@pytest.mark.parametrize("benchmark", BENCHMARKS, ids=lambda f: f.__name__)
def test_benchmarks_preserve_existing_data_runs_and_config(benchmark, tmp_path, monkeypatch):
    data_dir = tmp_path / "user-data"
    runs_dir = tmp_path / "user-runs"
    data_dir.mkdir()
    runs_dir.mkdir()
    # A real database with unrelated user content must not be dropped or seeded.
    database = data_dir / "payments.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE user_sentinel (value TEXT)")
        conn.execute("INSERT INTO user_sentinel VALUES ('keep me')")
    state_file = runs_dir / "state.json"
    state_file.write_text('{"keep": true}', encoding="utf-8")
    config_path = evaluation.ROOT_DIR / "connectors.yaml"
    before_db = database.read_bytes()
    before_config = config_path.read_bytes()
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(evaluation.runtime, "RUNS_DIR", runs_dir)
    monkeypatch.setenv("RELAY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CHAOS_SEED", "not-an-integer")

    assert asyncio.run(benchmark()) is True

    assert database.read_bytes() == before_db
    assert set(data_dir.iterdir()) == {database}
    assert set(runs_dir.iterdir()) == {state_file}
    assert state_file.read_text(encoding="utf-8") == '{"keep": true}'
    assert config_path.read_bytes() == before_config
    assert db.DATA_DIR == data_dir
    assert evaluation.runtime.RUNS_DIR == runs_dir
    assert os.environ["RELAY_DATA_DIR"] == str(data_dir)
    assert os.environ["CHAOS_SEED"] == "not-an-integer"


@pytest.mark.parametrize("failure_phase", ["startup", "benchmark"])
def test_sandbox_restores_paths_and_removes_artifacts_on_failure(monkeypatch, failure_phase):
    original_data = db.DATA_DIR
    original_runs = evaluation.runtime.RUNS_DIR
    seen = {}

    class FailingHub:
        def __init__(self, config_path):
            self.config_path = config_path
            seen["config"] = config_path

        def load_config(self):
            config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
            command = config["connectors"]["payments"]["command"]
            assert command[0] == evaluation.sys.executable
            assert Path(command[3]) == db.DATA_DIR
            assert os.environ["RELAY_DATA_DIR"] == str(db.DATA_DIR)
            assert command[4] == "connectors.payments"

        async def start_all(self):
            assert db.DATA_DIR != original_data
            assert evaluation.runtime.RUNS_DIR != original_runs
            assert (db.DATA_DIR / "payments.db").exists()
            if failure_phase == "startup":
                raise RuntimeError("startup failure")

        def list_connectors(self):
            return [{"name": "payments", "connected": True}]

        async def shutdown(self):
            seen["shutdown"] = True

    monkeypatch.setattr(evaluation, "ConnectorHub", FailingHub)
    monkeypatch.setenv("RELAY_DATA_DIR", "user-data-sentinel")

    async def run():
        async with evaluation.isolated_connectors("payments") as (_, runs_dir):
            runs_dir.mkdir()
            (runs_dir / "sentinel.txt").write_text("temporary artifact", encoding="utf-8")
            raise RuntimeError("benchmark failure")

    with pytest.raises(RuntimeError, match=f"{failure_phase} failure"):
        asyncio.run(run())
    assert seen["shutdown"]
    assert not seen["config"].parent.exists()
    assert db.DATA_DIR == original_data
    assert evaluation.runtime.RUNS_DIR == original_runs
    assert os.environ["RELAY_DATA_DIR"] == "user-data-sentinel"


@pytest.mark.parametrize("result", [None, {"error": "write not verified"}, {"isError": True}])
def test_script_rejects_missing_or_failed_observations(result):
    llm = evaluation.DeterministicPolicyMockLLM()
    llm.pending = [ToolCall(id="refund", name="payments__create_refund", arguments={})]
    messages = [] if result is None else [
        {"role": "tool", "tool_call_id": "refund", "content": json.dumps(result)}
    ]
    with pytest.raises(AssertionError, match=r"Missing tool observation|failed"):
        llm.chat(messages)
    assert llm.call_count == 0


def test_script_replans_after_observation_without_fixed_step_ids():
    llm = evaluation.DeterministicPolicyMockLLM()
    response = llm.chat([{"role": "user", "content": "Analyze this support ticket"}])
    assert response.tool_calls[0].arguments["steps"] == llm.STEPS
    observations = [{"role": "tool", "tool_call_id": response.tool_calls[0].id,
                     "content": '{"status": "plan_updated"}'}]
    reads = llm.chat(observations)
    assert [c.name for c in reads.tool_calls] == ["shop__get_order", "shop__get_shipment"]
    observations += [
        {"role": "tool", "tool_call_id": reads.tool_calls[0].id, "content": '{"id": 1042}'},
        {"role": "tool", "tool_call_id": reads.tool_calls[1].id, "content": '{"status": "delivered"}'},
        {"role": "user", "content": "Active step: Step 999: Check order and shipment"},
    ]
    replan = llm.chat(observations)
    assert replan.tool_calls[0].name == "update_plan"
    assert replan.tool_calls[0].arguments["steps"] == llm.STEPS[1:]


def test_main_runs_all_five_and_returns_failure_exit(monkeypatch, capsys):
    called = []

    def benchmark(index):
        async def run():
            called.append(index)
            if index == 0:
                raise AssertionError("deliberate failure")
            return True
        return run

    for index, original in enumerate(BENCHMARKS):
        monkeypatch.setattr(evaluation, original.__name__, benchmark(index))
    with pytest.raises(SystemExit) as error:
        asyncio.run(evaluation.main())
    assert error.value.code == 1
    assert called == list(range(5))
    output = capsys.readouterr().out
    assert "Evaluation Benchmarks Passed: 4/5" in output
    assert "1 scripted runtime + 4 integration" in output
    assert "Not covered: autonomous LLM quality" in output
