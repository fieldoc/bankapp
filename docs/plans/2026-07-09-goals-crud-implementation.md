# In-app Goal Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user add, edit, and archive savings goals from the Goals page, with the database owning goal values and `config.toml` demoted to an insert-if-absent seed.

**Architecture:** A new `bankapp/goals.py` domain module owns all goal writes and validation; `report/advisor.py` keeps `goals_status` (a report) and loses `upsert_goals`. Five routes in `web/api.py` wrap the module, and `goals.html` gains a modal reusing the existing `.modal-card` CSS. No schema migration: archiving reuses the unused `goals.active` column.

**Tech Stack:** Python 3.14, sqlite3 (stdlib), FastAPI + Pydantic, Typer, vanilla JS, pytest.

Spec: [`docs/plans/2026-07-09-goals-crud-design.md`](2026-07-09-goals-crud-design.md)

---

## Current-state reality (READ THIS FIRST)

Grepped before slicing. Do not trust the spec's framing over this table.

| Surface | Status |
| --- | --- |
| `src/bankapp/goals.py` | **NET-NEW → build** (confirmed absent) |
| `money.known_currencies()` | **NET-NEW → build** (absent) |
| `include_archived` anywhere | **NET-NEW → build** (absent) |
| `goals.active` column | **ALREADY-BUILT → reuse.** Exists in `schema.sql:168`, defaults to 1, and is read by `goals_status` (`WHERE active = 1`). Nothing ever sets it to 0. No migration needed. |
| `advisor.upsert_goals` | **ALREADY-BUILT → move + change semantics** (`ON CONFLICT DO UPDATE` → `DO NOTHING`) |
| `advisor.AllocationError` | **ALREADY-BUILT → move** to `goals.py` |
| `App.post` (JS) | **ALREADY-BUILT → reuse.** `app.js:23`. Lifts `detail` into the error banner, then rethrows so the modal can stay open. |
| `.modal` / `.modal-card` / `.fld` / `.hint` / `.modal-actions` CSS | **ALREADY-BUILT → reuse.** `app.css:234-255`. Do not write new modal CSS. |

### Spec corrections found during grounding

The spec claims `tests/test_advisor_goals_digest.py` "must pass unmodified", protected by
re-exporting `AllocationError` from `advisor`. **That is wrong.** The test calls
`advisor.upsert_goals` directly at lines 28, 36, 44, 50, and 58. A re-exported exception
does not save a moved function.

Resolution (supersedes the spec): **do not add a back-compat re-export.** There are no
external consumers of `advisor.upsert_goals` — the only callers are `cli.py:86` and that
one test file. Task 5 updates all six call sites to `goals.seed_from_config` /
`goals.AllocationError` and the alias is never created. An `upsert_goals` alias that no
longer upserts would be a lie in the name.

Verified harmless (do NOT plan work for these):

- All five `upsert_goals` calls in that test insert into a **fresh empty DB** (`conn`
  fixture, `:memory:`). None exercises the `DO UPDATE` overwrite branch, so `DO NOTHING`
  is behaviourally identical for them.
- `digest()` builds its `goals` payload from an **explicit field whitelist**
  (`advisor.py:491-495`), so extending `GoalStatus` cannot perturb the digest JSON.
  `test_digest_json_keys_stable` is not coupled to this change.

### Cross-task test invariants (Lever 3)

- `tests/test_web_static.py::test_no_external_origins` scans every page in `PAGES` (which
  already includes `/goals.html`) for any non-localhost `http(s)://` origin. **The modal
  must be inline, local-only markup.** No CDN fonts, no external icons.
- `tests/test_web_api.py::test_meta_and_status_seeded` asserts `body["currencies"]` is a
  `dict` of `str -> int`. `filter_options` builds it from *data* (`raw_txn` /
  `balance_snapshot` / `accounts` currencies), not from an allowlist. Therefore
  `known_currencies` must be a **new, separate key** on `/api/meta`. Do not reshape or
  reuse `currencies`.
- Task 5 is the only RED window: it removes `advisor.upsert_goals` and updates its callers
  in the same commit. Every other task lands green independently.

### Snippet-shape notes (Lever 2)

All Python and JS below was written against the live files in this worktree. Two shapes
were verified rather than recalled:

- `INSERT INTO goals(...)` takes **7 placeholders + a literal `1` for `active`** (8
  columns). Copied from `advisor.py:371-377`.
- `cur.rowcount` is unreliable for `INSERT ... ON CONFLICT DO NOTHING`. Task 5 uses
  `conn.total_changes` deltas instead, and Step 1 of that task asserts the count
  empirically (Lever 5 — the value-behaviour check).

The `goals.html` rendering code is **illustrative in structure; live code is truth** for
`App.*` helper names — they are `App.el`, `App.esc`, `App.empty`, `App.fmtMoney`,
`App.api`, `App.post`, `App.notice`, `App.nav`, `App.loadMeta` (`app.js:59-216`).

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/bankapp/money.py` (modify) | Add `known_currencies()` — the allowlist that stops a typo'd currency silently defaulting to exponent 2. |
| `src/bankapp/goals.py` (create) | Goal writes + validation. `sqlite3` + `money` only; no FastAPI, so the CLI can call it. |
| `src/bankapp/report/advisor.py` (modify) | Drop `upsert_goals` + `AllocationError`. Extend `GoalStatus`; add `include_archived` to `goals_status`. |
| `src/bankapp/web/api.py` (modify) | `known_currencies` on `/api/meta`; `GoalIn`; five goal routes; `GoalError` → HTTP status mapping. |
| `src/bankapp/web/static/goals.html` (modify) | New-goal button, per-row Edit/Archive, archived disclosure, modal. |
| `src/bankapp/cli.py` (modify) | `init` calls `goals.seed_from_config`. |
| `tests/test_goals.py` (create) | Unit tests for the domain module. |
| `tests/test_web_api.py` (modify) | Route-level tests for the five goal routes. |
| `tests/test_web_static.py` (modify) | Assert the Goals page ships the CRUD hooks. |
| `tests/test_advisor_goals_digest.py` (modify) | Repoint six call sites off `advisor.upsert_goals`. |

**Running tests from this worktree requires `PYTHONPATH=src`.** Prefix every pytest
command below with it.

---

## Task 1: `money.known_currencies()`

**Files:**
- Modify: `src/bankapp/money.py:24-27`
- Test: `tests/test_money.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_money.py`:

```python
def test_known_currencies_is_the_exponent_allowlist():
    known = money.known_currencies()
    assert known == ("BTC", "CAD", "USD")


