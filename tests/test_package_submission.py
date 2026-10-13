"""Offline packaging tests use only temporary source trees, never the real .env."""

import json
import os
import socket
import subprocess
import zipfile
from pathlib import Path

import pytest

from eval import package_submission as packaging


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Packaging must not use the network or Git/subprocesses")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    put(root, "README.md", "Current working-tree README\n")
    put(root, "agent/loop.py", "# Current uncommitted source\n")
    return root


def put(root, name, content: str | bytes = "excluded\n"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="\n")
    return path


def members(output):
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        return {name: archive.read(name) for name in archive.namelist()}


def evidence(root, run_id, results=None, **extra):
    directory = root / "runs" / "live_validation" / run_id
    put(directory, "summary.md", extra.get("summary", "Successful fictional live validation\n"))
    put(directory, "verification.json", json.dumps(
        [{"check": "persisted reply", "passed": True}] if results is None else results,
    ))
    put(directory, "state.json", '{"private": "raw state"}')
    put(directory, "runtime/trace.jsonl", "raw trace")
    put(directory, "evidence.json", '{"private": "raw evidence"}')
    if "report" in extra:
        put(directory, "report.json", json.dumps(extra["report"]))
    return directory


def test_allowlist_packages_current_tracked_and_untracked_source(source):
    for name in packaging.ROOT_FILES:
        put(source, name, f"current {name}\n")
    for directory in packaging.SOURCE_DIRS:
        put(source, f"{directory}/new_source.py", f"# untracked {directory}\n")
    put(source, ".git/HEAD", "old committed version")
    put(source, "private_notes.md")
    put(source, "unapproved/extra.py")
    output = packaging.package_submission(source)
    assert output == source / "dist" / "relay-submission.zip"
    contents = members(output)
    expected = set(packaging.ROOT_FILES) | {
        f"{directory}/new_source.py" for directory in packaging.SOURCE_DIRS
    } | {"agent/loop.py"}
    assert set(contents) == expected
    assert contents["README.md"] == b"current README.md\n"
    assert contents["agent/loop.py"] == b"# Current uncommitted source\n"
    put(source, "agent/loop.py", "# newer unsaved-to-Git version\n")
    assert members(packaging.package_submission(source))["agent/loop.py"] == b"# newer unsaved-to-Git version\n"


def test_excludes_nested_env_credentials_caches_databases_and_artifacts(source):
    excluded = (
        ".env", "agent/.env", "agent/deep/.env", "connectors/.env.production",
        "tests/nested/.env.example", "agent/credentials.json", "agent/api_keys.yaml",
        "agent/secret.txt", "connectors/id_rsa", "company/private-key.pem",
        "agent/.git/config", "agent/.venv/config.py", "agent/__pycache__/a.pyc",
        "agent/.pytest_cache/item", "tests/.ruff_cache/item", "tests/cache/file.txt",
        "agent/data/demo.db", "agent/runs/state.json", "agent/dist/demo.zip",
        "agent/demo.sqlite", "agent/demo.sqlite3", "agent/demo.db-wal",
        "agent/demo.db-shm", "agent/demo.db-journal", "agent/file.pyc",
        "agent/module.pyd", "agent/binary.exe", "agent/lib.dll", "agent/image.png",
                "agent/ascii.bin", "agent/ascii.dat", "agent/model.pkl", "agent/array.npy",
        "agent/trace.jsonl", "viewers/node_modules/index.js", "agent/build/out.py",
        "data/demo.db", "runs/state.json", "dist/old.zip",
    )
    for name in excluded:
        put(source, name)
    put(source, "agent/unknown", b"\x00\x01binary")
    put(source, "agent/invalid.txt", b"\xff\xfe")
    assert set(members(packaging.package_submission(source))) == {"README.md", "agent/loop.py"}


def test_zip_is_byte_repeatable_with_fixed_metadata(source):
    first = packaging.package_submission(source, source / "dist" / "first.zip")
    original = first.read_bytes()
    for path in (source / "README.md", source / "agent" / "loop.py"):
        os.utime(path, (1_600_000_000, 1_600_000_000))
    second = packaging.package_submission(source, source / "dist" / "second.zip")
    assert second.read_bytes() == original
    with zipfile.ZipFile(second) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.external_attr >> 16 == 0o100644
            assert not Path(info.filename).is_absolute()
            assert "\\" not in info.filename


def symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("This platform/user cannot create symlinks")


def test_excludes_external_and_internal_symlinks(source, tmp_path):
    outside = put(tmp_path, "outside.py", "external secret")
    outside_dir = tmp_path / "external"
    put(outside_dir, "leak.py", "external secret")
    symlink(source / "agent" / "outside.py", outside)
    symlink(source / "agent" / "linked", outside_dir, directory=True)
    symlink(source / "agent" / "inside.py", source / "agent" / "loop.py")
    symlink(source / "viewers", outside_dir, directory=True)
    assert set(members(packaging.package_submission(source))) == {"README.md", "agent/loop.py"}


def test_evidence_is_opt_in_and_only_successful_sanitized_pair(source):
    token = "sk-" + "A1b2C3d4" * 5
    evidence(source, "success", [
        {"passed": True, "detail": f"Bearer abc123 https://user:password@example.org/?token=private {token}",
         "api_key": "private", "nested": {"password": "private"}},
    ], summary=f"Verified result {token}\n", report={"status": "passed", "checked": True})
    evidence(source, "failure", [{"passed": False}])
    evidence(source, "mixed", [{"passed": True}, {"passed": False}])
    evidence(source, "empty", [])
    evidence(source, "truthy", [{"passed": "true"}])
    evidence(source, "numeric", [{"passed": 1}])
    evidence(source, "missing", [{"check": "not verified"}])
    evidence(source, "failed-report", report={"status": "failed", "checked": False})
    evidence(source, "invalid-type", {"passed": True})
    evidence(source, "invalid-json")
    put(source, "runs/live_validation/invalid-json/verification.json", "{")
    evidence(source, "no-summary")
    (source / "runs/live_validation/no-summary/summary.md").unlink()
    evidence(source, "blank-summary", summary=" \n")
    default = members(packaging.package_submission(source))
    assert not any(name.startswith("evidence/") for name in default)
    contents = members(packaging.package_submission(source, include_evidence=True))
    assert set(contents) - set(default) == {
        "evidence/success/summary.md", "evidence/success/verification.json",
    }
    exported = contents["evidence/success/verification.json"].decode()
    assert token not in exported
    assert "abc123" not in exported
    assert "example.org" not in exported
    assert "private" not in exported
    assert json.loads(exported)[0]["passed"] is True
    assert token.encode() not in contents["evidence/success/summary.md"]


def test_symlinked_evidence_is_excluded(source, tmp_path):
    run = evidence(source, "safe")
    (run / "summary.md").unlink()
    symlink(run / "summary.md", put(tmp_path, "outside.md"))
    assert not any(name.startswith("evidence/") for name in members(
        packaging.package_submission(source, include_evidence=True),
    ))


@pytest.mark.parametrize("origin", ["dotenv", "environment"])
def test_configured_llm_key_aborts_privately_without_overwriting_output(source, monkeypatch, capsys, origin):
    secret = "temporary-credential-" + "abcdefgh" * 3
    if origin == "dotenv":
        put(source, ".env", f'LLM_API_KEY="{secret}"\n')
    else:
        monkeypatch.setenv("LLM_API_KEY", secret)
    put(source, "tests/accidental.txt", f"Oops {secret}\n")
    output = put(source, "dist/relay-submission.zip", b"previous archive")
    assert packaging.main(["--source-path", str(source)]) == 1
    assert output.read_bytes() == b"previous archive"
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "Configured LLM_API_KEY" in captured.out
    assert not list(output.parent.glob("*.tmp"))


def test_dotenv_is_root_only_and_does_not_mutate_environment(source, tmp_path, monkeypatch):
    secret = "temporary-only-local-value"
    put(tmp_path, ".env", "LLM_API_KEY=parent-value\n")
    put(source, ".env", f"LLM_API_KEY={secret}\n")
    put(source, "README.md", "parent-value is documentation, not this root's key\n")
    contents = members(packaging.package_submission(source))
    assert b"parent-value" in contents["README.md"]
    assert "LLM_API_KEY" not in os.environ
    assert ".env" not in contents


