"""SQLite connection + schema apply.

connect() turns on foreign keys; apply_schema() runs the idempotent DDL in
schema.sql (safe to call on every startup). Immutability of raw_txn is enforced by
triggers in that file, not here.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from typing import Union

_SCHEMA_RESOURCE = "schema.sql"


def _schema_sql() -> str:
    return resources.files("bankapp").joinpath(_SCHEMA_RESOURCE).read_text(encoding="utf-8")


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    """Open a connection with foreign keys ON and Row access by name."""
    p = Path(path).expanduser()
    if p.parent and str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    """Apply the DDL. Idempotent: every CREATE is IF NOT EXISTS."""
    conn.executescript(_schema_sql())
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return row[0] if row else ""


def init_db(path: Union[str, Path]) -> sqlite3.Connection:
    """Connect and apply schema in one step."""
    conn = connect(path)
    apply_schema(conn)
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a meta key (interpretation-layer state: cursors, last-sync, last-error)."""
    with conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default
