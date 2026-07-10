# In-app goal management (add / edit / archive)

Date: 2026-07-09
Status: approved design, not yet implemented

## Problem

Savings goals can only be created by hand-editing `[[goals]]` blocks in
`config.toml` and running `finance init`. There is no way to add, edit, or remove
a goal from the running app. `/api/goals` is a GET, `goals.html` renders
read-only progress bars, and `finance goals status` only prints.

Worse, `advisor.upsert_goals` **overwrites** `target_minor`, `currency`,
`start_date`, `target_date`, `allocation_pct`, and `note` for any goal whose
`name` matches a config block. Any in-app edit would be silently reverted by the
next `finance init`. Ownership has to be settled before write routes can exist.

## Decisions

1. **The database owns a goal's values. Config seeds.** `finance init` inserts a
   `[[goals]]` block only when no goal of that name exists; otherwise it leaves
   the row completely alone. Editing `config.toml` no longer affects a goal that
   already exists. This is a deliberate behavior change.

2. **Removing a goal archives it (`active = 0`); it never deletes.** `goals_status`
   and the digest already filter `active = 1`, so archiving needs no new filtering.
   Archiving also survives seeding: the row still exists, so insert-if-absent
   skips it. A hard delete would let a stale `[[goals]]` block resurrect the goal
   on the next `finance init`.

3. **Allocation is capped per currency, and breaches are rejected.** Active goals'
   `allocation_pct` must sum to <= 100 *within each currency*, because `_net_since`
   computes each goal's funded amount from its own currency's savings pool. The
   current global sum is a latent bug: a 100% CAD goal plus a 100% USD goal draw
   from two different pools yet today total 200% and are rejected. The CLI seed
   path gets the same per-currency rule so CLI and UI agree.

**No schema migration is required.** Archiving reuses the existing, currently
unused `goals.active` column. Because config no longer overwrites, no `source`
column is needed to distinguish config-owned from UI-owned goals.

## Components

### `money.py`

Add `known_currencies() -> tuple[str, ...]`, returning the keys of `_EXPONENTS`
(`CAD`, `USD`, `BTC`).

Rationale: `exponent_for` silently falls back to `_DEFAULT_EXPONENT = 2` for an
unrecognized currency. Without an allowlist, a typo'd currency creates a goal
whose `_net_since` matches zero rows and reports "0% funded" forever, with no
error anywhere. The goal form must select from a known list.

### `goals.py` (new)

Domain module. Imports `sqlite3` and `money` only — no FastAPI, so the CLI can
use it.

```
@dataclass(frozen=True)
class Goal:
    id, name, target_minor, currency, start_date,
    target_date, allocation_pct, note, active

list_goals(conn, include_archived=False) -> list[Goal]
get(conn, goal_id) -> Goal | None
create(conn, *, name, target_minor, currency, start_date,
       target_date, allocation_pct, note) -> int      # returns new id
update(conn, goal_id, *, <same fields>) -> None
archive(conn, goal_id) -> None                        # idempotent
unarchive(conn, goal_id) -> None                      # idempotent
allocation_headroom(conn, currency, exclude_id=None) -> int
check_fields(*, <fields>) -> None                     # raises ValidationError
check_name_free(conn, name, exclude_id=None) -> None  # raises DuplicateName
check_allocation(conn, currency, pct, exclude_id=None) -> None  # AllocationError
seed_from_config(conn, goals) -> int                  # insert-if-absent
```

Error tree, all deriving from `GoalError(ValueError)`:
`DuplicateName`, `AllocationError`, `ValidationError`, `NotFound`.

Validation is split into three checks rather than one `validate()`, because the
three writers need different subsets:

- `create` runs all three.
- `update` runs all three, passing `exclude_id` so the goal does not collide with
  its own stored name and allocation.
- `seed_from_config` runs `check_fields` per config goal, **skips
  `check_name_free`** (a name collision is the expected, correct case — it means
  the goal already exists and `ON CONFLICT DO NOTHING` leaves it alone), and runs
  `check_allocation` **once after all inserts**, over the resulting active set.

Sharing `check_fields` and `check_allocation` across all three writers is the
mechanism that prevents the CLI and the web UI from drifting into different
definitions of a legal goal.

### `advisor.py`

- `upsert_goals` is removed; its logic moves to `goals.seed_from_config` and
  changes from `ON CONFLICT DO UPDATE` to `ON CONFLICT DO NOTHING`.
- `AllocationError` is re-exported from `goals`, because
  `tests/test_advisor_goals_digest.py` imports it as `advisor.AllocationError`.
- `goals_status` stays (it is a report, not a mutation) and gains `id`,
  `start_date`, `target_date`, `note`, and `active` on `GoalStatus`. These
  additions are purely additive: no key is removed, so the advisor skill's digest
  JSON and `finance goals status` are unaffected.

### `web/api.py`

`get_meta` gains a `known_currencies` key (from `money.known_currencies()`) so the
goal modal's currency `<select>` has an allowlist to render. Then a `GoalIn`
Pydantic body plus five routes:

| Route | Purpose |
| --- | --- |
| `GET /api/goals?include_archived=false` | list (default: active only) |
| `POST /api/goals` | create, returns `{"id": n}` |
| `PUT /api/goals/{goal_id}` | full update, including rename |
| `POST /api/goals/{goal_id}/archive` | set `active = 0` |
| `POST /api/goals/{goal_id}/unarchive` | set `active = 1` |

Edits are keyed on `id`, not `name`, so a goal can be renamed. Archive is an
action-suffixed POST rather than HTTP `DELETE` because nothing is deleted —
matching the existing `POST /api/transactions/{id}/categorize` precedent.

Exception mapping: `DuplicateName` -> 409; `ValidationError` and
`AllocationError` -> 400; `NotFound` -> 404. The `detail` string is the
human-readable sentence shown to the user.

### `web/static/goals.html`

- A `＋ New goal` button opens a modal reusing the existing `.modal-card` /
  `.fld` CSS from `app.css` (precedent: the categorize modal in
  `transactions.html`).
- Each active goal row gains `Edit` and `Archive` actions.
- A collapsed `Archived (n)` disclosure lists archived goals with `Unarchive`.
- Modal fields: name, target (major units, text), currency (select, from
  `money.known_currencies()` surfaced via `/api/meta`), start date, target date
  (optional), allocation %, note.
- `App.post` already lifts the server's `detail` into the error banner and
  rethrows, so on a 400 the modal stays open with the reason visible.

### `cli.py`

`init` calls `goals.seed_from_config`; its output line becomes
`Goals: N seeded`. No `finance goals add/edit/rm` commands — the requirement was
an in-app path, and a CLI mutation surface is not needed yet.

## Validation rules

Run inside the same `with conn:` block as the write.

`check_fields` (no DB access):

- `name` is non-empty after stripping.
- `target_minor > 0`.
- `currency` is in `money.known_currencies()`.
- `start_date` is ISO `YYYY-MM-DD`.
- `target_date` is absent, or ISO and `>= start_date`. (`goals_status` computes
  `total_days = max(1, (target - start).days)`, so an inverted range would
  silently produce a nonsense pace rather than an error.)
- `0 <= allocation_pct <= 100`.

`check_name_free`: the name is unused by any other goal, archived included (the
column is `UNIQUE`), excluding `exclude_id`.

`check_allocation`: `pct <= allocation_headroom(conn, currency, exclude_id)`.
Excluding self is required so that editing a 100% goal down to 90% does not
collide with its own stored value.

The allocation error names the headroom, e.g.
`"CAD is 85% allocated; this goal can take at most 15%."`

## Data flow

**Create.** modal -> `App.post('/api/goals', body)` -> `GoalIn` -> `goals.create`
-> `check_fields` + `check_name_free` + `check_allocation` ->
`INSERT ... active = 1` -> page reloads the list.

**Seed.** `config.toml [[goals]]` -> `goals.seed_from_config` -> `check_fields`
per goal -> `INSERT ... ON CONFLICT(name) DO NOTHING` -> one per-currency
`check_allocation` over the resulting active set -> exit 1 on breach (as today).

**Funded math is unchanged**: `_net_since(start_date, currency) * allocation_pct`.

## Testing

New `tests/test_goals.py` (unit, in-memory DB):

- CRUD round-trip: create -> get -> update -> archive -> unarchive.
- Rejects: duplicate name; `target_minor <= 0`; unknown currency;
  `target_date < start_date`; `allocation_pct` outside 0..100.
- Allocation breach within a single currency is rejected.
- **CAD 100% + USD 100% are both accepted** — pins decision 3 and the
  cross-currency bug it fixes.
- Headroom excludes the goal under edit: a 100% goal can be edited to 90%.
- **`seed_from_config` run twice does not clobber a UI edit, does not resurrect
  an archived goal, and does not raise `DuplicateName`** — pins decisions 1 and
  2, and guards the trap that seeding an already-seeded name is the expected
  case, not an error.

`tests/test_web_api.py`: POST create then GET reflects it; PUT rename; 400 on
allocation breach with the `detail` sentence; 404 on unknown id; 409 on duplicate
name; `include_archived=true` returns archived rows.

`tests/test_web_static.py`: `goals.html` serves and contains the new-goal hook.

Existing `tests/test_advisor_goals_digest.py` and `tests/test_cli_advisor.py`
must pass unmodified. They are the regression contract that the
`AllocationError` re-export and the additive `GoalStatus` keys exist to preserve.

Note: running pytest from a worktree requires `PYTHONPATH` be set to the
worktree's `src/`.

## Scope

1 new module (`goals.py`), 1 new test file, 5 files edited (`money.py`,
`advisor.py`, `web/api.py`, `web/static/goals.html`, `cli.py`), 0 schema
migrations.

## Out of scope

- CLI mutation commands (`finance goals add/edit/rm`).
- Editing `config.toml` from the web app.
- Hard deletion of goals.
- Any change to how a goal's funded amount is computed.