def test_configured_secret_in_evidence_aborts_only_when_opted_in(source):
    secret = "temporary-configured-evidence-key"
    put(source, ".env", f"LLM_API_KEY={secret}\n")
    evidence(source, "success", summary=f"Accidentally echoed {secret}")
    packaging.package_submission(source)
    with pytest.raises(packaging.PackagingError, match="Configured LLM_API_KEY") as exc:
        packaging.package_submission(source, include_evidence=True)
    assert secret not in str(exc.value)


@pytest.mark.parametrize("secret", [
    "sk-" + "aB3dE5fG" * 5,
    "sk-proj-" + "aB3dE5fG" * 5,
    "sk-ant-api03-" + "aB3dE5fG" * 5,
    "ghp_" + "aB3dE5fG" * 5,
    "AKIA" + "ABCDEFGHIJKL1234",
    "-----BEGIN " + "PRIVATE KEY-----",
])
def test_obvious_nonplaceholder_secret_prefixes_fail_closed(source, secret):
    put(source, "agent/accidental.txt", secret)
    with pytest.raises(packaging.PackagingError, match="Potential secret") as exc:
        packaging.package_submission(source)
    assert secret not in str(exc.value)
    assert not (source / "dist").exists()


def test_short_mock_strings_and_explicit_placeholders_are_not_secrets(source):
    put(source, "tests/mock_values.py", 'VALUES = ["sk-test", "sk-mock-key", "configured-secret"]\n')
    put(source, ".env.example", "LLM_API_KEY=sk-placeholder-" + "x" * 30 + "\n")
    assert "tests/mock_values.py" in members(packaging.package_submission(source))


def test_cli_arguments_and_default_source(monkeypatch, source, tmp_path, capsys):
    calls = []

    def fake_package(source_path, output, *, include_evidence):
        calls.append((source_path, output, include_evidence))
        return tmp_path / "result.zip"

    monkeypatch.setattr(packaging, "package_submission", fake_package)
    assert packaging.main([]) == 0
    assert calls[-1] == (packaging.ROOT_DIR, None, False)
    output = tmp_path / "custom.zip"
    assert packaging.main([
        "--source-path", str(source), "--output", str(output), "--include-evidence",
    ]) == 0
    assert calls[-1] == (source, output, True)
    assert "Created" in capsys.readouterr().out


def test_empty_source_fails_without_creating_archive(tmp_path):
    with pytest.raises(packaging.PackagingError, match="No allowed source"):
        packaging.package_submission(tmp_path)
    assert not (tmp_path / "dist").exists()


def test_symlinked_dotenv_is_never_read(source, tmp_path):
    symlink(source / ".env", put(tmp_path, "private.env", "LLM_API_KEY=private-value\n"))
    with pytest.raises(packaging.PackagingError, match="symlinked .env"):
        packaging.package_submission(source)
    assert not (source / "dist").exists()


def test_symlinked_output_directory_is_rejected(source, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink(source / "dist", outside, directory=True)
    with pytest.raises(packaging.PackagingError, match="Output path"):
        packaging.package_submission(source)
    assert list(outside.iterdir()) == []


def test_failed_zip_write_keeps_previous_archive_and_cleans_temporary(source, monkeypatch):
    output = put(source, "dist/relay-submission.zip", b"previous archive")

    def fail_write(*args, **kwargs):
        raise OSError("simulated disk error")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_write)
    with pytest.raises(OSError, match="simulated disk error"):
        packaging.package_submission(source)
    assert output.read_bytes() == b"previous archive"
    assert not list(output.parent.glob("*.tmp"))


def test_include_evidence_without_live_runs_is_harmless(source):
    assert set(members(packaging.package_submission(source, include_evidence=True))) == {
        "README.md", "agent/loop.py",
    }


def test_preserves_original_source_bytes_including_crlf(source):
    content = b"# Windows source\r\nprint('hello')\r\n"
    put(source, "agent/loop.py", content)
    assert members(packaging.package_submission(source))["agent/loop.py"] == content
