"""Spend report + status dashboard over v_effective and the interpretation layer.

Per-currency subtotals only — amounts are NEVER converted across currencies.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SpendRow:
    category: str
    currency: str
    spend_minor: int  # positive magnitude of money out


def spend_total(conn: sqlite3.Connection, month: str) -> list[SpendRow]:
    """Total spend per currency for a month (money out only)."""
    rows = conn.execute(
        """SELECT currency, SUM(CASE WHEN effective_minor < 0 THEN -effective_minor ELSE 0 END) AS spend
           FROM v_effective WHERE substr(posted_date,1,7) = ?
           GROUP BY currency ORDER BY currency""",
        (month,),
    ).fetchall()
    return [SpendRow("(all)", r["currency"], r["spend"] or 0) for r in rows if (r["spend"] or 0) > 0]


def spend_by_category(conn: sqlite3.Connection, month: str) -> list[SpendRow]:
    """Spend per (category, currency) for a month; NULL category -> (uncategorized)."""
    rows = conn.execute(
        """SELECT COALESCE(category, '(uncategorized)') AS cat, currency,
                  SUM(CASE WHEN effective_minor < 0 THEN -effective_minor ELSE 0 END) AS spend
           FROM v_effective WHERE substr(posted_date,1,7) = ?
           GROUP BY cat, currency
           HAVING spend > 0
           ORDER BY spend DESC""",
        (month,),
    ).fetchall()
    return [SpendRow(r["cat"], r["currency"], r["spend"]) for r in rows]


# ---- status dashboard -------------------------------------------------------

@dataclass
class StatusReport:
    uncategorized: int
    pending_transfers: list  # rows: id, account_id, amount_minor, age_days, warn
    receivables: list        # rows: template, period_key, status, outstanding_minor, age_days
    last_import: Optional[str]
    last_ws_sync: Optional[str]
    ws_last_error: Optional[str]


def status(conn: sqlite3.Connection, transfer_window_days: int) -> StatusReport:
    from bankapp import db as dbmod
    from bankapp.classify import review

    pending = [
        {
            "id": r["id"], "account_id": r["account_id"], "amount_minor": r["amount_minor"],
            "age_days": r["age_days"], "warn": (r["age_days"] or 0) > 2 * transfer_window_days,
        }
        for r in conn.execute("SELECT * FROM v_pending_transfers ORDER BY age_days DESC")
    ]
    receivables = [
        dict(r) for r in conn.execute(
            """SELECT template, period_key, status, outstanding_minor, age_days
               FROM v_receivables WHERE outstanding_minor > 0 ORDER BY age_days DESC"""
        )
    ]
    last_import = conn.execute(
        "SELECT MAX(imported_at) FROM import_log"
    ).fetchone()[0]
    return StatusReport(
        uncategorized=review.count(conn),
        pending_transfers=pending,
        receivables=receivables,
        last_import=last_import,
        last_ws_sync=dbmod.get_meta(conn, "ws_last_sync"),
        ws_last_error=(dbmod.get_meta(conn, "ws_last_error") or None),
    )
