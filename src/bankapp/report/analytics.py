"""Spend report + status dashboard over v_effective and the interpretation layer.

Per-currency subtotals only — amounts are NEVER converted across currencies.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Mapping, Optional


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


# ---- cash-flow Sankey -------------------------------------------------------
#
# A four-column flow diagram for one month, dominant currency only:
#   income sources -> Income -> category groups (+ Savings) -> categories
# Band width = dollars. The income and spend totals are read straight from
# v_monthly_cashflow, and the per-source / per-category queries reuse that
# view's exact predicates, so the partitioned sums reconcile with it by
# construction (asserted in tests).

_DIRECT_DEPOSIT_RE = re.compile(r"^direct deposit: from\s+(.+)$")

# Ordered: first substring match wins. Applied to description_norm, which is
# already lowercased/normalized upstream.
_INCOME_LABELS: tuple[tuple[str, str], ...] = (
    ("interest", "Interest"),
    ("stock lending", "Stock lending"),
    ("deposit: cheque", "Cheque deposit"),
)

OTHER_INCOME = "Other income"
FALLBACK_GROUP = "Other"

# Node-key prefixes double as column encodings for the Sankey renderer:
#   src: (col 0) -> inc:Income (col 1) -> grp:/sav: (col 2) -> cat: (col 3)
# Prefixing also prevents cross-column collisions -- a real category is literally
# named "income".
_INCOME_KEY = "inc:Income"
_SAVINGS_KEY = "sav:Savings"
# Overspent months: the shortfall is drawn from savings/balance. Modeled as an
# explicit income-side source so the Income node has a visible origin for every
# dollar it emits (mirrors the Savings sink of a surplus month), instead of a
# sourceless gap left by the renderer's max-sizing.
_DRAWDOWN_KEY = "src:From savings"


def income_source_label(description_norm: Optional[str]) -> str:
    """Display-only income source parsed from a normalized description.

    'direct deposit: from cloud produce a' -> 'Cloud Produce A'. Falls back to
    'Other income' for anything unrecognized. Never used for categorization.
    """
    d = (description_norm or "").strip()
    m = _DIRECT_DEPOSIT_RE.match(d)
    if m:
        return m.group(1).strip().title()
    for needle, label in _INCOME_LABELS:
        if needle in d:
            return label
    return OTHER_INCOME


@dataclass(frozen=True)
class FlowLink:
    source: str       # node key (prefixed)
    target: str       # node key (prefixed)
    flow_minor: int   # positive magnitude


@dataclass(frozen=True)
class MonthFlows:
    month: str
    currency: str                       # dominant currency shown
    income_total_minor: int             # == v_monthly_cashflow.income_minor
    spend_total_minor: int              # == v_monthly_cashflow.spend_minor
    savings_minor: int                  # income - spend; may be negative (overspent)
    links: list[FlowLink]
    labels: dict[str, str]              # node key -> display label
    other_currencies: list[str] = field(default_factory=list)


def month_flows(
    conn: sqlite3.Connection,
    month: str,
    category_groups: Mapping[str, str],
) -> Optional[MonthFlows]:
    """Build one month's cash-flow Sankey for the dominant currency.

    Returns None when the month has no activity in any currency.
    """
    # 1. Dominant currency + totals, straight from the reconciliation source.
    cf_rows = conn.execute(
        """SELECT currency, income_minor, spend_minor
           FROM v_monthly_cashflow WHERE month = ?""",
        (month,),
    ).fetchall()
    if not cf_rows:
        return None
    # volume = income + spend; ties broken lexicographically for determinism.
    ranked = sorted(
        cf_rows,
        key=lambda r: (-( (r["income_minor"] or 0) + (r["spend_minor"] or 0) ), r["currency"]),
    )
    top = ranked[0]
    currency = top["currency"]
    income_total = top["income_minor"] or 0
    spend_total = top["spend_minor"] or 0
    other_currencies = sorted(r["currency"] for r in cf_rows if r["currency"] != currency)

    links: list[FlowLink] = []
    labels: dict[str, str] = {_INCOME_KEY: "Income"}

    # 2. Income side: same predicate as the view's income arm.
    income_rows = conn.execute(
        """SELECT description_norm, SUM(effective_minor) AS amt
           FROM v_effective
           WHERE substr(posted_date,1,7) = ? AND currency = ?
             AND effective_minor > 0
             AND NOT (role_hint IS 'reimbursement' AND group_role IS NULL)
           GROUP BY description_norm""",
        (month, currency),
    ).fetchall()
    by_source: dict[str, int] = {}
    for r in income_rows:
        label = income_source_label(r["description_norm"])
        by_source[label] = by_source.get(label, 0) + (r["amt"] or 0)
    for src_label, amt in sorted(by_source.items(), key=lambda kv: (-kv[1], kv[0])):
        if amt <= 0:
            continue
        key = f"src:{src_label}"
        labels[key] = src_label
        links.append(FlowLink(key, _INCOME_KEY, amt))

    # 3. Spend side per category: the view's spend arm, partitioned by category.
    #    Ungrouped reimbursement inflows net against their own category's spend.
    cat_rows = conn.execute(
        """SELECT COALESCE(category, '(uncategorized)') AS cat,
                  SUM(CASE WHEN effective_minor < 0 THEN -effective_minor ELSE 0 END)
                - SUM(CASE WHEN effective_minor > 0
                             AND role_hint IS 'reimbursement' AND group_role IS NULL
                           THEN effective_minor ELSE 0 END) AS spend
           FROM v_effective
           WHERE substr(posted_date,1,7) = ? AND currency = ?
           GROUP BY cat""",
        (month, currency),
    ).fetchall()
    group_totals: dict[str, int] = {}
    for r in cat_rows:
        spend = r["spend"] or 0
        if spend <= 0:
            continue  # negative-net (refund-heavy) category: unrenderable band, omit
        cat = r["cat"]
        group = category_groups.get(cat, FALLBACK_GROUP)
        grp_key, cat_key = f"grp:{group}", f"cat:{cat}"
        labels[grp_key] = group
        labels[cat_key] = cat
        links.append(FlowLink(grp_key, cat_key, spend))
        group_totals[grp_key] = group_totals.get(grp_key, 0) + spend
    for grp_key, total in sorted(group_totals.items(), key=lambda kv: (-kv[1], kv[0])):
        links.append(FlowLink(_INCOME_KEY, grp_key, total))

    # 4. Savings: the leftover. Positive -> a terminal band out of Income.
    #    Overspent -> a "From savings" source band INTO Income covering the
    #    shortfall, so the Income node is fully sourced either way.
    savings = income_total - spend_total
    if savings > 0:
        labels[_SAVINGS_KEY] = "Savings"
        links.append(FlowLink(_INCOME_KEY, _SAVINGS_KEY, savings))
    elif savings < 0:
        labels[_DRAWDOWN_KEY] = "From savings"
        links.append(FlowLink(_DRAWDOWN_KEY, _INCOME_KEY, -savings))

    return MonthFlows(
        month=month,
        currency=currency,
        income_total_minor=income_total,
        spend_total_minor=spend_total,
        savings_minor=savings,
        links=links,
        labels=labels,
        other_currencies=other_currencies,
    )


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
               FROM v_receivables
               WHERE outstanding_minor > 0 AND status != 'settled'
               ORDER BY age_days DESC"""
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