def test_known_currencies_excludes_codes_exponent_for_silently_defaults():
    # exponent_for("XYZ") returns the 2-place default rather than raising, so callers
    # taking user input must gate on known_currencies() instead.
    assert money.exponent_for("XYZ") == 2
    assert "XYZ" not in money.known_currencies()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src pytest tests/test_money.py -k known_currencies -v`
Expected: FAIL with `AttributeError: module 'bankapp.money' has no attribute 'known_currencies'`

- [ ] **Step 3: Write minimal implementation**

In `src/bankapp/money.py`, directly after `exponent_for`:

```python
def known_currencies() -> tuple[str, ...]:
    """Currencies with a defined minor-unit exponent, sorted.

    ``exponent_for`` silently falls back to two decimal places for an unrecognized
    code, so any caller accepting user input must gate on this allowlist first —
    otherwise a typo'd currency yields a row that matches no transactions and reads
    as 0% funded forever, with no error raised anywhere.
    """
    return tuple(sorted(_EXPONENTS))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src pytest tests/test_money.py -v`
Expected: PASS (all tests in file)

- [ ] **Step 5: Commit**

```bash
git add src/bankapp/money.py tests/test_money.py
git commit -m "feat(money): known_currencies() allowlist"
```

---

## Task 2: `goals.py` — dataclass, errors, reads, field validation

**Files:**
- Create: `src/bankapp/goals.py`
- Test: `tests/test_goals.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_goals.py`:

```python
"""Goal CRUD + validation. Real sqlite, no mocks."""

from __future__ import annotations

import pytest

from bankapp import goals


def _mk(conn, **kw):
    kw.setdefault("name", "trip")
    kw.setdefault("target_minor", 300000)
    kw.setdefault("currency", "CAD")
    kw.setdefault("start_date", "2026-01-01")
    kw.setdefault("target_date", "2026-12-31")
    kw.setdefault("allocation_pct", 100)
    kw.setdefault("note", None)
    return goals.create(conn, **kw)


# ---- reads + field validation ----

def test_list_goals_empty(conn):
    assert goals.list_goals(conn) == []


def test_get_returns_none_for_unknown_id(conn):
    assert goals.get(conn, 999) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "   "),
        ("target_minor", 0),
        ("target_minor", -5),
        ("currency", "XYZ"),
        ("start_date", "01-01-2026"),
        ("target_date", "2025-12-31"),  # before start_date
        ("allocation_pct", -1),
        ("allocation_pct", 101),
    ],
)
def test_check_fields_rejects(field, value):
    kw = dict(name="trip", target_minor=300000, currency="CAD",
              start_date="2026-01-01", target_date="2026-12-31", allocation_pct=100)
    kw[field] = value
    with pytest.raises(goals.ValidationError):
        goals.check_fields(**kw)


