"""Keep every test, including legacy seed/setup hooks, away from demo artifacts."""

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_test_artifacts(tmp_path_factory, request):
    from agent import loop as runtime
    from connectors.common import db
    from connectors.common.chaos import chaos

    root = tmp_path_factory.mktemp("relay-session")
    data_dir = root / "data"
    runs_dir = root / "runs"
    data_dir.mkdir()
    runs_dir.mkdir()

    # Session autouse fixtures run before the legacy module/function setup hooks.
    # Keep the module attribute patchable by individual tests and eval sandboxes.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, "DATA_DIR", data_dir)
        patch.setenv("RELAY_DATA_DIR", str(data_dir))
        patch.setattr(runtime, "RUNS_DIR", runs_dir)
        patch.delenv("CHAOS_SEED", raising=False)
        patch.setattr(chaos, "enabled", False)
        for module in {item.module for item in request.session.items}:
            if module.__name__.rsplit(".", 1)[-1] == "test_action_log_and_hub":
                patch.setattr(module, "TMP_RUN_DIR", runs_dir / "test_run_unit")
        yield root
