"""Transfer matching: pair opposite-sign hinted legs across accounts.

Rules-gated: only txns with txn_interp.role_hint='transfer' enter, so an unrelated
inflow can't pair with an unrelated purchase. Pairing itself is a pure function over
tuples (pair_legs); persistence and rebuild live below it.

A lone hinted leg = "not in a group" = pending (surfaced by v_pending_transfers with
age). When the counterpart lands later, the next run pairs it. No stored state machine.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from bankapp.ingest.core import _utc_now_iso


@dataclass(frozen=True)
class Leg:
    id: int
    account_id: int
    posted_date: str  # 'YYYY-MM-DD'
    amount_minor: int  # signed
    currency: str = "CAD"
    description_norm: str = ""


@dataclass(frozen=True)
class Pair:
    out_id: int
    in_id: int


def _days_between(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def _tokens(desc: str) -> frozenset[str]:
    """Alphanumeric tokens (>=2 chars) of a normalized description, for overlap scoring.

    Sub-account codes like ``wk2pdhr38cad`` / ``h62239605cad`` survive as single tokens,
    so the true counterpart (which shares the code + account-type words) scores higher
    than a coincidental same-amount/same-day leg with a different code.
    """
    out: set[str] = set()
    tok = ""
    for ch in desc.lower():
        if ch.isalnum():
            tok += ch
        else:
            if len(tok) >= 2:
                out.add(tok)
            tok = ""
    if len(tok) >= 2:
        out.add(tok)
    return frozenset(out)


def _desc_overlap(a: str, b: str) -> int:
    return len(_tokens(a) & _tokens(b))


def pair_legs(legs: Iterable[Leg], window_days: int, tolerance_minor: int) -> list[Pair]:
    """Greedy one-to-one pairing of outflow/inflow legs across different accounts.

    A candidate (out, in) requires: different accounts, SAME currency (a $500 USD
    outflow must never pair with a coincidental $500 CAD inflow), ``|out.amount +
    in.amount| <= tolerance_minor`` (fee tolerance), and ``date_diff <= window_days`` in
    either order (TD batch lag can post the out after the in). Candidates are sorted by
    (date_diff, amount_diff, -desc_overlap, out.id, in.id): among legs tied on date and
    amount, the pair whose descriptions share the most tokens (e.g. the same WS
    sub-account code) wins, so two same-amount/same-day transfers to different
    sub-accounts don't cross-pair. The greedy pass then takes each leg once.
    """
    legs = list(legs)
    outs = [l for l in legs if l.amount_minor < 0]
    ins = [l for l in legs if l.amount_minor > 0]

    candidates = []
    for o in outs:
        for i in ins:
            if o.account_id == i.account_id:
                continue
            if o.currency != i.currency:
                continue
            amount_diff = abs(o.amount_minor + i.amount_minor)
            if amount_diff > tolerance_minor:
                continue
            date_diff = _days_between(o.posted_date, i.posted_date)
            if date_diff > window_days:
                continue
            overlap = _desc_overlap(o.description_norm, i.description_norm)
            candidates.append((date_diff, amount_diff, -overlap, o.id, i.id))

    candidates.sort()  # (date_diff, amount_diff, -overlap, out_id, in_id)

    used: set[int] = set()
    pairs: list[Pair] = []
    for _date_diff, _amount_diff, _neg_overlap, out_id, in_id in candidates:
        if out_id in used or in_id in used:
            continue
        used.add(out_id)
        used.add(in_id)
        pairs.append(Pair(out_id=out_id, in_id=in_id))
    return pairs


def _ungrouped_hinted_legs(conn: sqlite3.Connection) -> list[Leg]:
    rows = conn.execute(
        """SELECT r.id, r.account_id, r.posted_date, r.amount_minor, r.currency, r.description_norm
           FROM raw_txn r
           JOIN txn_interp i ON i.raw_txn_id = r.id AND i.role_hint = 'transfer'
           LEFT JOIN group_members gm ON gm.raw_txn_id = r.id
           WHERE gm.raw_txn_id IS NULL"""
    ).fetchall()
    return [
        Leg(r["id"], r["account_id"], r["posted_date"], r["amount_minor"],
            r["currency"], r["description_norm"])
        for r in rows
    ]


def clear_generic_groups(conn: sqlite3.Connection) -> None:
    """Delete all generic transfer groups (CASCADE clears members).

    Split-expense groups and the transfer legs they claimed are untouched. Caller
    owns the transaction. Freed legs are re-claimable by split templates (which
    match first) or re-paired by the next match_transfers run.
    """
    conn.execute("DELETE FROM groups WHERE type = 'transfer'")


def match_transfers(
    conn: sqlite3.Connection,
    window_days: int,
    tolerance_minor: int,
    rebuild: bool = False,
) -> int:
    """Pair ungrouped hinted transfer legs into transfer groups. Returns pairs created.

    Idempotent: only ever adds groups over never-grouped legs, so a re-run is a no-op.
    ``rebuild`` first deletes all generic transfer groups (type='transfer'; split-expense
    transfer legs live in split_expense groups and are untouched) and rematches — the
    interpretation layer is deletable by design.
    """
    now = _utc_now_iso()
    with conn:
        if rebuild:
            clear_generic_groups(conn)
        pairs = pair_legs(_ungrouped_hinted_legs(conn), window_days, tolerance_minor)
        for p in pairs:
            cur = conn.execute(
                "INSERT INTO groups(type, status, created_at, updated_at) VALUES ('transfer','matched',?,?)",
                (now, now),
            )
            gid = cur.lastrowid
            conn.executemany(
                "INSERT INTO group_members(group_id, raw_txn_id, role) VALUES (?,?,?)",
                [(gid, p.out_id, "transfer_out"), (gid, p.in_id, "transfer_in")],
            )
    return len(pairs)
