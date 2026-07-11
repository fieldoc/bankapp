"""Persisted advisor briefs (Claude coaching output). Append-only, like raw_txn.

Lets a future web UI display the latest brief without re-running the advisor.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

from bankapp.ingest.core import _utc_now_iso

_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VALID_SOURCES = {"claude", "manual"}


def add_brief(
    conn: sqlite3.Connection,
    content_md: str,
    digest_as_of: str,
    source: str = "claude",
    digest_json: Optional[str] = None,
) -> int:
    """Insert a new brief row. Raises ValueError on invalid input.

    `digest_json`, when provided, should be the JSON-serialized PURE digest() dict
    (i.e. without the volatile "changes_since_brief" key) this brief was based on --
    it becomes the "prior" snapshot for a future digest's changes_since_brief. Older
    briefs (or callers that don't pass it) store NULL and are ignored as a prior.
    """
    if not content_md or not content_md.strip():
        raise ValueError("content_md must not be empty")
    if not _AS_OF_RE.match(digest_as_of):
        raise ValueError(f"digest_as_of must match YYYY-MM-DD, got {digest_as_of!r}")
    if source not in _VALID_SOURCES:
        raise ValueError(f"source must be one of {sorted(_VALID_SOURCES)}, got {source!r}")

    with conn:
        cur = conn.execute(
            "INSERT INTO advisor_brief(created_at, digest_as_of, content_md, source, digest_json) "
            "VALUES (?,?,?,?,?)",
            (_utc_now_iso(), digest_as_of, content_md, source, digest_json),
        )
    return cur.lastrowid


def latest(conn: sqlite3.Connection) -> Optional[dict]:
    """Return the newest brief as a dict, or None if there are none."""
    row = conn.execute("SELECT * FROM advisor_brief ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row is not None else None


def list_briefs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Return briefs newest-first, up to limit."""
    rows = conn.execute(
        "SELECT * FROM advisor_brief ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
