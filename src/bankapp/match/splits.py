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

import calendar
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


# ---- date helpers -----------------------------------------------------------

def _add_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def _clamped(year: int, month: int, day: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def _account_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["key"]: r["id"] for r in conn.execute("SELECT id, key FROM accounts")}


def _compile(pattern: str):
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(pattern), re.IGNORECASE)


# ---- T6.2 period + expense leg + share -------------------------------------

def _ensure_group(conn: sqlite3.Connection, template_id: int, period_key: str, now: str) -> int:
    conn.execute(
        """INSERT OR IGNORE INTO groups(type, status, template_id, period_key, created_at, updated_at)
           VALUES ('split_expense','open',?,?,?,?)""",
        (template_id, period_key, now, now),
    )
    return conn.execute(
        "SELECT id FROM groups WHERE template_id = ? AND period_key = ?",
        (template_id, period_key),
    ).fetchone()[0]


def _find_ungrouped_expense(conn, tmpl: Template, exp_acct_id: int, period_key: str):
    """Ungrouped expense txn in the period month matching the pattern, closest to day_of_month."""
    rows = conn.execute(
        """SELECT r.id, r.posted_date, r.amount_minor
           FROM raw_txn r
           LEFT JOIN group_members gm ON gm.raw_txn_id = r.id
           WHERE r.account_id = ? AND substr(r.posted_date,1,7) = ?
             AND r.amount_minor < 0 AND gm.raw_txn_id IS NULL""",
        (exp_acct_id, period_key),
    ).fetchall()
    pat = _compile(tmpl.expense_pattern)
    matches = [r for r in _rows_matching(conn, rows, pat)]
    if not matches:
        return None
    matches.sort(key=lambda r: (abs(date.fromisoformat(r["posted_date"]).day - tmpl.day_of_month), r["id"]))
    return matches[0]


def _rows_matching(conn, rows, pat):
    """Filter raw_txn rows whose description_norm matches the compiled pattern."""
    ids = [r["id"] for r in rows]
    if not ids:
        return []
    norms = {
        rr["id"]: rr["description_norm"]
        for rr in conn.execute(
            f"SELECT id, description_norm FROM raw_txn WHERE id IN ({','.join('?' * len(ids))})", ids
        )
    }
    return [r for r in rows if pat.search(norms[r["id"]])]


def _attach_expense(conn, tmpl: Template, exp_acct_id: int, gid: int, period_key: str) -> None:
    # already attached?
    exists = conn.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND role = 'expense'", (gid,)
    ).fetchone()
    if exists:
        return
    row = _find_ungrouped_expense(conn, tmpl, exp_acct_id, period_key)
    if row is None:
        return
    my_share, _remainder = money.share_split(abs(row["amount_minor"]), tmpl.share_numer, tmpl.share_denom)
    conn.execute(
        "INSERT INTO group_members(group_id, raw_txn_id, role, share_amount_minor) VALUES (?,?,'expense',?)",
        (gid, row["id"], my_share),
    )


# ---- T6.4 transfer-leg linking (TD->WS) ------------------------------------

def _window_bounds(expected_date: date, tmpl: Template) -> tuple[str, str]:
    lo = (expected_date - timedelta(days=REIMBURSE_LOOKBACK_DAYS)).isoformat()
    hi = (expected_date + timedelta(days=tmpl.window_days)).isoformat()
    return lo, hi


def _find_ungrouped_amount(conn, acct_id: int, target_minor: int, tol: int, lo: str, hi: str, sign: int):
    """One ungrouped txn in acct within [lo,hi], of the given sign, whose |amount| is
    within tol of target_minor. Returns the closest by amount, or None."""
    rows = conn.execute(
        """SELECT r.id, r.amount_minor FROM raw_txn r
           LEFT JOIN group_members gm ON gm.raw_txn_id = r.id
           WHERE r.account_id = ? AND gm.raw_txn_id IS NULL
             AND r.posted_date >= ? AND r.posted_date <= ?
             AND ((? > 0 AND r.amount_minor > 0) OR (? < 0 AND r.amount_minor < 0))""",
        (acct_id, lo, hi, sign, sign),
    ).fetchall()
    cands = [r for r in rows if abs(abs(r["amount_minor"]) - target_minor) <= tol]
    if not cands:
        return None
    cands.sort(key=lambda r: (abs(abs(r["amount_minor"]) - target_minor), r["id"]))
    return cands[0]


