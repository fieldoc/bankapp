"""Savings-goal writes. The DB owns a goal's values; config only seeds new names.

Writes live here rather than in report/advisor.py, which reads. create(), update()
and seed_from_config() share check_fields() and check_allocation(), so the CLI and
the web UI cannot drift into different definitions of a legal goal.

Allocation is capped per currency, not globally: goals_status funds each goal from
_net_since(start_date, currency), so a CAD goal and a USD goal draw on separate
pools and a global cap would reject a legal pair.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from bankapp import money

# meta key holding the JSON list of config goal names that have ever been seeded.
_SEEDED_KEY = "goals_seeded"


class GoalError(ValueError):
    """Base for every rejected goal write."""


class ValidationError(GoalError):
    """A field is malformed or out of range."""


class DuplicateName(GoalError):
    """Another goal already holds this name (the column is UNIQUE)."""


class AllocationError(GoalError):
    """Active allocations for a currency would exceed 100%."""


class NotFound(GoalError):
    """No goal with the given id."""


@dataclass(frozen=True)
class Goal:
    id: int
    name: str
    target_minor: int
    currency: str
    start_date: str
    target_date: Optional[str]
    allocation_pct: int
    note: Optional[str]
    active: bool
    funding_mode: str
    monthly_minor: Optional[int]
    priority: int


_COLS = (
    "id, name, target_minor, currency, start_date, target_date, allocation_pct, note, active, "
    "funding_mode, monthly_minor, priority"
)

_INSERT = (
    "INSERT INTO goals(name, target_minor, currency, start_date, target_date, "
    "allocation_pct, note, active, funding_mode, monthly_minor, priority) "
    "VALUES (?,?,?,?,?,?,?,1,?,?,?)"
)


def _to_goal(r: sqlite3.Row) -> Goal:
    return Goal(
        id=r["id"], name=r["name"], target_minor=r["target_minor"], currency=r["currency"],
        start_date=r["start_date"], target_date=r["target_date"],
        allocation_pct=r["allocation_pct"], note=r["note"], active=bool(r["active"]),
        funding_mode=r["funding_mode"], monthly_minor=r["monthly_minor"], priority=r["priority"],
    )


def list_goals(conn: sqlite3.Connection, include_archived: bool = False) -> list[Goal]:
    sql = f"SELECT {_COLS} FROM goals"
    if not include_archived:
        sql += " WHERE active = 1"
    sql += " ORDER BY active DESC, name"
    return [_to_goal(r) for r in conn.execute(sql)]


def get(conn: sqlite3.Connection, goal_id: int) -> Optional[Goal]:
    r = conn.execute(f"SELECT {_COLS} FROM goals WHERE id = ?", (goal_id,)).fetchone()
    return _to_goal(r) if r else None


# ---- validation -------------------------------------------------------------

def _iso(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a date in YYYY-MM-DD form, got {value!r}")


def _is_int(value: object) -> bool:
    # bool subclasses int; True would otherwise sail through as 1.
    return isinstance(value, int) and not isinstance(value, bool)


def check_fields(
    *, name: str, target_minor: int, currency: str,
    start_date: str, target_date: Optional[str], allocation_pct: int,
    funding_mode: str = "target_date", monthly_minor: Optional[int] = None,
    priority: int = 100,
) -> None:
    """Validate everything needing no DB access. Raises ValidationError.

    funding_mode picks how a goal's monthly ask is computed (see monthly_ask()):
    'fixed_monthly' is a flat $/month bucket (target_minor may be 0 -- a
    perpetual bucket with no progress %), 'target_date' auto-computes the ask
    from target_minor + target_date (today's original, unchanged rule: the
    target must be positive and this mode never carries a monthly_minor).
    """
    if not (name or "").strip():
        raise ValidationError("name is required")
    if currency not in money.known_currencies():
        known = ", ".join(money.known_currencies())
        raise ValidationError(f"unknown currency {currency!r}; known currencies are {known}")
    if funding_mode not in ("fixed_monthly", "target_date"):
        raise ValidationError(
            f"funding_mode must be 'fixed_monthly' or 'target_date', got {funding_mode!r}"
        )
    if funding_mode == "fixed_monthly":
        if not _is_int(monthly_minor) or monthly_minor <= 0:
            raise ValidationError("monthly must be greater than zero for a fixed_monthly goal")
        if not _is_int(target_minor) or target_minor < 0:
            raise ValidationError("target must be zero or greater")
    else:
        if monthly_minor is not None:
            raise ValidationError("monthly must not be set unless funding_mode is fixed_monthly")
        if not _is_int(target_minor) or target_minor <= 0:
            raise ValidationError("target must be greater than zero")
    start = _iso(start_date, "start_date")
    if target_date:
        # goals_status computes total_days = max(1, (target - start).days), so an
        # inverted range yields a nonsense pace rather than an error. Reject it here.
        if _iso(target_date, "target_date") < start:
            raise ValidationError("target_date cannot be before start_date")
    if not _is_int(allocation_pct) or not (0 <= allocation_pct <= 100):
        raise ValidationError("allocation_pct must be between 0 and 100")
    if not _is_int(priority) or not (0 <= priority <= 999):
        raise ValidationError("priority must be between 0 and 999")


def check_name_free(
    conn: sqlite3.Connection, name: str, exclude_id: Optional[int] = None
) -> None:
    """Raise DuplicateName if another goal holds this name. Archived goals still
    hold theirs — the column is UNIQUE."""
    # `id IS NOT ?` degrades to `id IS NOT NULL` (always true) when exclude_id is None.
    row = conn.execute(
        "SELECT 1 FROM goals WHERE name = ? AND id IS NOT ?", (name, exclude_id)
    ).fetchone()
    if row:
        raise DuplicateName(f"a goal named {name!r} already exists")


def allocation_headroom(
    conn: sqlite3.Connection, currency: str, exclude_id: Optional[int] = None
) -> int:
    """Percentage points still unallocated for `currency` among ACTIVE goals."""
    row = conn.execute(
        "SELECT COALESCE(SUM(allocation_pct), 0) FROM goals "
        "WHERE active = 1 AND currency = ? AND id IS NOT ?",
        (currency, exclude_id),
    ).fetchone()
    return 100 - int(row[0])


def check_allocation(
    conn: sqlite3.Connection, currency: str, allocation_pct: int,
    exclude_id: Optional[int] = None,
) -> None:
    headroom = allocation_headroom(conn, currency, exclude_id)
    if allocation_pct > headroom:
        raise AllocationError(
            f"{currency} is {100 - headroom}% allocated; "
            f"this goal can take at most {headroom}%"
        )


# ---- writes -----------------------------------------------------------------

def create(
    conn: sqlite3.Connection, *, name: str, target_minor: int, currency: str,
    start_date: str, target_date: Optional[str] = None,
    allocation_pct: int = 100, note: Optional[str] = None,
    funding_mode: str = "target_date", monthly_minor: Optional[int] = None,
    priority: int = 100,
) -> int:
    """Insert an active goal. Returns its new id."""
    name = (name or "").strip()
    check_fields(name=name, target_minor=target_minor, currency=currency,
                 start_date=start_date, target_date=target_date,
                 allocation_pct=allocation_pct, funding_mode=funding_mode,
                 monthly_minor=monthly_minor, priority=priority)
    with conn:  # commits on success, rolls back if a check raises
        check_name_free(conn, name)
        check_allocation(conn, currency, allocation_pct)
        cur = conn.execute(
            _INSERT,
            (name, target_minor, currency, start_date, target_date, allocation_pct, note,
             funding_mode, monthly_minor, priority),
        )
        return int(cur.lastrowid)


def update(
    conn: sqlite3.Connection, goal_id: int, *, name: str, target_minor: int, currency: str,
    start_date: str, target_date: Optional[str] = None,
    allocation_pct: int = 100, note: Optional[str] = None,
    funding_mode: str = "target_date", monthly_minor: Optional[int] = None,
    priority: int = 100,
) -> None:
    """Full replace of a goal's fields, including a rename. `active` is untouched."""
    name = (name or "").strip()
    check_fields(name=name, target_minor=target_minor, currency=currency,
                 start_date=start_date, target_date=target_date,
                 allocation_pct=allocation_pct, funding_mode=funding_mode,
                 monthly_minor=monthly_minor, priority=priority)
    with conn:
        if get(conn, goal_id) is None:
            raise NotFound(f"no goal with id {goal_id}")
        check_name_free(conn, name, exclude_id=goal_id)
        check_allocation(conn, currency, allocation_pct, exclude_id=goal_id)
        conn.execute(
            "UPDATE goals SET name=?, target_minor=?, currency=?, start_date=?, "
            "target_date=?, allocation_pct=?, note=?, funding_mode=?, monthly_minor=?, "
            "priority=? WHERE id=?",
            (name, target_minor, currency, start_date, target_date,
             allocation_pct, note, funding_mode, monthly_minor, priority, goal_id),
        )


