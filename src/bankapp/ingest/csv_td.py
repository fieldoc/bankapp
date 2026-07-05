"""TD chequing headerless CSV adapter.

# LAYOUT ASSUMED -- verify (T2.0). Believed columns (headerless):
#   Date (MM/DD/YYYY), Description, Withdrawal, Deposit, Balance
# Confirm against a real TD chequing CSV export before trusting in production.
# The Balance column is ignored; withdrawal -> negative, deposit -> positive.

CSV rows carry no transaction id, so dedup uses the content hash + a per-file
occurrence counter (normalize.assign_occurrences). This is stable across overlapping
export windows ONLY when whole days are exported (the documented user contract), so
identical same-day rows always appear together and get occurrences 0,1,... every time.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional, Union

from bankapp import money, normalize
from bankapp.ingest.core import NormalizedTxn, make_txn

EXPECTED_MIN_COLS = 4  # date, description, withdrawal, deposit (balance optional)


class MalformedCSVError(ValueError):
    """A CSV row did not match the believed TD layout."""


@dataclass
class _Staging:
    account_key: str
    posted_date: str
    amount_minor: int
    currency: str
    description_raw: str
    desc_norm: str
    occurrence: int = 0


def _amount_field(raw: str, currency: str) -> int:
    s = (raw or "").strip().replace(",", "").replace("$", "")
    if not s:
        return 0
    try:
        return money.to_minor(Decimal(s), currency)
    except (InvalidOperation, ValueError) as exc:
        raise MalformedCSVError(f"unparseable amount {raw!r}: {exc}") from exc


def _to_iso_date(raw: str) -> str:
    s = (raw or "").strip()
    try:
        return datetime.strptime(s, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise MalformedCSVError(f"bad date {raw!r} (expected MM/DD/YYYY): {exc}") from exc


def parse_td_csv(
    path: Union[str, Path], account_key: str, currency: str = "CAD"
) -> list[NormalizedTxn]:
    """Parse a headerless TD chequing CSV into NormalizedTxn rows with content-hash keys."""
    staging: list[_Staging] = []
    with Path(path).open("r", newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if not row or not any(cell.strip() for cell in row):
                continue  # blank line
            if len(row) < EXPECTED_MIN_COLS:
                raise MalformedCSVError(
                    f"row has {len(row)} columns, expected >= {EXPECTED_MIN_COLS}: {row!r}"
                )
            date_s, desc, withdrawal, deposit = row[0], row[1], row[2], row[3]
            amount_minor = _amount_field(deposit, currency) - _amount_field(withdrawal, currency)
            desc_raw = desc.strip()
            staging.append(
                _Staging(
                    account_key=account_key,
                    posted_date=_to_iso_date(date_s),
                    amount_minor=amount_minor,
                    currency=currency,
                    description_raw=desc_raw,
                    desc_norm=normalize.norm_desc(desc_raw),
                )
            )

    normalize.assign_occurrences(staging)
    out: list[NormalizedTxn] = []
    for s in staging:
        dedup_key = normalize.content_dedup_key(
            s.account_key, s.posted_date, s.amount_minor, s.currency, s.desc_norm, s.occurrence
        )
        out.append(
            make_txn(
                account_key=s.account_key,
                posted_date=s.posted_date,
                amount_minor=s.amount_minor,
                currency=s.currency,
                description_raw=s.description_raw,
                dedup_key=dedup_key,
                source="csv",
            )
        )
    return out