def _attach_transfer_legs(conn, tmpl, exp_acct_id, reimb_acct_id, gid, expected_date) -> None:
    if conn.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND role IN ('transfer_out','transfer_in')", (gid,)
    ).fetchone():
        return
    lo, hi = _window_bounds(expected_date, tmpl)
    tol = tmpl.amount_tolerance_minor
    out = _find_ungrouped_amount(conn, reimb_acct_id, tmpl.expected_amount_minor, tol, lo, hi, sign=-1)
    inn = _find_ungrouped_amount(conn, exp_acct_id, tmpl.expected_amount_minor, tol, lo, hi, sign=1)
    if out is None or inn is None:
        return  # incomplete pair -> leave for the generic transfer matcher
    conn.executemany(
        "INSERT INTO group_members(group_id, raw_txn_id, role) VALUES (?,?,?)",
        [(gid, out["id"], "transfer_out"), (gid, inn["id"], "transfer_in")],
    )


# ---- T6.3 reimbursement matching (AR-lite, FIFO across months) -------------

def _group_expected_and_received(conn, gid: int) -> tuple[Optional[int], int, Optional[str]]:
    """Returns (expected_receivable, received, expense_date) for a split group.
    expected_receivable = |expense| - my_share; None if no expense yet."""
    exp = conn.execute(
        """SELECT r.amount_minor, gm.share_amount_minor, r.posted_date
           FROM group_members gm JOIN raw_txn r ON r.id = gm.raw_txn_id
           WHERE gm.group_id = ? AND gm.role = 'expense'""",
        (gid,),
    ).fetchone()
    received = conn.execute(
        """SELECT COALESCE(SUM(ABS(r.amount_minor)), 0)
           FROM group_members gm JOIN raw_txn r ON r.id = gm.raw_txn_id
           WHERE gm.group_id = ? AND gm.role = 'reimbursement'""",
        (gid,),
    ).fetchone()[0]
    if exp is None:
        return (None, received, None)
    expected = abs(exp["amount_minor"]) - exp["share_amount_minor"]
    return (expected, received, exp["posted_date"])


def _match_reimbursements(conn, tmpl: Template, reimb_acct_id: int) -> None:
    """FIFO-allocate ungrouped reimbursement inflows to the oldest unsettled period."""
    pat = _compile(tmpl.reimburser_pattern)
    rows = conn.execute(
        """SELECT r.id, r.posted_date, r.amount_minor, r.description_norm
           FROM raw_txn r LEFT JOIN group_members gm ON gm.raw_txn_id = r.id
           WHERE r.account_id = ? AND r.amount_minor > 0 AND gm.raw_txn_id IS NULL
           ORDER BY r.posted_date, r.id""",
        (reimb_acct_id,),
    ).fetchall()
    candidates = [r for r in rows if pat.search(r["description_norm"])]

    # period groups of this template, oldest first
    groups = conn.execute(
        "SELECT id, period_key FROM groups WHERE template_id = ? ORDER BY period_key", (tmpl.id,)
    ).fetchall()

    for cand in candidates:
        cand_date = cand["posted_date"]
        target_gid = None
        for g in groups:  # oldest unsettled with the candidate in its window
            expected, received, exp_date = _group_expected_and_received(conn, g["id"])
            if expected is None or exp_date is None:
                continue
            outstanding = expected - received
            if outstanding <= 0:
                continue
            lo, hi = _window_bounds(date.fromisoformat(exp_date), tmpl)
            if lo <= cand_date <= hi:
                target_gid = g["id"]
                break
        if target_gid is not None:
            conn.execute(
                "INSERT INTO group_members(group_id, raw_txn_id, role) VALUES (?,?,'reimbursement')",
                (target_gid, cand["id"]),
            )