def archive(conn: sqlite3.Connection, goal_id: int) -> None:
    """Hide a goal without destroying it. Idempotent. Frees its allocation."""
    with conn:
        if get(conn, goal_id) is None:
            raise NotFound(f"no goal with id {goal_id}")
        conn.execute("UPDATE goals SET active = 0 WHERE id = ?", (goal_id,))


def unarchive(conn: sqlite3.Connection, goal_id: int) -> None:
    """Restore an archived goal. Idempotent. Re-spends its allocation, so the
    per-currency cap is re-checked — the pool may have been claimed since."""
    with conn:
        g = get(conn, goal_id)
        if g is None:
            raise NotFound(f"no goal with id {goal_id}")
        check_allocation(conn, g.currency, g.allocation_pct, exclude_id=goal_id)
        conn.execute("UPDATE goals SET active = 1 WHERE id = ?", (goal_id,))


def _seeded_names(conn: sqlite3.Connection) -> set[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (_SEEDED_KEY,)).fetchone()
    return set(json.loads(row[0])) if row else set()


def seed_from_config(conn: sqlite3.Connection, goals: Iterable) -> int:
    """Insert each config goal exactly once, ever. Existing rows are left alone.

    Seed-once (not upsert) is what lets a UI edit survive `finance init` and keeps an
    archived goal archived.

    Identity is tracked by a ledger of names already seeded, NOT by looking for the
    name in the goals table, because the app lets you rename. Renaming a config goal
    would otherwise make the next `finance init` fail to recognize it and re-insert
    the config version -- silently duplicating the goal, or blowing the per-currency
    cap when the two together exceed 100%.

    A name collision is therefore an expected case, so check_name_free is deliberately
    NOT called. The per-currency cap is checked once after all inserts, over the
    resulting active set; the enclosing transaction rolls both the inserts and the
    ledger update back if it fails.

    Returns the number of goals actually inserted.
    """
    inserted = 0
    with conn:
        seeded = _seeded_names(conn)
        pending = [g for g in goals if g.name not in seeded]
        for g in pending:
            check_fields(name=g.name, target_minor=g.target_minor, currency=g.currency,
                         start_date=g.start_date, target_date=g.target_date,
                         allocation_pct=g.allocation_pct, funding_mode=g.funding_mode,
                         monthly_minor=g.monthly_minor, priority=g.priority)
            # rowcount is unreliable for ON CONFLICT DO NOTHING; total_changes is not.
            # A pre-ledger DB may already hold the row: adopt it rather than insert.
            before = conn.total_changes
            conn.execute(
                _INSERT + " ON CONFLICT(name) DO NOTHING",
                (g.name, g.target_minor, g.currency, g.start_date, g.target_date,
                 g.allocation_pct, g.note, g.funding_mode, g.monthly_minor, g.priority),
            )
            inserted += conn.total_changes - before
        if pending:
            # Raw SQL, not db.set_meta: that opens its own `with conn:`, which would
            # commit mid-transaction and defeat the rollback below.
            names = json.dumps(sorted(seeded | {g.name for g in pending}))
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_SEEDED_KEY, names),
            )
        for currency in sorted({g.currency for g in pending}):
            headroom = allocation_headroom(conn, currency)
            if headroom < 0:
                raise AllocationError(
                    f"{currency} goal allocations total {100 - headroom}% > 100%"
                )
    return inserted