def test_check_fields_allows_absent_target_date():
    goals.check_fields(name="trip", target_minor=1, currency="CAD",
                       start_date="2026-01-01", target_date=None, allocation_pct=0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src pytest tests/test_goals.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bankapp.goals'`

- [ ] **Step 3: Write minimal implementation**

Create `src/bankapp/goals.py`:

```python
"""Savings-goal writes. The DB owns a goal's values; config only seeds new names.

Writes live here rather than in report/advisor.py, which reads. create(), update(),
and seed_from_config() share check_fields() and check_allocation(), so the CLI and
the web UI cannot drift into different definitions of a legal goal.

Allocation is capped per currency, not globally: goals_status funds each goal from
_net_since(start_date, currency), so a CAD goal and a USD goal draw on separate
pools and a global cap would reject a legal pair.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from bankapp import money


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


def _iso(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a date in YYYY-MM-DD form, got {value!r}")


def _is_int(value: object) -> bool:
    # bool is a subclass of int; True would otherwise sail through as 1.
    return isinstance(value, int) and not isinstance(value, bool)


def check_fields(
    *, name: str, target_minor: int, currency: str,
    start_date: str, target_date: Optional[str], allocation_pct: int,
) -> None:
    """Validate everything that needs no DB access. Raises ValidationError."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src pytest tests/test_goals.py -v`
Expected: FAIL — `goals.create` is not defined yet, so `_mk` is unused but importable;
the listed tests (`test_list_goals_empty`, `test_get_returns_none_for_unknown_id`,
`test_check_fields_*`) all PASS. If any of those fail, fix before continuing.

- [ ] **Step 5: Commit**

```bash
git add src/bankapp/goals.py tests/test_goals.py
git commit -m "feat(goals): domain module skeleton — Goal, error tree, reads, check_fields"
```

---

## Task 3: `goals.py` — name + per-currency allocation checks

**Files:**
- Modify: `src/bankapp/goals.py`
- Test: `tests/test_goals.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_goals.py`:

```python
# ---- allocation headroom + name uniqueness ----

def _raw_insert(conn, name, pct, currency="CAD", active=1):
    conn.execute(
        "INSERT INTO goals(name, target_minor, currency, start_date, target_date, "
        "allocation_pct, note, active) VALUES (?,?,?,?,?,?,?,?)",
        (name, 100000, currency, "2026-01-01", None, pct, None, active),
    )
    conn.commit()
    return conn.execute("SELECT id FROM goals WHERE name = ?", (name,)).fetchone()[0]


def test_headroom_is_100_on_empty_db(conn):
    assert goals.allocation_headroom(conn, "CAD") == 100


def test_headroom_counts_only_same_currency(conn):
    _raw_insert(conn, "cad-goal", 80, currency="CAD")
    assert goals.allocation_headroom(conn, "CAD") == 20
    assert goals.allocation_headroom(conn, "USD") == 100


def test_headroom_ignores_archived(conn):
    _raw_insert(conn, "old", 100, active=0)
    assert goals.allocation_headroom(conn, "CAD") == 100


def test_headroom_excludes_self(conn):
    gid = _raw_insert(conn, "trip", 100)
    assert goals.allocation_headroom(conn, "CAD") == 0
    assert goals.allocation_headroom(conn, "CAD", exclude_id=gid) == 100


def test_check_allocation_message_names_the_headroom(conn):
    _raw_insert(conn, "trip", 85)
    with pytest.raises(goals.AllocationError) as exc:
        goals.check_allocation(conn, "CAD", 20)
    assert "CAD is 85% allocated" in str(exc.value)
    assert "at most 15%" in str(exc.value)


def test_check_name_free(conn):
    gid = _raw_insert(conn, "trip", 10)
    with pytest.raises(goals.DuplicateName):
        goals.check_name_free(conn, "trip")
    goals.check_name_free(conn, "trip", exclude_id=gid)  # renaming to itself is fine
    goals.check_name_free(conn, "other")


def test_check_name_free_sees_archived_names(conn):
    # the column is UNIQUE, so an archived name is still taken
    _raw_insert(conn, "trip", 10, active=0)
    with pytest.raises(goals.DuplicateName):
        goals.check_name_free(conn, "trip")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src pytest tests/test_goals.py -k "headroom or name_free or allocation_message" -v`
Expected: FAIL with `AttributeError: module 'bankapp.goals' has no attribute 'allocation_headroom'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/bankapp/goals.py`:

```python
def check_name_free(conn: sqlite3.Connection, name: str, exclude_id: Optional[int] = None) -> None:
    """Raise DuplicateName if another goal holds this name. Archived goals still
    hold their name — the column is UNIQUE."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src pytest tests/test_goals.py -v`
Expected: PASS for every test defined so far.

- [ ] **Step 5: Commit**

```bash
git add src/bankapp/goals.py tests/test_goals.py
git commit -m "feat(goals): per-currency allocation headroom + name-uniqueness checks"
```

---

## Task 4: `goals.py` — create, update, archive, unarchive

**Files:**
- Modify: `src/bankapp/goals.py`
- Test: `tests/test_goals.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_goals.py`:

```python
# ---- writes ----

def test_create_then_get_round_trip(conn):
    gid = _mk(conn, note="hello")
    g = goals.get(conn, gid)
    assert (g.name, g.target_minor, g.currency, g.allocation_pct) == ("trip", 300000, "CAD", 100)
    assert g.active is True
    assert g.note == "hello"


def test_create_strips_name(conn):
    gid = _mk(conn, name="  trip  ")
    assert goals.get(conn, gid).name == "trip"


def test_create_rejects_duplicate_name(conn):
    _mk(conn, allocation_pct=50)
    with pytest.raises(goals.DuplicateName):
        _mk(conn, allocation_pct=50)


def test_create_rejects_allocation_breach(conn):
    _mk(conn, name="a", allocation_pct=60)
    with pytest.raises(goals.AllocationError):
        _mk(conn, name="b", allocation_pct=60)


def test_cad_and_usd_may_each_take_100_pct(conn):
    """Decision 3: allocation is a share of the goal's own currency pool."""
    _mk(conn, name="cad-trip", currency="CAD", allocation_pct=100)
    _mk(conn, name="usd-trip", currency="USD", allocation_pct=100)
    assert len(goals.list_goals(conn)) == 2


def test_update_can_lower_its_own_allocation(conn):
    """Headroom must exclude the goal under edit, or 100 -> 90 self-collides."""
    gid = _mk(conn, allocation_pct=100)
    goals.update(conn, gid, name="trip", target_minor=300000, currency="CAD",
                 start_date="2026-01-01", target_date="2026-12-31",
                 allocation_pct=90, note=None)
    assert goals.get(conn, gid).allocation_pct == 90


def test_update_can_rename(conn):
    gid = _mk(conn)
    goals.update(conn, gid, name="safari", target_minor=300000, currency="CAD",
                 start_date="2026-01-01", target_date="2026-12-31",
                 allocation_pct=100, note=None)
    assert goals.get(conn, gid).name == "safari"


def test_update_unknown_id_raises_not_found(conn):
    with pytest.raises(goals.NotFound):
        goals.update(conn, 999, name="x", target_minor=1, currency="CAD",
                     start_date="2026-01-01", target_date=None,
                     allocation_pct=1, note=None)


def test_archive_hides_and_frees_allocation(conn):
    gid = _mk(conn, allocation_pct=100)
    goals.archive(conn, gid)
    assert goals.list_goals(conn) == []
    assert goals.get(conn, gid).active is False
    assert goals.allocation_headroom(conn, "CAD") == 100


def test_archive_is_idempotent(conn):
    gid = _mk(conn)
    goals.archive(conn, gid)
    goals.archive(conn, gid)
    assert goals.get(conn, gid).active is False


def test_unarchive_restores(conn):
    gid = _mk(conn)
    goals.archive(conn, gid)
    goals.unarchive(conn, gid)
    assert goals.get(conn, gid).active is True


def test_unarchive_rejects_allocation_breach(conn):
    """Unarchiving re-spends allocation, so it must be re-checked."""
    gid = _mk(conn, name="old", allocation_pct=100)
    goals.archive(conn, gid)
    _mk(conn, name="new", allocation_pct=100)
    with pytest.raises(goals.AllocationError):
        goals.unarchive(conn, gid)


def test_archive_unknown_id_raises_not_found(conn):
    with pytest.raises(goals.NotFound):
        goals.archive(conn, 999)


def test_failed_create_leaves_no_row(conn):
    _mk(conn, name="a", allocation_pct=60)
    with pytest.raises(goals.AllocationError):
        _mk(conn, name="b", allocation_pct=60)
    assert [g.name for g in goals.list_goals(conn)] == ["a"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src pytest tests/test_goals.py -k "create or update or archive" -v`
Expected: FAIL with `AttributeError: module 'bankapp.goals' has no attribute 'create'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/bankapp/goals.py`:

```python
_INSERT = (
    "INSERT INTO goals(name, target_minor, currency, start_date, target_date, "
    "allocation_pct, note, active) VALUES (?,?,?,?,?,?,?,1)"
)


def create(
    conn: sqlite3.Connection, *, name: str, target_minor: int, currency: str,
    start_date: str, target_date: Optional[str] = None,
    allocation_pct: int = 100, note: Optional[str] = None,
) -> int:
    """Insert an active goal. Returns its new id."""
    name = (name or "").strip()
    check_fields(name=name, target_minor=target_minor, currency=currency,
                 start_date=start_date, target_date=target_date, allocation_pct=allocation_pct)
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
                 start_date=start_date, target_date=target_date, allocation_pct=allocation_pct)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src pytest tests/test_goals.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add src/bankapp/goals.py tests/test_goals.py
git commit -m "feat(goals): create/update/archive/unarchive with atomic validation"
```

---

## Task 5: `seed_from_config` replaces `advisor.upsert_goals` (the RED window)

This task removes `advisor.upsert_goals` and repoints all six call sites in the same
commit. Do not stop partway.

**Files:**
- Modify: `src/bankapp/goals.py`
- Modify: `src/bankapp/report/advisor.py:356-380` (delete `AllocationError` + `upsert_goals`)
- Modify: `src/bankapp/cli.py:80-95`
- Modify: `tests/test_advisor_goals_digest.py:28,36,43,44,50,58`
- Test: `tests/test_goals.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_goals.py`:

```python
# ---- config seeding ----

from bankapp.config import GoalConfig  # noqa: E402


def _cfg(name="trip", alloc=100, target=300000, currency="CAD"):
    return GoalConfig(name=name, target_minor=target, currency=currency,
                      start_date="2026-01-01", target_date="2026-12-31",
                      allocation_pct=alloc, note=None)


def test_seed_inserts_and_reports_count(conn):
    # Lever 5: rowcount is unreliable for ON CONFLICT DO NOTHING; pin the real number.
    assert goals.seed_from_config(conn, [_cfg("a", 50), _cfg("b", 50)]) == 2
    assert len(goals.list_goals(conn)) == 2


def test_seed_twice_is_a_no_op(conn):
    goals.seed_from_config(conn, [_cfg()])
    assert goals.seed_from_config(conn, [_cfg()]) == 0
    assert len(goals.list_goals(conn)) == 1


def test_seed_does_not_raise_duplicate_name(conn):
    """A name collision during seeding is the EXPECTED case, not an error."""
    goals.seed_from_config(conn, [_cfg()])
    goals.seed_from_config(conn, [_cfg()])  # must not raise


def test_seed_does_not_clobber_a_ui_edit(conn):
    """Decision 1: the DB owns a goal's values; config only seeds new names."""
    goals.seed_from_config(conn, [_cfg(target=300000)])
    gid = goals.list_goals(conn)[0].id
    goals.update(conn, gid, name="trip", target_minor=999, currency="CAD",
                 start_date="2026-01-01", target_date="2026-12-31",
                 allocation_pct=100, note="mine")
    goals.seed_from_config(conn, [_cfg(target=300000)])
    g = goals.get(conn, gid)
    assert g.target_minor == 999
    assert g.note == "mine"


def test_seed_does_not_resurrect_an_archived_goal(conn):
    """Decision 2: archiving survives `finance init`."""
    goals.seed_from_config(conn, [_cfg()])
    gid = goals.list_goals(conn)[0].id
    goals.archive(conn, gid)
    goals.seed_from_config(conn, [_cfg()])
    assert goals.get(conn, gid).active is False
    assert goals.list_goals(conn) == []


def test_seed_rejects_allocation_breach_and_rolls_back(conn):
    with pytest.raises(goals.AllocationError):
        goals.seed_from_config(conn, [_cfg("a", 60), _cfg("b", 60)])
    assert goals.list_goals(conn, include_archived=True) == []


def test_seed_allows_100_pct_in_each_currency(conn):
    assert goals.seed_from_config(conn, [_cfg("c", 100, currency="CAD"),
                                         _cfg("u", 100, currency="USD")]) == 2


def test_seed_validates_fields(conn):
    with pytest.raises(goals.ValidationError):
        goals.seed_from_config(conn, [_cfg(target=0)])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src pytest tests/test_goals.py -k seed -v`
Expected: FAIL with `AttributeError: module 'bankapp.goals' has no attribute 'seed_from_config'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/bankapp/goals.py`:

```python
def seed_from_config(conn: sqlite3.Connection, goals: Iterable) -> int:
    """Insert config goals whose names don't exist yet; leave existing rows alone.

    Insert-if-absent (not upsert) is what lets a UI edit survive `finance init` and
    keeps an archived goal archived. A name collision here is the expected case, so
    check_name_free is deliberately NOT called. The per-currency cap is checked once,
    after all inserts, over the resulting active set; the enclosing transaction rolls
    the inserts back if it fails.

    Returns the number of goals actually inserted.
    """
    inserted = 0
    with conn:
        for g in goals:
            check_fields(name=g.name, target_minor=g.target_minor, currency=g.currency,
                         start_date=g.start_date, target_date=g.target_date,
                         allocation_pct=g.allocation_pct)
            before = conn.total_changes
            conn.execute(
                _INSERT + " ON CONFLICT(name) DO NOTHING",
                (g.name, g.target_minor, g.currency, g.start_date, g.target_date,
                 g.allocation_pct, g.note),
            )
            inserted += conn.total_changes - before
        for currency in sorted({g.currency for g in goals}):
            headroom = allocation_headroom(conn, currency)
            if headroom < 0:
                raise AllocationError(
                    f"{currency} goal allocations total {100 - headroom}% > 100%"
                )
    return inserted
```

Note: the `goals` parameter shadows the module name inside this function only; the
module never refers to itself by name, so this is safe and matches the old signature.

- [ ] **Step 4: Delete the old implementation**

In `src/bankapp/report/advisor.py`, delete the `AllocationError` class and the whole
`upsert_goals` function (the block between `# ---- T10.1 goals ----` and
`@dataclass(frozen=True)\nclass GoalStatus`). Leave `GoalStatus` and `goals_status`.

- [ ] **Step 5: Repoint `cli.py`**

Replace `src/bankapp/cli.py:80-95` body so `init` reads:

```python
    seeded = classify.upsert_seed_rules(conn, cfg.transfers.seed_patterns)
    ntmpl = splits.upsert_templates(conn, cfg.templates)
    nbud = advisor.upsert_budgets(conn, cfg.budgets)
    try:
        ngoal = goalsmod.seed_from_config(conn, cfg.goals)
    except goalsmod.GoalError as exc:
        typer.echo(f"Goal config error: {exc}")
        raise typer.Exit(code=1)
    typer.echo(f"Initialized DB at {cfg.db_path}")
    typer.echo(f"Accounts: {len(cfg.accounts)} synced")
    typer.echo(f"Seed transfer rules: {seeded} added")
    typer.echo(f"Templates: {ntmpl} upserted")
    typer.echo(f"Budgets: {nbud} upserted")
    typer.echo(f"Goals: {ngoal} seeded")
```

and add `from bankapp import goals as goalsmod` to the local imports at the top of
`init` (beside `from bankapp.report import advisor`). Catch `GoalError`, not just
`AllocationError`, so a malformed `[[goals]]` block reports cleanly instead of
tracebacking.

- [ ] **Step 6: Repoint `tests/test_advisor_goals_digest.py`**

Add `from bankapp import goals as goalsmod` to the imports. Replace every
`advisor.upsert_goals(` with `goalsmod.seed_from_config(` (lines 28, 36, 44, 50, 58) and
`advisor.AllocationError` with `goalsmod.AllocationError` (line 43).

- [ ] **Step 7: Run the full suite**

Run: `PYTHONPATH=src pytest -q`
Expected: PASS. `test_allocation_over_100_rejected` still raises (60+60 CAD breaches the
per-currency cap); `test_inactive_goal_excluded` still passes (it sets `active = 0` by raw
SQL and reads through `goals_status`).

- [ ] **Step 8: Commit**

```bash
git add src/bankapp/goals.py src/bankapp/report/advisor.py src/bankapp/cli.py \
        tests/test_goals.py tests/test_advisor_goals_digest.py
git commit -m "feat(goals)!: config seeds, DB owns — replace upsert_goals with seed_from_config"
```

---

## Task 6: `goals_status` gains id/dates/note/active + `include_archived`

**Files:**
- Modify: `src/bankapp/report/advisor.py` (`GoalStatus`, `goals_status`)
- Test: `tests/test_advisor_goals_digest.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_advisor_goals_digest.py`:

```python
def test_goals_status_exposes_edit_fields(conn):
    goalsmod.seed_from_config(conn, [_goal()])
    g = advisor.goals_status(conn, today=date(2026, 6, 1))[0]
    assert g.id > 0
    assert g.start_date == "2026-01-01"
    assert g.target_date == "2026-12-31"
    assert g.active is True


def test_goals_status_include_archived(conn):
    goalsmod.seed_from_config(conn, [_goal(name="trip")])
    conn.execute("UPDATE goals SET active = 0 WHERE name = 'trip'")
    conn.commit()
    assert advisor.goals_status(conn) == []
    archived = advisor.goals_status(conn, include_archived=True)
    assert [g.name for g in archived] == ["trip"]
    assert archived[0].active is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src pytest tests/test_advisor_goals_digest.py -k "edit_fields or include_archived" -v`
Expected: FAIL — `AttributeError: 'GoalStatus' object has no attribute 'id'`

- [ ] **Step 3: Write minimal implementation**

In `src/bankapp/report/advisor.py`, replace `GoalStatus` and `goals_status`:

```python
@dataclass(frozen=True)
class GoalStatus:
    id: int
    name: str
    target_minor: int
    funded_minor: int
    currency: str
    allocation_pct: int
    pct_complete: float
    pace: str  # 'on_track' | 'behind' | 'no_target'
    start_date: str
    target_date: Optional[str]
    note: Optional[str]
    active: bool


def goals_status(
    conn: sqlite3.Connection,
    today: Optional[date] = None,
    include_archived: bool = False,
) -> list[GoalStatus]:
    today = today or date.today()
    sql = """SELECT id, name, target_minor, currency, start_date, target_date,
                    allocation_pct, note, active
             FROM goals"""
    if not include_archived:
        sql += " WHERE active = 1"
    sql += " ORDER BY active DESC, name"
    out: list[GoalStatus] = []
    for g in conn.execute(sql):
        net = _net_since(conn, g["start_date"], g["currency"])
        funded = round(net * g["allocation_pct"] / 100)
        pct = (funded / g["target_minor"] * 100) if g["target_minor"] > 0 else 0.0
        pace = "no_target"
        if g["target_date"]:
            start = date.fromisoformat(g["start_date"])
            target = date.fromisoformat(g["target_date"])
            total_days = max(1, (target - start).days)
            elapsed = min(max((today - start).days, 0), total_days)
            expected = g["target_minor"] * elapsed / total_days
            pace = "on_track" if funded >= expected else "behind"
        out.append(GoalStatus(
            id=g["id"], name=g["name"], target_minor=g["target_minor"], funded_minor=funded,
            currency=g["currency"], allocation_pct=g["allocation_pct"], pct_complete=pct,
            pace=pace, start_date=g["start_date"], target_date=g["target_date"],
            note=g["note"], active=bool(g["active"]),
        ))
    return out
```

`digest()` needs no change: it builds its `goals` payload from an explicit field
whitelist, so the new attributes cannot leak into the digest JSON.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=src pytest tests/test_advisor_goals_digest.py tests/test_cli_advisor.py -q`
Expected: PASS. `test_digest_json_keys_stable` in particular must stay green.

- [ ] **Step 5: Commit**

```bash
git add src/bankapp/report/advisor.py tests/test_advisor_goals_digest.py
git commit -m "feat(advisor): GoalStatus carries id/dates/note/active; goals_status(include_archived)"
```

---

## Task 7: Five goal routes + `known_currencies` on `/api/meta`

**Files:**
- Modify: `src/bankapp/web/api.py`
- Test: `tests/test_web_api.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_api.py`:

```python
def _init(app_env):
    dbmod.init_db(app_env["db"])
    return _client(app_env)


def _body(**kw):
    b = {"name": "trip", "target": "3000.00", "currency": "CAD",
         "start_date": "2026-01-01", "target_date": "2026-12-31",
         "allocation_pct": 100, "note": None}
    b.update(kw)
    return b


def test_meta_exposes_known_currencies(app_env):
    client = _init(app_env)
    body = client.get("/api/meta").json()
    assert body["known_currencies"] == ["BTC", "CAD", "USD"]
    # the data-derived map is a separate key and keeps its shape
    assert isinstance(body["currencies"], dict)


def test_create_goal_then_list(app_env):
    client = _init(app_env)
    r = client.post("/api/goals", json=_body())
    assert r.status_code == 200, r.text
    gid = r.json()["id"]
    goals = client.get("/api/goals").json()
    assert [g["name"] for g in goals] == ["trip"]
    assert goals[0]["id"] == gid
    assert goals[0]["target_minor"] == 300000  # "3000.00" CAD -> minor units


def test_create_goal_rejects_bad_money(app_env):
    client = _init(app_env)
    r = client.post("/api/goals", json=_body(target="3000.999"))
    assert r.status_code == 400
    assert "precision" in r.json()["detail"]


def test_create_goal_rejects_unknown_currency(app_env):
    client = _init(app_env)
    r = client.post("/api/goals", json=_body(currency="XYZ"))
    assert r.status_code == 400
    assert "unknown currency" in r.json()["detail"]


def test_create_goal_duplicate_name_conflicts(app_env):
    client = _init(app_env)
    client.post("/api/goals", json=_body(allocation_pct=50))
    r = client.post("/api/goals", json=_body(allocation_pct=50))
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


def test_create_goal_allocation_breach_is_400_with_headroom(app_env):
    client = _init(app_env)
    client.post("/api/goals", json=_body(name="a", allocation_pct=85))
    r = client.post("/api/goals", json=_body(name="b", allocation_pct=20))
    assert r.status_code == 400
    assert "CAD is 85% allocated" in r.json()["detail"]
    assert "at most 15%" in r.json()["detail"]


def test_update_goal_renames(app_env):
    client = _init(app_env)
    gid = client.post("/api/goals", json=_body()).json()["id"]
    r = client.put(f"/api/goals/{gid}", json=_body(name="safari"))
    assert r.status_code == 200, r.text
    assert client.get("/api/goals").json()[0]["name"] == "safari"


def test_update_unknown_goal_is_404(app_env):
    client = _init(app_env)
    r = client.put("/api/goals/999", json=_body())
    assert r.status_code == 404


def test_archive_and_unarchive_round_trip(app_env):
    client = _init(app_env)
    gid = client.post("/api/goals", json=_body()).json()["id"]

    assert client.post(f"/api/goals/{gid}/archive").status_code == 200
    assert client.get("/api/goals").json() == []

    archived = client.get("/api/goals", params={"include_archived": True}).json()
    assert [g["name"] for g in archived] == ["trip"]
    assert archived[0]["active"] is False

    assert client.post(f"/api/goals/{gid}/unarchive").status_code == 200
    assert len(client.get("/api/goals").json()) == 1


def test_archive_unknown_goal_is_404(app_env):
    client = _init(app_env)
    assert client.post("/api/goals/999/archive").status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src pytest tests/test_web_api.py -k goal -v`
Expected: FAIL — `known_currencies` KeyError, and `POST /api/goals` returns 405.

- [ ] **Step 3: Write minimal implementation**

In `src/bankapp/web/api.py`:

Add imports:

```python
from bankapp import goals as goalsmod
from bankapp import money
```

Extend `get_meta`:

```python
@router.get("/api/meta")
def get_meta(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return {
        "app_version": _app_version(),
        "known_currencies": list(money.known_currencies()),
        **filter_options(conn),
    }
```

Replace the existing `get_goals` route with:

```python
@router.get("/api/goals")
def get_goals(
    include_archived: bool = False, conn: sqlite3.Connection = Depends(get_conn)
) -> list:
    return [
        dataclasses.asdict(r)
        for r in advisor.goals_status(conn, include_archived=include_archived)
    ]
```

Add near the other Pydantic models:

```python
class GoalIn(BaseModel):
    name: str
    target: str                      # major units, e.g. "3000.00"; parsed by money.to_minor
    currency: str = "CAD"
    start_date: str
    target_date: Optional[str] = None
    allocation_pct: int = 100
    note: Optional[str] = None
```

Add the write routes at the bottom of the file:

```python
def _target_minor(body: "GoalIn") -> int:
    """Parse the major-unit target. Currency is gated first: money.exponent_for
    silently defaults an unknown code to 2 places, which would let a typo through."""
    if body.currency not in money.known_currencies():
        known = ", ".join(money.known_currencies())
        raise HTTPException(
            status_code=400,
            detail=f"unknown currency {body.currency!r}; known currencies are {known}",
        )
    try:
        return money.to_minor(body.target, body.currency)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _write(fn):
    """Run a goals write, mapping its error tree onto HTTP status codes."""
    try:
        return fn()
    except goalsmod.DuplicateName as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except goalsmod.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except goalsmod.GoalError as exc:  # ValidationError, AllocationError
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/goals")
def post_goal(body: GoalIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Create an active goal. The DB owns its values from here on; config.toml
    will not overwrite it."""
    minor = _target_minor(body)
    gid = _write(lambda: goalsmod.create(
        conn, name=body.name, target_minor=minor, currency=body.currency,
        start_date=body.start_date, target_date=body.target_date,
        allocation_pct=body.allocation_pct, note=body.note,
    ))
    return {"id": gid}


@router.put("/api/goals/{goal_id}")
def put_goal(goal_id: int, body: GoalIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Full replace, including rename. Keyed on id so the name can change."""
    minor = _target_minor(body)
    _write(lambda: goalsmod.update(
        conn, goal_id, name=body.name, target_minor=minor, currency=body.currency,
        start_date=body.start_date, target_date=body.target_date,
        allocation_pct=body.allocation_pct, note=body.note,
    ))
    return {"ok": True}


@router.post("/api/goals/{goal_id}/archive")
def post_goal_archive(goal_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Hide the goal; never deletes. Idempotent."""
    _write(lambda: goalsmod.archive(conn, goal_id))
    return {"ok": True}


@router.post("/api/goals/{goal_id}/unarchive")
def post_goal_unarchive(goal_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Restore the goal. Re-spends its allocation, so this can 400."""
    _write(lambda: goalsmod.unarchive(conn, goal_id))
    return {"ok": True}
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=src pytest tests/test_web_api.py -q`
Expected: PASS, including the pre-existing `test_meta_and_status_seeded`.

- [ ] **Step 5: Commit**

```bash
git add src/bankapp/web/api.py tests/test_web_api.py
git commit -m "feat(api): goal create/update/archive/unarchive routes + known_currencies"
```

---

## Task 8: Goals page UI

**Files:**
- Modify: `src/bankapp/web/static/goals.html`
- Test: `tests/test_web_static.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_static.py`:

```python
def test_goals_page_has_crud_ui(app_env):
    """The Goals page must ship the add/edit/archive entry points + modal wiring."""
    client = _client(app_env)
    html = client.get("/goals.html").text
    assert "new-goal" in html          # add button hook
    assert "openGoalModal" in html     # modal builder
    assert "goal-edit" in html         # per-row edit hook
    assert "goal-archive" in html      # per-row archive hook
    assert "include_archived" in html  # archived disclosure
```

`test_no_external_origins` already covers `/goals.html`; the modal must stay local-only.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src pytest tests/test_web_static.py -k goals_page -v`
Expected: FAIL — `assert "new-goal" in html`

- [ ] **Step 3: Write the implementation**

Replace the whole of `src/bankapp/web/static/goals.html`. Structure is illustrative;
`App.*` helper names are live-verified (`App.el`, `App.esc`, `App.empty`, `App.fmtMoney`,
`App.api`, `App.post`, `App.notice`, `App.nav`, `App.loadMeta`). Build fields with
`createElement` (the `transactions.html` modal precedent), never `innerHTML`, for
user-supplied values.

```html
<!doctype html>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>BankApp — Goals</title>
<link rel="stylesheet" href="/app.css" />
<script src="/vendor/chart.umd.js"></script>
<script src="/app.js"></script>

<main>
  <h1 class="page-title">Goals</h1>
  <p class="page-sub">Where the money you keep is headed.</p>

  <section class="block">
    <div class="block-head">
      <button class="btn" id="new-goal">＋ New goal</button>
    </div>
    <div class="panel" id="goals"></div>
  </section>

  <section class="block">
    <details id="archived-block">
      <summary>Archived</summary>
      <div class="panel" id="archived"></div>
    </details>
  </section>
</main>

<script>
  (async function () {
    await App.nav();
    await App.loadMeta();

    const CURRENCIES = (App.meta && App.meta.known_currencies) || ["CAD"];

    function paceBadge(g) {
      if (g.pace === "behind") return { cls: "pace", txt: "behind" };
      if (g.pace === "no_target") return { cls: "role", txt: "no target date" };
      return { cls: "ok", txt: "on track" };
    }

    function goalRow(g, archived) {
      const pct = Math.max(0, Math.min(100, g.pct_complete || 0));
      const badge = paceBadge(g);
      const row = document.createElement("div");
      row.className = "bar-row";

      const head = document.createElement("div");
      head.className = "bar-head";
      head.innerHTML =
        `<span class="cat">${App.esc(g.name)} ` +
        `<span class="badge ${badge.cls}">${badge.txt}</span> ` +
        `<span class="badge role">${g.allocation_pct}% alloc</span></span>` +
        `<span class="amt">${App.fmtMoney(g.funded_minor, g.currency)} / ` +
        `${App.fmtMoney(g.target_minor, g.currency)} · ${pct.toFixed(0)}%</span>`;
      row.appendChild(head);

      const track = document.createElement("div");
      track.className = "bar-track";
      const fill = document.createElement("div");
      fill.className = "bar-fill" + (g.pace === "behind" ? " pace" : "");
      fill.style.width = pct + "%";
      track.appendChild(fill);
      row.appendChild(track);

      const actions = document.createElement("div");
      actions.className = "modal-actions";
      if (archived) {
        const un = document.createElement("button");
        un.className = "btn ghost goal-unarchive";
        un.textContent = "Unarchive";
        un.onclick = async () => {
          await App.post(`/api/goals/${g.id}/unarchive`, {});
          App.notice(`Restored ${g.name}`);
          render();
        };
        actions.appendChild(un);
      } else {
        const edit = document.createElement("button");
        edit.className = "btn ghost goal-edit";
        edit.textContent = "Edit";
        edit.onclick = () => openGoalModal(g);
        const arch = document.createElement("button");
        arch.className = "btn ghost goal-archive";
        arch.textContent = "Archive";
        arch.onclick = async () => {
          await App.post(`/api/goals/${g.id}/archive`, {});
          App.notice(`Archived ${g.name}`);
          render();
        };
        actions.appendChild(edit);
        actions.appendChild(arch);
      }
      row.appendChild(actions);
      return row;
    }

    async function render() {
      const all = await App.api("/api/goals?include_archived=true");
      const active = all.filter((g) => g.active);
      const archived = all.filter((g) => !g.active);

      const wrap = App.el("goals");
      wrap.innerHTML = "";
      if (!active.length) {
        App.empty(wrap, "No goals yet. Use <b>＋ New goal</b> to add one.");
      } else {
        for (const g of active) wrap.appendChild(goalRow(g, false));
      }

      const arcWrap = App.el("archived");
      arcWrap.innerHTML = "";
      App.el("archived-block").style.display = archived.length ? "" : "none";
      App.el("archived-block").querySelector("summary").textContent =
        `Archived (${archived.length})`;
      for (const g of archived) arcWrap.appendChild(goalRow(g, true));
    }

    function field(card, label, input, hint) {
      const fld = document.createElement("div");
      fld.className = "fld";
      const lbl = document.createElement("label");
      lbl.textContent = label;
      fld.appendChild(lbl);
      fld.appendChild(input);
      if (hint) {
        const h = document.createElement("div");
        h.className = "hint";
        h.textContent = hint;
        fld.appendChild(h);
      }
      card.appendChild(fld);
      return input;
    }

    // `existing` is a goal object when editing, null when creating.
    function openGoalModal(existing) {
      const overlay = document.createElement("div");
      overlay.className = "modal";
      const card = document.createElement("div");
      card.className = "modal-card";
      overlay.appendChild(card);

      const title = document.createElement("h3");
      title.textContent = existing ? "Edit goal" : "New goal";
      card.appendChild(title);

      const name = document.createElement("input");
      name.value = existing ? existing.name : "";
      name.placeholder = "e.g. japan-trip";
      field(card, "Name", name);

      const target = document.createElement("input");
      target.value = existing
        ? (existing.target_minor / Math.pow(10, App.meta.currencies[existing.currency] ?? 2)).toFixed(2)
        : "";
      target.placeholder = "3000.00";
      field(card, "Target", target);

      const currency = document.createElement("select");
      for (const c of CURRENCIES) {
        const o = document.createElement("option");
        o.value = c;
        o.textContent = c;
        if (existing && existing.currency === c) o.selected = true;
        else if (!existing && c === "CAD") o.selected = true;
        currency.appendChild(o);
      }
      field(card, "Currency", currency);

      const start = document.createElement("input");
      start.type = "date";
      start.value = existing ? existing.start_date : new Date().toISOString().slice(0, 10);
      field(card, "Start date", start, "Progress counts net savings from this date.");

      const tgtDate = document.createElement("input");
      tgtDate.type = "date";
      tgtDate.value = existing && existing.target_date ? existing.target_date : "";
      field(card, "Target date (optional)", tgtDate, "Leave blank for no pace tracking.");

      const alloc = document.createElement("input");
      alloc.type = "number";
      alloc.min = "0";
      alloc.max = "100";
      alloc.value = existing ? existing.allocation_pct : "100";
      field(card, "Allocation %", alloc,
            "Share of that currency's monthly net savings credited to this goal.");

      const note = document.createElement("input");
      note.value = existing && existing.note ? existing.note : "";
      field(card, "Note (optional)", note);

      const actions = document.createElement("div");
      actions.className = "modal-actions";
      const cancel = document.createElement("button");
      cancel.className = "btn ghost";
      cancel.textContent = "Cancel";
      const save = document.createElement("button");
      save.className = "btn";
      save.textContent = "Save";
      actions.appendChild(cancel);
      actions.appendChild(save);
      card.appendChild(actions);

      const close = () => overlay.remove();
      cancel.onclick = close;
      overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });

      save.onclick = async () => {
        const body = {
          name: name.value,
          target: target.value,
          currency: currency.value,
          start_date: start.value,
          target_date: tgtDate.value || null,
          allocation_pct: Number(alloc.value),
          note: note.value || null,
        };
        save.disabled = true;
        try {
          // App.post surfaces the server's `detail` in the banner and rethrows,
          // so a rejected save leaves the modal open with the reason on screen.
          if (existing) await App.post(`/api/goals/${existing.id}`, body, "PUT");
          else await App.post("/api/goals", body);
          close();
          App.notice(existing ? `Updated ${body.name}` : `Added ${body.name}`);
          render();
        } catch (_) {
          save.disabled = false;
        }
      };

      document.body.appendChild(overlay);
      name.focus();
    }

    App.el("new-goal").onclick = () => openGoalModal(null);
    await render();
  })();
</script>
```

- [ ] **Step 4: Teach `App.post` an HTTP method**

`App.post` is hard-coded to `method: "POST"` (`app.js:26`). The edit path needs `PUT`.
Add an optional third argument, defaulting to `POST` so every existing caller is
unaffected:

```javascript
  App.post = async function (path, body, method) {
    let res;
    try {
      res = await fetch(path, {
        method: method || "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body || {}),
      });
    } catch (err) {
```

Leave the rest of the function untouched.

- [ ] **Step 5: Run tests**

Run: `PYTHONPATH=src pytest tests/test_web_static.py -q`
Expected: PASS, including `test_no_external_origins` and `test_app_js_has_post_helper`.

- [ ] **Step 6: Commit**

```bash
git add src/bankapp/web/static/goals.html src/bankapp/web/static/app.js tests/test_web_static.py
git commit -m "feat(web): add/edit/archive goals from the Goals page"
```

---

## Task 9: Documentation + full-suite verification

**Files:**
- Modify: `config.example.toml:52-57`
- Modify: `README.md` (goals section, if present)

- [ ] **Step 1: Update the config comment**

The `[[goals]]` block's semantics changed. Amend the comment at
`config.example.toml:52`:

```toml
[[goals]]                              # the "real joy" fund(s)
# Seeds a goal the first time `finance init` sees this name. After that the app owns
# it: add / edit / archive goals on the Goals page. Editing the block below will NOT
# change a goal that already exists.
name           = "example-trip"
target         = "3000.00"
start_date     = "2026-07-01"
target_date    = "2027-02-01"
allocation_pct = 100                   # share of THIS CURRENCY's monthly net savings
```

- [ ] **Step 2: Update README if it documents goal editing**

Run `grep -n -i goal README.md` and update any text claiming goals are config-only.

- [ ] **Step 3: Full suite, fresh shell**

Run: `PYTHONPATH=src python -m pytest -q`
Expected: all tests PASS, zero failures. Paste the real, untruncated summary line.

- [ ] **Step 4: Commit**

```bash
git add config.example.toml README.md
git commit -m "docs: goals are DB-owned; config.toml only seeds new names"
```

---

## Self-review

**Spec coverage.** Decision 1 (DB owns) → Task 5. Decision 2 (archive) → Tasks 4, 6, 8.
Decision 3 (per-currency cap) → Task 3, asserted in Tasks 4/5/7. `money.known_currencies`
→ Task 1. `goals.py` surface → Tasks 2–5. `advisor` changes → Tasks 5, 6. Five routes +
`/api/meta` → Task 7. `goals.html` → Task 8. Docs → Task 9. Every spec section maps to a
task.

**Spec deviations, deliberate.** (1) No `advisor.AllocationError` re-export — the spec's
regression contract was unachievable (six live call sites, not one import). (2) `App.post`
gains an optional `method` argument, unmentioned in the spec but required by `PUT`; it is
backward-compatible and Task 8 Step 4 covers it. (3) `unarchive` re-checks allocation —
implied by decision 3 but not spelled out in the spec; without it, unarchiving could push
a currency past 100%.

**Placeholder scan.** No TBD/TODO. Every code step carries the literal code. Every test
step carries the literal assertions.

**Type consistency.** `check_fields`, `check_name_free`, `check_allocation`,
`allocation_headroom`, `create`, `update`, `archive`, `unarchive`, `seed_from_config`,
`list_goals`, `get` — names identical in Tasks 2–5, in `api.py` (Task 7), and in
`cli.py` (Task 5). `GoalStatus` field order in Task 6 matches the kwargs used to build it.
`GoalIn.target` is a string in Task 7's model, its test body, and the `goals.html` payload.
Route paths in Task 7 match the `fetch` calls in Task 8.
