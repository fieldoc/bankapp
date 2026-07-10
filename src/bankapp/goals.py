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


_COLS = "id, name, target_minor, currency, start_date, target_date, allocation_pct, note, active"

_INSERT = (
    "INSERT INTO goals(name, target_minor, currency, start_date, target_date, "
    "allocation_pct, note, active) VALUES (?,?,?,?,?,?,?,1)"
)


def _to_goal(r: sqlite3.Row) -> Goal:
    return Goal(
        id=r["id"], name=r["name"], target_minor=r["target_minor"], currency=r["currency"],
        start_date=r["start_date"], target_date=r["target_date"],
        allocation_pct=r["allocation_pct"], note=r["note"], active=bool(r["active"]),
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
) -> None:
    """Validate everything needing no DB access. Raises ValidationError."""
    if not (name or "").strip():
        raise ValidationError("name is required")
    if not _is_int(target_minor) or target_minor <= 0:
        raise ValidationError("target must be greater than zero")
    if currency not in money.known_currencies():
        known = ", ".join(money.known_currencies())
        raise ValidationError(f"unknown currency {currency!r}; known currencies are {known}")
    start = _iso(start_date, "start_date")
    if target_date:
        # goals_status computes total_days = max(1, (target - start).days), so an
        # inverted range yields a nonsense pace rather than an error. Reject it here.
        if _iso(target_date, "target_date") < start:
            raise ValidationError("target_date cannot be before start_date")
    if not _is_int(allocation_pct) or not (0 <= allocation_pct <= 100):
        raise ValidationError("allocation_pct must be between 0 and 100")


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
) -> int:
    """Insert an active goal. Returns its new id."""
    name = (name or "").strip()
    check_fields(name=name, target_minor=target_minor, currency=currency,
                 start_date=start_date, target_date=target_date,
                 allocation_pct=allocation_pct)
    with conn:  # commits on success, rolls back if a check raises
        check_name_free(conn, name)
        check_allocation(conn, currency, allocation_pct)
        cur = conn.execute(
            _INSERT,
            (name, target_minor, currency, start_date, target_date, allocation_pct, note),
        )
        return int(cur.lastrowid)


def update(
    conn: sqlite3.Connection, goal_id: int, *, name: str, target_minor: int, currency: str,
    start_date: str, target_date: Optional[str] = None,
    allocation_pct: int = 100, note: Optional[str] = None,
) -> None:
    """Full replace of a goal's fields, including a rename. `active` is untouched."""
    name = (name or "").strip()
    check_fields(name=name, target_minor=target_minor, currency=currency,
                 start_date=start_date, target_date=target_date,
                 allocation_pct=allocation_pct)
    with conn:
        if get(conn, goal_id) is None:
            raise NotFound(f"no goal with id {goal_id}")
        check_name_free(conn, name, exclude_id=goal_id)
        check_allocation(conn, currency, allocation_pct, exclude_id=goal_id)
        conn.execute(
            "UPDATE goals SET name=?, target_minor=?, currency=?, start_date=?, "
            "target_date=?, allocation_pct=?, note=? WHERE id=?",
            (name, target_minor, currency, start_date, target_date,
             allocation_pct, note, goal_id),
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
                         allocation_pct=g.allocation_pct)
            # rowcount is unreliable for ON CONFLICT DO NOTHING; total_changes is not.
            # A pre-ledger DB may already hold the row: adopt it rather than insert.
            before = conn.total_changes
            conn.execute(
                _INSERT + " ON CONFLICT(name) DO NOTHING",
                (g.name, g.target_minor, g.currency, g.start_date, g.target_date,
                 g.allocation_pct, g.note),
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
