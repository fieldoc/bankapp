"""Anomaly detection: money-affecting oddities the advisor layer should surface.

Three kinds, each pure-testable over plain tuples/dataclasses:
- unusual_charge: a charge well above that merchant's own norm.
- stopped_subscription: a detected subscription gone quiet past its cadence.
- duplicate_charge: same account+amount+merchant within a few days.

``anomalies_from_db`` composes all three over ONE scan of v_effective (shared by
the unusual + duplicate detectors -- no per-merchant/per-account re-query) plus
one call to ``subscriptions_from_db`` for the stopped detector.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from bankapp import money

# ---- tuning constants ---------------------------------------------------------

UNUSUAL_MIN_HISTORY = 4         # minimum prior charges needed to establish a norm
UNUSUAL_MULT = 2.5              # latest must exceed this multiple of the median
UNUSUAL_ABS_FLOOR_MINOR = 1000  # ...and the absolute jump must clear this floor
UNUSUAL_LOOKBACK_DAYS = 60      # only flag spikes that are still "recent"
STOPPED_MULT = 1.5              # quiet-past-cadence multiplier
DUP_WINDOW_DAYS = 3             # adjacent same-amount charges within this many days


@dataclass(frozen=True)
class Anomaly:
    kind: str            # 'unusual_charge' | 'stopped_subscription' | 'duplicate_charge'
    merchant: str
    currency: str
    amount_minor: Optional[int]
    date: Optional[str]  # charge date (unusual/duplicate) or last_charge (stopped)
    detail: str          # human-readable specifics, e.g. "$120.00 vs usual $40.00"


# ---- pure detectors -------------------------------------------------------------

def detect_unusual_charges(txns: Iterable[tuple], today: date) -> list[Anomaly]:
    """txns: (posted_date, amount_minor, merchant, currency); amount_minor<0 = outflow.

    Groups by (merchant, currency). A group needs >= UNUSUAL_MIN_HISTORY prior
    charges (i.e. excluding the single latest-by-date charge) to establish a norm;
    the latest charge is flagged when it clears both the multiplier and the
    absolute floor over that norm's median, and is still within the lookback
    window of `today`.
    """
    groups: dict[tuple, list[tuple]] = {}
    for posted_date, amount_minor, merchant, currency in txns:
        if amount_minor >= 0:
            continue  # charges (outflows) only
        groups.setdefault((merchant, currency), []).append((posted_date, amount_minor))

    out: list[Anomaly] = []
    for (merchant, currency), charges in groups.items():
        charges_sorted = sorted(charges, key=lambda c: c[0])
        latest_date_s, latest_amount = charges_sorted[-1]
        hist = charges_sorted[:-1]
        if len(hist) < UNUSUAL_MIN_HISTORY:
            continue
        med = statistics.median(abs(a) for _, a in hist)
        latest_abs = abs(latest_amount)
        latest_date = date.fromisoformat(latest_date_s)
        within_lookback = abs((today - latest_date).days) <= UNUSUAL_LOOKBACK_DAYS
        if (
            latest_abs > UNUSUAL_MULT * med
            and (latest_abs - med) >= UNUSUAL_ABS_FLOOR_MINOR
            and within_lookback
        ):
            out.append(Anomaly(
                kind="unusual_charge", merchant=merchant, currency=currency,
                amount_minor=latest_amount, date=latest_date_s,
                detail=f"${money.from_minor(latest_abs, currency)} vs usual "
                       f"${money.from_minor(round(med), currency)}",
            ))
    return out


def detect_stopped_subscriptions(subs: Iterable, today: date) -> list[Anomaly]:
    """For each Subscription, flag it if it has gone quiet past STOPPED_MULT times
    its cadence interval (monthly=30d / weekly=7d / annual=365d)."""
    interval_days = {"monthly": 30, "weekly": 7, "annual": 365}
    out: list[Anomaly] = []
    for s in subs:
        interval = interval_days.get(s.cadence)
        if interval is None:
            continue
        last = date.fromisoformat(s.last_charge)
        days_since = (today - last).days
        if days_since > STOPPED_MULT * interval:
            out.append(Anomaly(
                kind="stopped_subscription", merchant=s.merchant, currency=s.currency,
                amount_minor=s.monthly_cost_minor, date=s.last_charge,
                detail=f"no {s.cadence} charge in {days_since} days",
            ))
    return out


def detect_duplicate_charges(txns: Iterable[tuple]) -> list[Anomaly]:
    """txns: (account_id, posted_date, amount_minor, merchant, currency); outflows only.

    Groups by (account_id, merchant, amount_minor, currency); within each group,
    any two date-adjacent charges <= DUP_WINDOW_DAYS apart each emit one Anomaly.
    """
    groups: dict[tuple, list[str]] = {}
    for account_id, posted_date, amount_minor, merchant, currency in txns:
        if amount_minor >= 0:
            continue  # charges (outflows) only
        groups.setdefault((account_id, merchant, amount_minor, currency), []).append(posted_date)

    out: list[Anomaly] = []
    for (account_id, merchant, amount_minor, currency), dates in groups.items():
        dates_sorted = sorted(dates)
        for i in range(len(dates_sorted) - 1):
            d1 = date.fromisoformat(dates_sorted[i])
            d2 = date.fromisoformat(dates_sorted[i + 1])
            if (d2 - d1).days <= DUP_WINDOW_DAYS:
                out.append(Anomaly(
                    kind="duplicate_charge", merchant=merchant, currency=currency,
                    amount_minor=amount_minor, date=dates_sorted[i + 1],
                    detail=f"${money.from_minor(abs(amount_minor), currency)} charged "
                           f"{dates_sorted[i]} and {dates_sorted[i + 1]}",
                ))
    return out


# ---- DB composer ------------------------------------------------------------

def anomalies_from_db(conn: sqlite3.Connection, today: Optional[date] = None) -> list[Anomaly]:
    """ONE scan of v_effective for real outflows (excludes transfer/reimbursement
    legs), shared by the unusual + duplicate detectors; one subscriptions_from_db
    call feeds the stopped detector. Stable order: kind (unusual, stopped,
    duplicate), then merchant within each kind.
    """
    from bankapp.report.advisor import merchant_token, subscriptions_from_db

    today = today or date.today()
    rows = conn.execute(
        """SELECT account_id, posted_date, amount_minor, currency, description_norm, counterparty
           FROM v_effective
           WHERE amount_minor < 0 AND (group_role IS NULL OR group_role = 'expense')"""
    ).fetchall()

    unusual_txns = []
    dup_txns = []
    for r in rows:
        merchant = r["counterparty"] or merchant_token(r["description_norm"])
        unusual_txns.append((r["posted_date"], r["amount_minor"], merchant, r["currency"]))
        dup_txns.append((r["account_id"], r["posted_date"], r["amount_minor"], merchant, r["currency"]))

    subs = subscriptions_from_db(conn)

    unusual = sorted(detect_unusual_charges(unusual_txns, today), key=lambda a: a.merchant)
    stopped = sorted(detect_stopped_subscriptions(subs, today), key=lambda a: a.merchant)
    duplicate = sorted(detect_duplicate_charges(dup_txns), key=lambda a: a.merchant)
    return unusual + stopped + duplicate
