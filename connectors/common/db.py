import os
import sqlite3
from pathlib import Path
from typing import Any

DATA_DIR = Path(
    os.environ.get("RELAY_DATA_DIR")
    or Path(__file__).resolve().parent.parent.parent / "data"
).expanduser().resolve()


def get_db_path(connector_name: str) -> Path:
    """Get the SQLite database path for a connector."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{connector_name}.db"


def get_connection(connector_name: str) -> sqlite3.Connection:
    """Get a SQLite connection with row factory set."""
    db_path = get_db_path(connector_name)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def dict_from_row(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a dict."""
    return dict(row)


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Convert a list of sqlite3.Row to list of dicts."""
    return [dict(r) for r in rows]