# ---- status recompute -------------------------------------------------------

def _recompute_status(conn, tmpl: Template, gid: int, expected_date: date, today: date) -> None:
    expected, received, exp_date = _group_expected_and_received(conn, gid)
    exp = conn.execute(
        """SELECT r.amount_minor FROM group_members gm JOIN raw_txn r ON r.id = gm.raw_txn_id
           WHERE gm.group_id = ? AND gm.role = 'expense'""",
        (gid,),
    ).fetchone()

    if exp is None:
        status = "missing_expense" if today > expected_date + timedelta(days=MISSING_EXPENSE_GRACE_DAYS) else "open"
    else:
        abs_amount = abs(exp["amount_minor"])
        if abs(abs_amount - tmpl.expected_amount_minor) > tmpl.amount_tolerance_minor:
            status = "amount_anomaly"
        else:
            window_end = date.fromisoformat(exp_date) + timedelta(days=tmpl.window_days)
            if received >= (expected - tmpl.amount_tolerance_minor):
                status = "settled"
            elif today <= window_end:
                status = "open"
            else:
                status = "underpaid"
    conn.execute(
        "UPDATE groups SET status = ?, updated_at = ? WHERE id = ?",
        (status, _utc_now_iso(), gid),
    )


# ---- top-level orchestration ------------------------------------------------

def match_splits(conn: sqlite3.Connection, today: Optional[date] = None) -> int:
    """Process all active split templates. Returns number of period groups touched."""
    today = today or date.today()
    acct_ids = _account_ids(conn)
    now = _utc_now_iso()
    touched = 0
    with conn:
        for tmpl in load_templates(conn):
            touched += _process_template(conn, tmpl, acct_ids, today, now)
    return touched


def _process_template(conn, tmpl: Template, acct_ids, today: date, now: str) -> int:
    exp_acct_id = acct_ids.get(tmpl.expense_account)
    reimb_acct_id = acct_ids.get(tmpl.reimburse_account)
    if exp_acct_id is None or reimb_acct_id is None:
        return 0

    row = conn.execute(
        "SELECT MIN(posted_date) AS m FROM raw_txn WHERE account_id IN (?,?)",
        (exp_acct_id, reimb_acct_id),
    ).fetchone()
    if not row or row["m"] is None:
        return 0

    start = date.fromisoformat(row["m"]).replace(day=1)
    end = today.replace(day=1)

    periods: list[tuple[str, date]] = []
    m = start
    while m <= end:
        pk = m.strftime("%Y-%m")
        expected_date = _clamped(m.year, m.month, tmpl.day_of_month)
        has_expense = _any_expense_in_month(conn, tmpl, exp_acct_id, pk)
        if expected_date <= today or has_expense:
            periods.append((pk, expected_date))
        m = _add_month(m)

    for pk, expected_date in periods:
        gid = _ensure_group(conn, tmpl.id, pk, now)
        _attach_expense(conn, tmpl, exp_acct_id, gid, pk)
        if tmpl.link_transfer:
            _attach_transfer_legs(conn, tmpl, exp_acct_id, reimb_acct_id, gid, expected_date)

    _match_reimbursements(conn, tmpl, reimb_acct_id)

    for pk, expected_date in periods:
        gid = conn.execute(
            "SELECT id FROM groups WHERE template_id = ? AND period_key = ?", (tmpl.id, pk)
        ).fetchone()[0]
        _recompute_status(conn, tmpl, gid, expected_date, today)
    return len(periods)


def _any_expense_in_month(conn, tmpl: Template, exp_acct_id: int, period_key: str) -> bool:
    rows = conn.execute(
        """SELECT r.id FROM raw_txn r
           WHERE r.account_id = ? AND substr(r.posted_date,1,7) = ? AND r.amount_minor < 0""",
        (exp_acct_id, period_key),
    ).fetchall()
    return bool(_rows_matching(conn, rows, _compile(tmpl.expense_pattern)))
