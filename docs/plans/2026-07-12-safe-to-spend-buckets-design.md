# Safe-to-spend buckets — design (2026-07-12)

Approved by Graham 2026-07-12 (brainstorm Q&A). Separates four money concepts:

| Bucket | Meaning | Mechanism |
|---|---|---|
| Need to spend | rent, subscriptions | existing `committed_remaining` (unchanged) |
| Need to save | fixed $/month, non-negotiable | goal with `funding_mode='fixed_monthly'` |
| Like to save | 3D printer, vacation | goal with `funding_mode='target_date'` (auto monthly ask) |
| Casual spending | guilt-free fun money | the safe-to-spend number itself, after ALL of the above |

Decisions locked in brainstorm:
1. **safe-to-spend = pure fun money** — both savings tiers are deducted before it.
2. **target goals auto-compute their ask** = ⌈remaining ÷ months-left⌉ (self-corrects as real funding lags).
3. **Over-commitment funds by priority and shows what got cut** — no silent proportional scaling.
4. Real-money progress tracking (`funded = allocation_pct × net-since-start`) is **untouched**.

## Data model

Three new `goals` columns, added BOTH to `schema.sql`'s CREATE TABLE (fresh DBs) and
`db.py:_COLUMN_MIGRATIONS` (existing DBs — the app's only migration mechanism):

```python
("goals", "funding_mode", "TEXT NOT NULL DEFAULT 'target_date'"),
("goals", "monthly_minor", "INTEGER"),                # NULL unless fixed_monthly
("goals", "priority", "INTEGER NOT NULL DEFAULT 100"),  # lower funds first
```

Validation (extend `goals.check_fields`, new kwargs `funding_mode="target_date"`,
`monthly_minor=None`, `priority=100`):
- `funding_mode` ∈ {`fixed_monthly`, `target_date`} else ValidationError.
- `fixed_monthly`: `monthly_minor` must be int > 0; `target_minor` must be int ≥ 0
  (0 = perpetual bucket, no progress %). `target_date` optional as before.
- `target_date` mode: `monthly_minor` must be None; `target_minor` int > 0 (as today).
- `priority`: int in [0, 999].
- `_is_int` bool-exclusion applies to the new int fields.

Threading: `Goal` dataclass + `_COLS` + `_to_goal` + `create`/`update` kwargs + INSERT/UPDATE SQL
+ `seed_from_config`. `config.GoalConfig` gains `funding_mode` (default `"target_date"`),
`monthly_minor` (from TOML key `monthly`, major-unit string via `money.to_minor`, default None),
`priority` (default 100). `config.example.toml` gains a commented `fixed_monthly` example block.

## Monthly ask (pure function, `goals.monthly_ask`)

```python
def monthly_ask(*, funding_mode: str, monthly_minor: Optional[int], target_minor: int,
                funded_minor: int, target_date: Optional[str], today: date) -> int:
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
```

`months_left` counts the current month as one (target this month → 1 → ask = whole remaining);
a past target clamps to 1 (ask everything; pace already reads "behind").

## Waterfall (`projection.month_projection`)

After the existing `committed` computation, per currency:

```
available = expected_income_minor - spent_so_far_minor - committed      # may be < 0
pool = max(0, available)
queue = active goals of this currency with ask > 0, ordered:
        fixed_monthly tier first, then target_date tier;
        within each tier (priority ASC, name ASC)
for goal in queue:
    allocated = min(ask, pool); pool -= allocated
    status = 'funded' if allocated == ask else ('partial' if allocated > 0 else 'starved')
safe_to_spend_minor = pool
savings_shortfall_minor = Σask - Σallocated
```

Goal data comes from `advisor.goals_status(conn, today)` (which now computes `monthly_ask_minor`
per goal — see below), filtered to active + this currency. Goal currencies join the `currencies`
set like templates/subs do, so a USD-only goal still yields a USD row.

New `ProjectionRow` fields (all additive — existing tests attribute-access only):
- `need_to_save_minor` (Σ asks, fixed tier) · `like_to_save_minor` (Σ asks, target tier)
- `savings_allocated_minor` · `savings_shortfall_minor`
- `goal_funding: list[GoalFunding]` where `GoalFunding(goal_id, name, funding_mode, priority,
  ask_minor, allocated_minor, status)` — frozen dataclass; `dataclasses.asdict` recurses, so the
  digest's `"projection"` embedding needs no change.

Invariant kept: `safe_to_spend_minor == max(0, expected_income − spent − committed − savings_allocated)`.

## GoalStatus / digest / API

`advisor.GoalStatus` gains `funding_mode`, `priority`, `monthly_minor`, `monthly_ask_minor`
(computed via `goals.monthly_ask` using the goal's own `funded_minor`). Pace logic unchanged
(fixed goals without target_date read `no_target`). Digest's hand-picked `"goals"` subset dict
adds `funding_mode`, `priority`, `monthly_ask_minor`. **No new top-level digest key**
(`test_digest_json_keys_stable` is set-equality).

`web/api.py GoalIn` gains `funding_mode: str = "target_date"`, `monthly: Optional[str] = None`
(major units, parsed like `target` with the currency gate), `priority: int = 100`;
`target` becomes `Optional[str] = None` — required for target_date mode (400 otherwise),
defaults to 0 for fixed_monthly. Existing `_write` exception→HTTP mapping covers the new
ValidationErrors. `/api/goals` returns full `asdict(GoalStatus)` so new fields flow automatically.

## CLI

`finance report projection`: keep the four pinned labels (`expected income`, `spent so far`,
`committed remaining`, `safe to spend`) and insert between committed and safe:
`need to save`, `like to save`, plus a trailing warning line only when shortfall > 0:
`plan short by <amt> — lowest-priority goals cut`. `finance goals status`: append
mode/ask per row additively (keep existing columns; check `tests/test_cli_advisor.py` pins).

## UI

**Dashboard** (`index.html` sts-cards): per-currency `.panel.card` keeps `.label` and hero
`.value` (fun money), replaces the one-line `.sub` with a waterfall list (new `.wf-row` CSS:
flex space-between, muted label left, tabular amount right):
income (+) / spent so far (−) / bills & subs (−) / need to save (−) / like to save (−).
When `savings_shortfall_minor > 0`: `.badge.over` "plan short by $X" + a `.chipset` of
partial/starved goal chips (`.badge.pace` partial, `.badge.over` starved). Zero-ask rows
(no goals) render without the two savings lines rather than showing −0.

**Goals page** (`goals.html`): modal gains funding-mode radio group (`.radios`, exists unused):
"Save toward a target" / "Fixed monthly amount"; monthly-amount field shown only for fixed;
target field hint switches ("optional for fixed buckets"); priority number input
(hint: "Lower = funded first when money is tight"). Rows gain a `.badge.role` mode/ask chip
(`$500/mo` fixed · `$150/mo ask` target) and a this-month funding chip (funded `.badge.ok` /
partial `.badge.pace` / starved `.badge.over`) sourced from one extra `GET /api/projection`
(match by `goal_id`). Fixed goals with target 0 render no progress bar. Existing progress
bars, pace badges, archive flow untouched.

## Test-coupling constraints (from repo audit)

- NO new top-level digest key (set-equality test) — extend projection/goals *entries* only.
- Keep CLI labels `expected income` / `spent so far` / `committed` / `safe to spend`.
- Keep `pace`/`name` keys in digest goals entries (`_changes_since_brief` reads them).
- `tests/test_goals.py::_raw_insert` hardcodes an 8-column INSERT — new columns carry defaults
  so it keeps working.
- Worktree testing: `PYTHONPATH="$PWD/src" ~/BankApp/.venv/bin/python -m pytest` (editable
  install points at main checkout). Dev server: `--port 8399`, never 8377 (live app).

## Slices

1. Domain + schema (Standard) — this doc §Data model + §Monthly ask.
2. Waterfall + digest contract (Full; pinning test FIRST) — §Waterfall + §GoalStatus.
3. API + CLI (Standard) — §GoalStatus/API + §CLI.
4. Dashboard UI (Standard) — §UI dashboard.
5. Goals page UI (Standard) — §UI goals page.
