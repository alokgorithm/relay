"""Configuration and setup regression checks without calling a provider."""
import pytest
import typer
from typer.testing import CliRunner

import cli


def test_unset_provider_variables_fail_before_client_creation(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "provider:\n  base_url: ${LLM_BASE_URL}\n  api_key: ${LLM_API_KEY}\n  model: ${LLM_MODEL}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "ROOT_DIR", tmp_path)
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(typer.Exit):
        cli.create_llm(cli.load_config())


def test_tool_mode_environment_override(monkeypatch):
    import agent.llm.openai_compat as adapter
    from agent.llm.json_mode import JsonModeLLM

    received = {}

    def fake_client(**kwargs):
        received.update(kwargs)
        return object()

    monkeypatch.setattr(adapter, "OpenAICompatLLM", fake_client)
    monkeypatch.setenv("TOOL_MODE", "json")
    result = cli.create_llm({"provider": {
        "base_url": "http://localhost:11434/v1", "api_key": "local", "model": "test", "tool_mode": "native",
    }})
    assert isinstance(result, JsonModeLLM)
    assert received["tool_mode"] == "json"


def test_seed_reports_failure_with_nonzero_exit(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="seed failed"))
    result = CliRunner().invoke(cli.app, ["seed"])
    assert result.exit_code == 1
    assert "Done!" not in result.stdout
