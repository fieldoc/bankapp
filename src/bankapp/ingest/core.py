"""Ingest core: the frozen NormalizedTxn and the idempotent write path.

Every adapter (OFX, CSV, WS, Plaid) produces NormalizedTxn objects and hands them to
insert_batch(), which resolves account keys to ids and inserts via INSERT OR IGNORE
against UNIQUE(account_id, dedup_key). Re-ingesting the same data is always a no-op.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Union

from bankapp import normalize


class UnknownAccountError(ValueError):
    """Raised when a NormalizedTxn names an account key with no matching accounts row."""


@dataclass(frozen=True)
class NormalizedTxn:
    account_key: str
    posted_date: str          # 'YYYY-MM-DD' local
    amount_minor: int         # signed
    currency: str
    description_raw: str
    description_norm: str
    dedup_key: str            # 'fitid:...' | 'wsid:...' | 'plaid:...' | 'sha256:...'
    source: str               # 'ofx' | 'csv' | 'ws' | 'plaid'
    status: str = "posted"


def make_txn(
    account_key: str,
    posted_date: str,
    amount_minor: int,
    currency: str,
    description_raw: str,
    dedup_key: str,
    source: str,
    status: str = "posted",
) -> NormalizedTxn:
    """Build a NormalizedTxn, computing description_norm from the raw description."""
    return NormalizedTxn(
        account_key=account_key,
        posted_date=posted_date,
        amount_minor=amount_minor,
        currency=currency,
        description_raw=description_raw,
        description_norm=normalize.norm_desc(description_raw),
        dedup_key=dedup_key,
        source=source,
        status=status,
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _account_id_map(conn: sqlite3.Connection) -> dict[str, int]:
    return {row["key"]: row["id"] for row in conn.execute("SELECT id, key FROM accounts")}


def insert_batch(conn: sqlite3.Connection, txns: Iterable[NormalizedTxn]) -> tuple[int, int]:
    """Insert a batch in one transaction. Returns (inserted, skipped).

    Skips are rows whose (account_id, dedup_key) already exist. Unknown account key
    aborts the whole batch (nothing inserted) with UnknownAccountError.
    """
    txns = list(txns)
    if not txns:
        return (0, 0)

    id_map = _account_id_map(conn)
    for t in txns:
        if t.account_key not in id_map:
            raise UnknownAccountError(
                f"unknown account '{t.account_key}' — add it to config [[accounts]] and run `finance init`"
            )

    imported_at = _utc_now_iso()
    before = conn.total_changes
    with conn:  # one transaction: commit on success, rollback on error
        conn.executemany(
            """INSERT OR IGNORE INTO raw_txn
                 (account_id, posted_date, amount_minor, currency, description_raw,
                  description_norm, status, dedup_key, source, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    id_map[t.account_key],
                    t.posted_date,
                    t.amount_minor,
                    t.currency,
                    t.description_raw,
                    t.description_norm,
                    t.status,
                    t.dedup_key,
                    t.source,
                    imported_at,
                )
                for t in txns
            ],
        )
    inserted = conn.total_changes - before
    skipped = len(txns) - inserted
    return (inserted, skipped)


def file_sha256(path: Union[str, Path]) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def already_imported(conn: sqlite3.Connection, file_hash: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM import_log WHERE file_sha256 = ?", (file_hash,)
    ).fetchone()
    return row is not None


def record_import(
    conn: sqlite3.Connection,
    filename: str,
    file_hash: str,
    rows_inserted: int,
    rows_skipped: int,
) -> bool:
    """Record a processed file. Returns False if this exact file was already recorded
    (sha256 short-circuit), True if a new row was written."""
    if already_imported(conn, file_hash):
        return False
    with conn:
        conn.execute(
            """INSERT INTO import_log(filename, file_sha256, imported_at, rows_inserted, rows_skipped)
               VALUES (?,?,?,?,?)""",
            (filename, file_hash, _utc_now_iso(), rows_inserted, rows_skipped),
        )
    return True