# ---- monthly ask --------------------------------------------------------------

def monthly_ask(*, funding_mode: str, monthly_minor: Optional[int], target_minor: int,
                funded_minor: int, target_date: Optional[str], today: date) -> int:
    """How much this goal wants set aside this month, in minor units.

    'fixed_monthly' is a flat pass-through of monthly_minor (0 if unset -- callers
    are expected to have validated it via check_fields, but this stays defensive
    since it's pure and has no DB to lean on).

    'target_date' auto-computes the ask by spreading what's left evenly over the
    months remaining, then rounding UP (ceil) so the goal is never short by a
    fraction of a cent's worth of rounding at the very end -- consistently asking
    a little more each month beats falling short right before the deadline.

    months_left counts the CURRENT month as one whole month: a target date that
    falls within this month yields months_left = 1, so the entire remainder is
    asked for now rather than spread past the deadline. A target_date already in
    the past clamps to the same months_left = 1, for the same reason -- there is
    no time left to spread over, so ask for everything remaining right away.
    """
    if funding_mode == "fixed_monthly":
        return monthly_minor or 0
    if not target_date or target_minor <= 0:
        return 0
    remaining = max(0, target_minor - funded_minor)
    if remaining == 0:
        return 0
    t = date.fromisoformat(target_date)
    months_left = max(1, (t.year - today.year) * 12 + (t.month - today.month) + 1)
    return -(-remaining // months_left)  # ceil division
