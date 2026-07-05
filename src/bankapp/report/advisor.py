"""Advisor layer: balance snapshots, net worth, cashflow/savings, subscriptions,
leaks, budgets, goals, and the digest.

Everything here is analytics over the immutable ledger plus append-only
balance_snapshot rows. Amounts are per-currency and never converted. This is the data
engine for the frugally-luxurious mission: surface money slipping away unnoticed.
"""

from __future__ import annotations

import calendar
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

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


# ---- T9.1 cashflow / savings ------------------------------------------------

@dataclass(frozen=True)
class CashflowRow:
    month: str
    currency: str
    income_minor: int
    spend_minor: int
    net_minor: int
    savings_rate: float  # net / income, 0.0 when income is 0 (no div-by-zero)


def monthly_cashflow(conn: sqlite3.Connection, months: Optional[int] = None) -> list[CashflowRow]:
    """Income/spend/net per month over v_effective (transfers already netted, rent = my
    share). savings_rate = net/income. Newest last; `months` keeps the last N."""
    rows = conn.execute(
        "SELECT month, currency, income_minor, spend_minor, net_minor FROM v_monthly_cashflow ORDER BY month"
    ).fetchall()
    out = []
    for r in rows:
        income = r["income_minor"] or 0
        rate = (r["net_minor"] / income) if income > 0 else 0.0
        out.append(CashflowRow(r["month"], r["currency"], income, r["spend_minor"] or 0,
                               r["net_minor"] or 0, rate))
    if months is not None:
        out = out[-months:]
    return out


# ---- T9.2 budgets -----------------------------------------------------------

def upsert_budgets(conn: sqlite3.Connection, budgets: dict[str, int], currency: str = "CAD") -> int:
    """Upsert per-category monthly limits from config [budgets]. Returns count."""
    n = 0
    with conn:
        for category, limit_minor in budgets.items():
            conn.execute(
                """INSERT INTO budgets(category, monthly_limit_minor, currency, active)
                   VALUES (?,?,?,1)
                   ON CONFLICT(category) DO UPDATE SET
                     monthly_limit_minor=excluded.monthly_limit_minor, currency=excluded.currency""",
                (category, limit_minor, currency),
            )
            n += 1
    return n


@dataclass(frozen=True)
class BudgetRow:
    category: str
    limit_minor: Optional[int]   # None for unbudgeted-but-spent categories
    actual_minor: int
    over: bool
    pace_warn: bool


def _month_elapsed_fraction(month: str, today: date) -> float:
    y, m = int(month[:4]), int(month[5:7])
    days = calendar.monthrange(y, m)[1]
    if (today.year, today.month) == (y, m):
        return today.day / days
    return 1.0 if date(y, m, 1) < today else 0.0


def budget_status(conn: sqlite3.Connection, month: str, today: Optional[date] = None) -> list[BudgetRow]:
    """Per-category actual vs limit for a month, with an ahead-of-pace warning."""
    today = today or date.today()
    elapsed = _month_elapsed_fraction(month, today)
    actual_by_cat = {
        r["cat"]: r["spend"]
        for r in conn.execute(
            """SELECT COALESCE(category,'(uncategorized)') AS cat,
                      SUM(CASE WHEN effective_minor < 0 THEN -effective_minor ELSE 0 END) AS spend
               FROM v_effective WHERE substr(posted_date,1,7) = ? GROUP BY cat HAVING spend > 0""",
            (month,),
        )
    }
    budgets = {
        r["category"]: r["monthly_limit_minor"]
        for r in conn.execute("SELECT category, monthly_limit_minor FROM budgets WHERE active = 1")
    }
    out: list[BudgetRow] = []
    for cat, limit in sorted(budgets.items()):
        actual = actual_by_cat.get(cat, 0)
        over = actual > limit
        spent_fraction = (actual / limit) if limit > 0 else 0.0
        pace_warn = (not over) and elapsed > 0 and spent_fraction > elapsed
        out.append(BudgetRow(cat, limit, actual, over, pace_warn))
    # unbudgeted categories that had spend, listed separately (limit None)
    for cat, actual in sorted(actual_by_cat.items()):
        if cat not in budgets:
            out.append(BudgetRow(cat, None, actual, False, False))
    return out


# ---- T9.3 subscriptions + leaks (pure over txn tuples) ----------------------

def merchant_token(description_norm: str) -> str:
    """Coarse merchant key: first whitespace token of the normalized description."""
    parts = description_norm.split()
    return parts[0] if parts else "(unknown)"


