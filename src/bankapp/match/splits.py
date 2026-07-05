"""Split-expense / receivables matching: the 3-leg rent chain.

Per active template, per period (first-txn month -> today), each run lazily ensures a
group and attaches: the expense leg (with my floored share), the optional TD->WS
transfer pair, and reimbursement inflows (AR-lite, FIFO to the oldest unsettled period
across month boundaries). Status is recomputed every run from the current members, so
there is no stored state machine to corrupt. Everything is idempotent: UNIQUE(template,
period) and UNIQUE(raw_txn_id) keep re-runs no-ops.

Net in v_effective: expense -> -my_share, reimbursement -> 0, transfers -> 0, so a
month's spend is exactly my share.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from bankapp import money
from bankapp.ingest.core import _utc_now_iso

MISSING_EXPENSE_GRACE_DAYS = 7
REIMBURSE_LOOKBACK_DAYS = 14


@dataclass(frozen=True)
class Template:
    id: int
    name: str
    expected_amount_minor: int
    currency: str
    share_numer: int
    share_denom: int
    expense_account: str
    expense_pattern: str
    reimburse_account: str
    reimburser_pattern: str
    amount_tolerance_minor: int
    day_of_month: int
    window_days: int
    link_transfer: int


# ---- T6.1 template upsert ---------------------------------------------------

def upsert_templates(conn: sqlite3.Connection, templates) -> int:
    """Upsert config templates by name (id stable across edits). Returns count."""
    n = 0
    with conn:
        for t in templates:
            conn.execute(
                """INSERT INTO recurring_templates
                     (name, kind, expected_amount_minor, currency, cadence,
                      share_numer, share_denom, expense_account, expense_pattern,
                      reimburse_account, reimburser_pattern, amount_tolerance_minor,
                      day_of_month, window_days, link_transfer, active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(name) DO UPDATE SET
                     kind=excluded.kind, expected_amount_minor=excluded.expected_amount_minor,
                     currency=excluded.currency, cadence=excluded.cadence,
                     share_numer=excluded.share_numer, share_denom=excluded.share_denom,
                     expense_account=excluded.expense_account, expense_pattern=excluded.expense_pattern,
                     reimburse_account=excluded.reimburse_account,
                     reimburser_pattern=excluded.reimburser_pattern,
                     amount_tolerance_minor=excluded.amount_tolerance_minor,
                     day_of_month=excluded.day_of_month, window_days=excluded.window_days,
                     link_transfer=excluded.link_transfer""",
                (
                    t.name, t.kind, t.expected_amount_minor, t.currency, t.cadence,
                    t.share_numer, t.share_denom, t.expense_account, t.expense_pattern,
                    t.reimburse_account, t.reimburser_pattern, t.amount_tolerance_minor,
                    t.day_of_month, t.window_days, int(t.link_transfer),
                ),
            )
            n += 1
    return n


def load_templates(conn: sqlite3.Connection) -> list[Template]:
    rows = conn.execute(
        """SELECT id, name, expected_amount_minor, currency, share_numer, share_denom,
                  expense_account, expense_pattern, reimburse_account, reimburser_pattern,
                  amount_tolerance_minor, day_of_month, window_days, link_transfer
           FROM recurring_templates WHERE active = 1 AND kind = 'split_expense'"""
    ).fetchall()
    return [
        Template(
            id=r["id"], name=r["name"], expected_amount_minor=r["expected_amount_minor"],
            currency=r["currency"], share_numer=r["share_numer"], share_denom=r["share_denom"],
            expense_account=r["expense_account"], expense_pattern=r["expense_pattern"],
            reimburse_account=r["reimburse_account"], reimburser_pattern=r["reimburser_pattern"],
            amount_tolerance_minor=r["amount_tolerance_minor"], day_of_month=r["day_of_month"],
            window_days=r["window_days"], link_transfer=r["link_transfer"],
        )
        for r in rows
    ]
