"""Manually-entered FX rates and conversion. No external API / rate-fetching --
rates are entered by hand, local-first.

Rates are stored as decimal STRINGS (never float); all math uses
``decimal.Decimal``, mirroring ``bankapp.money``.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from bankapp import money
from bankapp.ingest.core import _utc_now_iso


def set_rate(
    conn: sqlite3.Connection,
    base: str,
    quote: str,
    rate,
    *,
    as_of: Optional[str] = None,
    now: Optional[str] = None,
) -> None:
    """Upsert the FX rate for (base, quote) on ``as_of`` (default: today).

    Re-running for the same (base, quote, as_of) UPDATES the existing row rather
    than inserting a duplicate -- latest wins, one row per pair per day.
    """
    base = base.upper()
    quote = quote.upper()
    as_of = as_of or date.today().isoformat()
    now = now or _utc_now_iso()
    with conn:
        conn.execute(
            """INSERT INTO fx_rate(base, quote, rate, as_of, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(base, quote, as_of) DO UPDATE SET
                 rate=excluded.rate, created_at=excluded.created_at""",
            (base, quote, str(rate), as_of, now),
        )


def latest_rate(conn: sqlite3.Connection, base: str, quote: str) -> Optional[Decimal]:
    """The most recent (max as_of) stored rate for (base, quote), or None if unset.

    Identity: ``latest_rate(X, X)`` always returns ``Decimal(1)``, no row needed.
    """
    base = base.upper()
    quote = quote.upper()
    if base == quote:
        return Decimal(1)
    row = conn.execute(
        "SELECT rate FROM fx_rate WHERE base = ? AND quote = ? ORDER BY as_of DESC LIMIT 1",
        (base, quote),
    ).fetchone()
    return Decimal(row["rate"]) if row else None


def list_rates(conn: sqlite3.Connection) -> list[dict]:
    """Latest rate per (base, quote) pair, sorted by base then quote."""
    rows = conn.execute(
        """SELECT base, quote, rate, as_of FROM fx_rate f
           WHERE as_of = (SELECT MAX(as_of) FROM fx_rate WHERE base = f.base AND quote = f.quote)
           ORDER BY base, quote"""
    ).fetchall()
    return [{"base": r["base"], "quote": r["quote"], "rate": r["rate"], "as_of": r["as_of"]} for r in rows]


def convert_minor(conn: sqlite3.Connection, amount_minor: int, from_cur: str, to_cur: str) -> Optional[int]:
    """Convert a signed minor-unit amount between currencies using the latest stored
    rate. Returns None if no rate path exists -- never silently converts at 0."""
    from_cur = from_cur.upper()
    to_cur = to_cur.upper()
    if from_cur == to_cur:
        return amount_minor
    rate = latest_rate(conn, from_cur, to_cur)
    if rate is None:
        return None
    major = money.from_minor(amount_minor, from_cur)
    converted = major * rate
    exp = money.exponent_for(to_cur)
    quantized = converted.quantize(Decimal(10) ** -exp, rounding=ROUND_HALF_UP)
    return money.to_minor(quantized, to_cur)