_CADENCES = (
    ("monthly", 30, 4),
    ("weekly", 7, 2),
    ("annual", 365, 10),
)


@dataclass(frozen=True)
class Subscription:
    merchant: str
    currency: str
    cadence: str
    monthly_cost_minor: int
    last_charge: str
    count: int
    price_creep: bool


def _classify_cadence(median_interval: float) -> Optional[str]:
    for name, center, tol in _CADENCES:
        if abs(median_interval - center) <= tol:
            return name
    return None


def _monthly_cost(cadence: str, amount_minor: int) -> int:
    a = abs(amount_minor)
    if cadence == "monthly":
        return a
    if cadence == "weekly":
        return round(a * 52 / 12)
    return round(a / 12)  # annual


def detect_subscriptions(txns: Iterable[tuple]) -> list[Subscription]:
    """Detect recurring charges. txns: (posted_date, amount_minor, description_norm, currency).

    Flags a merchant with >=3 outflow charges at a near-regular cadence (monthly +-4d /
    weekly +-2d / annual +-10d) and stable amounts (+-5%). Reports effective monthly
    cost and a price-creep flag (latest charge > trailing median of earlier charges).
    """
    groups: dict[tuple, list[tuple]] = {}
    for posted_date, amount_minor, desc_norm, currency in txns:
        if amount_minor >= 0:
            continue  # charges only
        groups.setdefault((merchant_token(desc_norm), currency), []).append((posted_date, amount_minor))

    subs: list[Subscription] = []
    for (merchant, currency), charges in groups.items():
        if len(charges) < 3:
            continue
        charges.sort(key=lambda c: c[0])
        dates = [date.fromisoformat(c[0]) for c in charges]
        intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        cadence = _classify_cadence(statistics.median(intervals))
        if cadence is None:
            continue
        amounts = [abs(c[1]) for c in charges]
        med_amt = statistics.median(amounts)
        if med_amt == 0 or (max(amounts) - min(amounts)) > 0.05 * med_amt:
            continue  # amounts not stable
        trailing_median = statistics.median(amounts[:-1])
        price_creep = amounts[-1] > trailing_median
        subs.append(
            Subscription(
                merchant=merchant, currency=currency, cadence=cadence,
                monthly_cost_minor=_monthly_cost(cadence, round(med_amt)),
                last_charge=charges[-1][0], count=len(charges), price_creep=price_creep,
            )
        )
    return sorted(subs, key=lambda s: -s.monthly_cost_minor)


@dataclass(frozen=True)
class LeakRow:
    merchant: str
    month: str
    currency: str
    total_minor: int
    count: int


def leak_report(txns: Iterable[tuple], threshold_minor: int) -> list[LeakRow]:
    """Drip spending. txns: (posted_date, amount_minor, description_norm, category, currency).

    Aggregates per (merchant, month) all outflows under threshold, plus everything
    categorized 'fees' regardless of size — the spending that never feels like a decision.
    """
    agg: dict[tuple, list[int]] = {}
    for posted_date, amount_minor, desc_norm, category, currency in txns:
        if amount_minor >= 0:
            continue
        is_small = abs(amount_minor) < threshold_minor
        is_fee = (category == "fees")
        if not (is_small or is_fee):
            continue
        key = (merchant_token(desc_norm), posted_date[:7], currency)
        agg.setdefault(key, []).append(-amount_minor)
    rows = [
        LeakRow(merchant, month, currency, sum(vals), len(vals))
        for (merchant, month, currency), vals in agg.items()
    ]
    return sorted(rows, key=lambda r: -r.total_minor)


def _effective_txn_tuples(conn: sqlite3.Connection):
    """(posted_date, effective_minor, description_norm, category, currency) over v_effective."""
    return conn.execute(
        "SELECT posted_date, effective_minor, description_norm, category, currency FROM v_effective"
    ).fetchall()


def subscriptions_from_db(conn: sqlite3.Connection) -> list[Subscription]:
    rows = _effective_txn_tuples(conn)
    return detect_subscriptions(
        (r["posted_date"], r["effective_minor"], r["description_norm"], r["currency"]) for r in rows
    )


def leaks_from_db(conn: sqlite3.Connection, threshold_minor: int) -> list[LeakRow]:
    rows = _effective_txn_tuples(conn)
    return leak_report(
        ((r["posted_date"], r["effective_minor"], r["description_norm"], r["category"], r["currency"])
         for r in rows),
        threshold_minor,
    )
