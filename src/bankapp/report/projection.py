"""Safe-to-spend projection: a forward-looking "how much can I still spend this
month" number per currency, composed from existing analytics (monthly cashflow,
split-expense templates, subscriptions) -- no new source of truth.

safe_to_spend = max(0, expected_income - spent_so_far - committed_remaining)

committed_remaining = this month's unpaid split-expense my-shares
                     + subscriptions predicted to charge later this month
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from bankapp import money

# Cadence -> interval used to predict the next charge date from last_charge.
_SUB_INTERVALS = {
    "monthly": timedelta(days=30),
    "weekly": timedelta(days=7),
    "annual": timedelta(days=365),
}


@dataclass(frozen=True)
class ProjectionRow:
    currency: str
    month: str
    expected_income_minor: int
    spent_so_far_minor: int
    committed_remaining_minor: int
    safe_to_spend_minor: int


def _per_charge_minor(cadence: str, monthly_cost_minor: int) -> int:
    """Invert advisor._monthly_cost: recover the amount of a single charge from
    the monthly-normalized cost."""
    if cadence == "monthly":
        return monthly_cost_minor
    if cadence == "weekly":
        return round(monthly_cost_minor * 12 / 52)
    return round(monthly_cost_minor * 12)  # annual


def _grouped_template_ids(conn: sqlite3.Connection, period_key: str) -> set[int]:
    """template_ids that already have an 'expense' member for this period -- one
    query total, not one per template."""
    rows = conn.execute(
        """SELECT DISTINCT g.template_id FROM groups g
           JOIN group_members gm ON gm.group_id = g.id
           WHERE g.period_key = ? AND gm.role = 'expense'""",
        (period_key,),
    ).fetchall()
    return {r[0] for r in rows}


def month_projection(conn: sqlite3.Connection, today: Optional[date] = None) -> list[ProjectionRow]:
    from bankapp.match import splits
    from bankapp.report import advisor

    today = today or date.today()
    month = today.strftime("%Y-%m")

    cashflow = advisor.monthly_cashflow(conn, months=4)  # this month + up to 3 complete ones
    templates = splits.load_templates(conn)
    subs = advisor.subscriptions_from_db(conn)
    grouped_template_ids = _grouped_template_ids(conn, month)

    by_cur_cashflow: dict[str, list] = {}
    currencies: set[str] = set()
    for r in cashflow:
        by_cur_cashflow.setdefault(r.currency, []).append(r)
        currencies.add(r.currency)
    for t in templates:
        currencies.add(t.currency)
    for s in subs:
        currencies.add(s.currency)

    rows: list[ProjectionRow] = []
    for cur in sorted(currencies):
        cur_rows = by_cur_cashflow.get(cur, [])
        this_month_row = next((r for r in cur_rows if r.month == month), None)
        complete_months = sorted((r for r in cur_rows if r.month < month), key=lambda r: r.month)
        trailing = complete_months[-3:]
        if trailing:
            expected_income_minor = round(statistics.median(r.income_minor for r in trailing))
        else:
            expected_income_minor = this_month_row.income_minor if this_month_row else 0

        spent_so_far_minor = this_month_row.spend_minor if this_month_row else 0

        committed = 0
        for t in templates:
            if t.currency != cur:
                continue
            if t.start_period is not None and t.start_period > month:
                continue
            if t.id in grouped_template_ids:
                continue
            my_share, _remainder = money.share_split(t.expected_amount_minor, t.share_numer, t.share_denom)
            committed += my_share

        for s in subs:
            if s.currency != cur:
                continue
            interval = _SUB_INTERVALS.get(s.cadence)
            if interval is None:
                continue
            predicted = date.fromisoformat(s.last_charge) + interval
            if predicted.strftime("%Y-%m") == month and predicted > today:
                committed += _per_charge_minor(s.cadence, s.monthly_cost_minor)

        safe = max(0, expected_income_minor - spent_so_far_minor - committed)
        rows.append(ProjectionRow(
            currency=cur, month=month, expected_income_minor=expected_income_minor,
            spent_so_far_minor=spent_so_far_minor, committed_remaining_minor=committed,
            safe_to_spend_minor=safe,
        ))
    return rows
