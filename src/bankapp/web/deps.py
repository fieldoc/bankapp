"""FastAPI dependencies: opens a per-request connection and closes it. Most routes
only read; the categorization POST routes write through the classify engine."""

from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import Request

from bankapp import db as dbmod


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Yield a request-scoped connection against the configured DB, then close it."""
    conn = dbmod.connect(request.app.state.cfg.db_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()
