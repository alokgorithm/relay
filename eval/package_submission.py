"""Build a deterministic, offline source submission from the current working tree.

Usage: python eval/package_submission.py [--source-path ROOT] [--output ZIP]
                                       [--include-evidence]
No Git checkout, provider, connector, or network access is needed. Symlinks are
omitted rather than followed. Live evidence is opt-in, sanitized, and restricted
to successful summary.md / verification.json pairs, never runtime traces/state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
ROOT_FILES = frozenset({
    ".gitignore", ".env.example", "README.md", "DESIGN.md", "SUBMISSION.md",
    "DEMO.md", "TECHNICAL_WALKTHROUGH.md", "cli.py", "config.yaml",
    "connectors.yaml", "pyproject.toml", "uv.lock",
})
SOURCE_DIRS = ("agent", "connectors", "company", "eval", "tests", "viewers")
EXCLUDED_DIRS = frozenset({
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", ".cache", "cache", "caches", "node_modules",
    "data", "runs", "dist", "build", "htmlcov", ".tox", ".nox", ".idea", ".vscode",
})
ARTIFACT_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".pyd", ".class", ".so", ".dll", ".exe", ".o", ".a",
    ".obj", ".lib", ".db", ".sqlite", ".sqlite3", ".zip", ".gz", ".bz2",
    ".xz", ".tar", ".7z", ".whl", ".egg", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".ico", ".pdf", ".mp4", ".mp3", ".woff", ".woff2", ".ttf",
    ".log", ".jsonl", ".coverage", ".swp", ".swo", ".pem", ".key", ".p12",
    ".pfx", ".crt", ".der", ".bak", ".tmp", ".bin", ".dat", ".dump",
        ".pkl", ".pickle", ".npy", ".npz", ".db3", ".wasm", ".dylib",
})
SENSITIVE_NAME = re.compile(
    r"(?:^|[._-])(?:credentials?|secrets?|keys?|private[_-]?key|id_rsa|id_ed25519)(?:[._-]|$)",
    re.IGNORECASE,
)
SENSITIVE_FIELD = re.compile(
    r"api[_-]?key|authorization|password|secret|credential|access[_-]?token|refresh[_-]?token",
    re.IGNORECASE,
)
# Require realistic token lengths, not short sk-test / mock credential strings.
SECRET_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    r"|\bsk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{20,}\b"
    r"|\b(?:ghp_|gho_|ghu_|ghs_|github_pat_)[A-Za-z0-9_]{30,}\b"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{20,}\b"
)
PLACEHOLDER_PATTERN = re.compile(
    r"(?:^|[-_])(?:placeholder|example|dummy|mock)(?:$|[-_])"
    r"|^(?:sk-)?(?:x+|your[-_]?(?:api[-_]?)?key(?:[-_]here)?)$",
    re.IGNORECASE,
)


class PackagingError(ValueError):
    """A submission could not be produced safely (never contains secret values)."""


def _configured_secrets(root: Path) -> tuple[str, ...]:
    values = {os.environ.get("LLM_API_KEY", "")}
    dotenv = root / ".env"
    if dotenv.is_symlink():
        raise PackagingError("Refusing to read a symlinked .env file")
    if dotenv.is_file():
        # Parse only this root's private .env; do not load it into the environment,
        # search parent directories, or interpolate values from the parent process.
        from dotenv import dotenv_values

        try:
            values.add(dotenv_values(dotenv, interpolate=False).get("LLM_API_KEY") or "")
        except (OSError, UnicodeError):
            raise PackagingError("Unable to read the source root's .env safely") from None
    return tuple(sorted((value for value in values if value), key=len, reverse=True))


def _unsafe_name(name: str) -> bool:
    lower = name.lower()
    return (
        lower == ".env" or lower.startswith(".env.")
        or bool(SENSITIVE_NAME.search(lower))
        or lower in {".ds_store", "thumbs.db", ".coverage"}
        or lower.endswith(("-wal", "-shm", "-journal", ".egg-info", ".dist-info"))
        or Path(lower).suffix in ARTIFACT_SUFFIXES
    )


def _source_files(root: Path):
    for name in sorted(ROOT_FILES):
        path = root / name
        if not path.is_symlink() and path.is_file():
            yield path
    for name in SOURCE_DIRS:
        directory = root / name
        if directory.is_symlink() or not directory.is_dir() or not directory.resolve().is_relative_to(root):
            continue
        for current, directories, filenames in os.walk(directory, followlinks=False):
            parent = Path(current)
            directories[:] = sorted(
                name for name in directories
                if name.lower() not in EXCLUDED_DIRS
                and not _unsafe_name(name)
                and not (parent / name).is_symlink()
                and (parent / name).resolve().is_relative_to(root)
            )
            for filename in sorted(filenames):
                path = parent / filename
                if (not path.is_symlink() and path.is_file() and not _unsafe_name(filename)
                                        and path.resolve().is_relative_to(root)):
                    yield path


def _text_bytes(path: Path) -> bytes | None:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        return None
    return data


def _check_secrets(data: bytes, secrets: tuple[str, ...]) -> None:
    if any(secret.encode("utf-8") in data for secret in secrets):
        raise PackagingError("Configured LLM_API_KEY found in prospective submission files")
    text = data.decode("utf-8")
    if any(not PLACEHOLDER_PATTERN.search(match.group()) for match in SECRET_PATTERN.finditer(text)):
        raise PackagingError("Potential secret credential found in prospective submission files")


def _sanitize_text(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    text = SECRET_PATTERN.sub("[REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", text)
    text = re.sub(r"https?://[^\s\"'<>]+", "[URL REDACTED]", text)
    return re.sub(
        r"(?im)(\b(?:api[_-]?key|password|secret|access[_-]?token|refresh[_-]?token)\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED]", text,
    )


def _sanitize_json(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {
            _sanitize_text(str(key), secrets): (
                "[REDACTED]" if SENSITIVE_FIELD.search(str(key)) else _sanitize_json(item, secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json(item, secrets) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value, secrets)
    return value


def _evidence_files(root: Path, secrets: tuple[str, ...]) -> dict[str, bytes]:
    base = root / "runs" / "live_validation"
    if ((root / "runs").is_symlink() or base.is_symlink() or not base.is_dir()
                or not base.resolve().is_relative_to(root)):
        return {}
    entries = {}
    for run in sorted(base.iterdir()):
        if (run.is_symlink() or not run.is_dir() or not run.resolve().is_relative_to(root)
                        or not re.fullmatch(r"[A-Za-z0-9_-]+", run.name)):
            continue
        summary, verification, report = (run / name for name in ("summary.md", "verification.json", "report.json"))
        if any(path.is_symlink() for path in (summary, verification, report)):
            continue
        if not summary.is_file() or not verification.is_file():
            continue
        try:
            verification_bytes = verification.read_bytes()
            results = json.loads(verification_bytes.decode("utf-8"))
            if not isinstance(results, list) or not results or not all(
                isinstance(result, dict) and result.get("passed") is True for result in results
            ):
                continue
            if report.exists():
                status = json.loads(report.read_text(encoding="utf-8"))
                if not isinstance(status, dict) or status.get("status") != "passed" or status.get("checked") is not True:
                    continue
            summary_bytes = _text_bytes(summary)
            if not summary_bytes or not summary_bytes.strip():
                continue
        except (OSError, UnicodeError, ValueError):
            continue
        # Configured credentials abort even in evidence. Other sensitive evidence
        # fields and provider error URLs are sanitized before the final scan.
        for data in (summary_bytes, verification_bytes):
            if any(secret.encode("utf-8") in data for secret in secrets):
                raise PackagingError("Configured LLM_API_KEY found in prospective submission files")
        entries[f"evidence/{run.name}/summary.md"] = _sanitize_text(
            summary_bytes.decode("utf-8"), secrets,
        ).encode("utf-8")
        entries[f"evidence/{run.name}/verification.json"] = (
            json.dumps(_sanitize_json(results, secrets), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
    return entries


def package_submission(
    source_path: Path | str = ROOT_DIR,
    output: Path | str | None = None,
    *,
    include_evidence: bool = False,
) -> Path:
    """Snapshot allowed on-disk text files and atomically write a stable ZIP."""
    root = Path(source_path).resolve()
    if not root.is_dir():
        raise PackagingError("Source path must be a directory")
    destination = Path(output).absolute() if output is not None else root / "dist" / "relay-submission.zip"
    if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents):
        raise PackagingError("Output path must not traverse symlinks")
    secrets = _configured_secrets(root)
    entries = {}
    for path in _source_files(root):
        if path.absolute() == destination:
            continue
        data = _text_bytes(path)
        if data is not None:
            _check_secrets(data, secrets)
            entries[path.relative_to(root).as_posix()] = data
    if include_evidence:
        entries.update(_evidence_files(root, secrets))
    if not entries:
        raise PackagingError("No allowed source files found")
    for name, data in entries.items():
        _check_secrets(name.encode("utf-8"), secrets)
        _check_secrets(data, secrets)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(entries.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data, compresslevel=9)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-path", type=Path, default=ROOT_DIR, help="Source root (default: eval's parent)")
    parser.add_argument("--output", type=Path, help="ZIP path (default: SOURCE/dist/relay-submission.zip)")
    parser.add_argument("--include-evidence", action="store_true", help="Include sanitized, passing live evidence only")
    args = parser.parse_args(argv)
    try:
        path = package_submission(args.source_path, args.output, include_evidence=args.include_evidence)
    except (PackagingError, OSError) as exc:
        # OSError filenames can themselves contain credentials; never echo them.
        print(f"Packaging failed: {exc if isinstance(exc, PackagingError) else 'filesystem error'}")
        return 1
    print(f"Created {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
