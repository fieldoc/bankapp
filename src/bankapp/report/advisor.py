"""Advisor layer: balance snapshots, net worth, cashflow/savings, subscriptions,
leaks, budgets, goals, and the digest.

Everything here is analytics over the immutable ledger plus append-only
balance_snapshot rows. Amounts are per-currency and never converted. This is the data
engine for the frugally-luxurious mission: surface money slipping away unnoticed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Optional

from bankapp.ingest.core import _utc_now_iso

# Account types whose balances are liabilities (stored NEGATIVE for net worth).
_LIABILITY_TYPES = {"visa"}


def normalize_balance_for_type(balance_minor: int, account_type: str) -> int:
    """Liabilities (visa) reduce net worth, so store them negative regardless of the
    source's sign convention."""
    if account_type in _LIABILITY_TYPES:
        return -abs(balance_minor)
    return balance_minor


def snapshot_balance(
    conn: sqlite3.Connection,
    account_id: int,
    as_of: str,
    balance_minor: int,
    currency: str,
    source: str,
) -> bool:
    """Append a balance snapshot. Idempotent per (account, day, source). Returns True if
    a new row was written."""
    before = conn.total_changes
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO balance_snapshot
                 (account_id, as_of, balance_minor, currency, source, captured_at)
               VALUES (?,?,?,?,?,?)""",
            (account_id, as_of, balance_minor, currency, source, _utc_now_iso()),
        )
    return conn.total_changes > before


# ---- T8.2 net worth ---------------------------------------------------------

@dataclass(frozen=True)
class NetWorthRow:
    currency: str
    net_worth_minor: int
    freshest_as_of: str


def net_worth(conn: sqlite3.Connection) -> list[NetWorthRow]:
    """Latest snapshot per account, summed per currency (no conversion)."""
    return [
        NetWorthRow(r["currency"], r["net_worth_minor"], r["freshest_as_of"])
        for r in conn.execute("SELECT * FROM v_net_worth ORDER BY currency")
    ]


def net_worth_history(conn: sqlite3.Connection) -> list[dict]:
    """Month-end net worth series per currency from the snapshot history.

    For each (currency, month), take each account's latest snapshot within that month
    and sum them — a month-end-ish net worth track.
    """
    rows = conn.execute(
        """WITH monthly AS (
             SELECT account_id, currency, substr(as_of,1,7) AS month, MAX(as_of) AS as_of
             FROM balance_snapshot GROUP BY account_id, currency, substr(as_of,1,7)
           )
           SELECT m.month, b.currency, SUM(b.balance_minor) AS net_worth_minor
           FROM monthly m
           JOIN balance_snapshot b
             ON b.account_id = m.account_id AND b.as_of = m.as_of AND b.currency = m.currency
           GROUP BY m.month, b.currency
           ORDER BY b.currency, m.month"""
    ).fetchall()
    return [dict(r) for r in rows]
