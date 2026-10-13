"""Copied entry-point modules must share resources with the agent package."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import agent
import cli
import pytest


@pytest.fixture
def copied_cli(tmp_path):
    package_root = Path(agent.__path__[0]).resolve().parent
    entrypoint_dir = tmp_path / "site-packages"
    entrypoint_dir.mkdir()
    shutil.copy2(cli.__file__, entrypoint_dir / "cli.py")
    (entrypoint_dir / "config.yaml").write_text("wrong_resource_root: true\n", encoding="utf-8")
    allowed = {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT",
        "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME", "LANG",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(
        PYTHONPATH=os.pathsep.join((str(entrypoint_dir), str(package_root))),
        PYTHON_DOTENV_DISABLED="1",
        PYTHONIOENCODING="utf-8",
        RELAY_DATA_DIR=str(tmp_path / "data"),
        LLM_BASE_URL="http://127.0.0.1:9/v1",
        LLM_API_KEY="offline-not-a-secret",
        LLM_MODEL="offline-validation",
    )

    def run(code):
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    return run


def test_copied_cli_uses_shared_config_and_run_roots(copied_cli):
    copied_cli(
        """
from pathlib import Path
import agent
import agent.hub as hub
import agent.loop as runtime
import cli
import viewers.app as viewer

root = Path(agent.__path__[0]).resolve().parent
assert Path(cli.__file__).resolve().parent != root
assert cli.ROOT_DIR == hub.ROOT_DIR == runtime.ROOT_DIR == viewer.ROOT_DIR == root
assert cli.RUNS_DIR == runtime.RUNS_DIR == viewer.RUNS_DIR == root / "runs"
assert hub.ConnectorHub().config_path == root / "connectors.yaml"
config = cli.load_config()
assert "wrong_resource_root" not in config
assert config["provider"]["api_key"] == "offline-not-a-secret"
assert (root / "company/policies.yaml").is_file()
assert (root / "company/playbooks/replacement.md").is_file()
"""
    )


def test_copied_cli_seeds_all_five_systems_in_sandbox(copied_cli):
    copied_cli(
        """
import os
import sqlite3
from contextlib import closing
from pathlib import Path
import cli
from connectors.common import db

sandbox = Path(os.environ["RELAY_DATA_DIR"]).resolve()
assert db.DATA_DIR.resolve() == sandbox
cli.seed()
tables = {
    "shop": "orders",
    "payments": "charges",
    "helpdesk": "tickets",
    "chat": "channels",
    "inventory": "products",
}
for name, table in tables.items():
    path = sandbox / (name + ".db")
    assert path.is_file(), name
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] > 0, name
"""
    )
