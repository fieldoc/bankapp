# Personal Finance Sync & Categorization Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Also load `plan-grounding-extras` when executing.

**Goal:** Local-first personal-finance system with two layers: (1) a data engine that ingests transactions from TD Canada Trust (automated daily via Plaid free Trial tier, plus OFX/QFX/CSV file drop for backfill/fallback) and Wealthsimple (`ws-api`), stores them immutably in SQLite, and layers revisable interpretation on top (categories, transfer links, split-expense groups with receivables); and (2) an advisor layer on that data — net worth from balance snapshots, monthly savings rate, budgets, subscription/leak detection, savings goals, and a Claude Code advisor skill (subscription-billed) that coaches toward the mission: **frugally luxurious** — catch money slipping away unnoticed so it can fund things that bring real joy.

**Architecture:** Immutable `raw_txn` ledger enforced by SQLite triggers; all interpretation (categories, groups) lives in separate revisable tables. Rules-first categorization; unknowns queue for a Claude Code skill (subscription-billed — **zero Anthropic API usage, no ANTHROPIC_API_KEY anywhere**). Deterministic, idempotent, re-runnable matching for transfers and splits.

**Tech Stack:** Python ≥3.10, SQLite (stdlib `sqlite3`), `ofxtools==1.1.1`, `ws-api==0.35.0` (pinned; alpha), `keyring`, `typer`, `pytest`. TOML config via `tomllib`/`tomli`.

---

## Context

Graham wants his TD (chequing + Visa) and Wealthsimple activity in one queryable local store, categorized, with cross-account noise handled correctly: internal TD↔WS transfers netted out (both legs kept), and the monthly rent chain (roommate e-transfer → TD → WS → landlord) modeled as one split-expense group so his true spend = his 50% share and late/short roommate payments get flagged, not lost. Core inviolable principle from the handoff spec: **bank lines are immutable truth; interpretation is a revisable layer on top; every classifier is idempotently re-runnable without corrupting balances.**

Expanded scope (2026-07-05): the pipeline is the data engine for a **personal financial advisor** with constant access to spending. Overall goal: **frugally luxurious** — surface the money that slips away unnoticed (subscriptions, fees, small frequent purchases, price creep) so it can be deliberately redirected into savings goals for things that bring real joy. Concretely that adds: real net worth across all accounts (assets − Visa liability, investments included), am-I-actually-saving tracking (monthly income/spend/net + savings rate), per-category budgets, recurring-charge and leak detection, named savings goals with progress, a one-shot digest, and an advisor skill. **Advisor boundary: spending/budget coaching only — no investment advice (not a licensed advisor; system is read-only by design).**

## Current-state reality (READ THIS FIRST)

- `/Users/grahammetcalfe/BankApp` is **EMPTY** (verified 2026-07-05). Not a git repo yet. Everything below is **NET-NEW → build**. No rebuild risk, no existing patterns to follow.
- Host Python is 3.14; target `requires-python = ">=3.10"`.

### Fresh research findings that shaped this plan (verified mid-2026)
- **TD Canada Trust has NO OFX Direct Connect server** → `ofxget` automation is impossible; dropped entirely.
- **No ws-api equivalent exists for TD**: TD's API surface has never been publicly reverse-engineered; the only community project (jfdoming/td-scraper) is a headless-browser EasyWeb scraper — lockout risk, ToS violation, ruled out. Canada's open-banking framework (Bill C-69) is **not live** and slipping (Bank of Canada, March 2026); when Phase 1 lands (~2027) it becomes a clean new adapter.
- **Plaid is the automated TD route**: TD has a signed data-access agreement with Plaid (token-based, revocable, no stored EasyWeb credentials); Plaid's **free Trial plan (accounts created ≥ April 2026) allows up to 10 production Items at no cost** — this project needs 1. Cursor-based `/transactions/sync` gives idempotent daily pulls with stable transaction ids, merchant enrichment, pending status, ~24 months history on first connect, for both chequing and Visa. **Gated by spike TP.0** (verify TD CA availability on the Trial tier with a real link before building).
- **TD Visa has no CSV export** — only OFX/QFX. Chequing has CSV (headerless, believed columns: Date MM/DD/YYYY, Description, Withdrawal, Deposit, Balance — **BELIEVED, NOT VERIFIED**; see gate T2.0) plus OFX/QFX.
- OFX/QFX carries `FITID` (stable txn id) → use it for dedup; CSV has no ids → content hash.
- `ofxtools` 1.1.1: `OFXTree().parse(path)` → `.convert()` → `ofx.statements[0].transactions`; txn fields `.dtposted` (datetime), `.trnamt` (Decimal), `.fitid`, `.name`/`.memo`. Banks emit malformed SGML sometimes → quarantine, don't crash.
- `ws-api` 0.35.0 (2026-06-01): `WealthsimpleAPI.login(username, password)` (interactive TOTP) and `WealthsimpleAPI.from_token(token)` confirmed; **exact `get_accounts()`/`get_activities()` signatures, field names, pagination, and token-refresh persistence are UNVERIFIED** → hard gate T3.1 probes the installed source before any adapter code. Every ws-api snippet in this plan is `illustrative; verify against installed source`.

### User decisions (2026-07-05)
- Roommate split **50/50**, reimbursement identified by **Interac e-transfer sender-name pattern** — pattern lives in **config, not code**.
- Host machine: **likely a Windows PC at home** (development happens on this Mac) → **fully cross-platform, Windows-first for deployment**. Missed scheduled runs are fine — the schedule must use catch-up semantics (run when the machine comes back online), which Windows Task Scheduler's `StartWhenAvailable` provides.
- TD (revised 2026-07-05, superseding the spec's "no aggregators for now"): priorities re-stated as **#1 security, #2 near-live automation (daily at the longest), #3 data breadth** → **Plaid direct (free Trial tier) is the automated TD route**; the **file adapter stays** as historical backfill, fallback during re-auth gaps, and the fixture path for acceptance tests. Manual-only fails priority #2; scraping fails #1.
- **Categorization via Claude subscription, NOT the API**: pipeline runs fully with no Anthropic credentials; unknowns accumulate in a review queue; a repo-local Claude Code skill (or headless `claude -p`, subscription-billed) reads the queue and writes verdicts back **only through the CLI** (`finance rules add`).

## Key design decisions

| Decision | Choice | Why |
|---|---|---|
| Money | **INTEGER minor units** (signed `amount_minor`; per-currency exponent map in code: CAD/USD=2, BTC=8, default 2) | Exact, sortable, native SQL `SUM()`. Parse with `decimal.Decimal` at the boundary only. Float is banned. |
| Immutability | SQLite `BEFORE UPDATE/DELETE` triggers on `raw_txn` that `RAISE(ABORT)` | The core principle enforced in the engine, not by convention. |
| Double-count prevention | `UNIQUE(raw_txn_id)` in `group_members` | One txn ∈ at most one group, structurally. |
| `merchant_category` table | **Folded into `rules`** (deliberate deviation from spec's starting schema) | A persisted Claude/manual verdict IS the learn-once cache; two tables answering "pattern → category" is one too many. |
| Pending txns | Skipped at ingest (`status` column kept for future) | Immutability forbids flipping pending→posted in place; TD exports are posted-only anyway. |
| CLI | `typer` (pinned) | Many subcommands; `CliRunner` gives clean CLI tests. |
| Config | TOML at `$FINANCE_CONFIG` > `%APPDATA%\bankapp\config.toml` (Windows) / `~/.config/bankapp/config.toml` (else); DB at `$FINANCE_DB` > config `db_path`; all paths `~`-expanded via `pathlib` | Dev on Mac, deploy on Windows — one codebase, OS-appropriate defaults. |
| Windows portability | `pathlib.Path` everywhere (no string path joins), `keyring` → Windows Credential Manager backend automatically, no POSIX-only APIs (no fork/signal), plain-ASCII console output (legacy Windows consoles choke on Unicode symbols), venv scripts at `.venv\Scripts\` on Windows | The deploy target is Windows; the dev machine is macOS. Both must pass the suite. |
| Multi-currency | Store `currency` per txn; **never convert** | YAGNI per spec. |
| Timezone | Dates stored as `YYYY-MM-DD` local to `America/Vancouver` (via `zoneinfo`) | Per spec. |

## File structure

```
BankApp/
├── pyproject.toml                  # pinned deps; [project.scripts] finance = "bankapp.cli:app"
├── config.example.toml             # user copies to ~/.config/bankapp/config.toml
├── .gitignore                      # *.db, *.db-journal, *.db-wal, inbox/, exports/, quarantine/, .env
├── .claude/skills/categorize/SKILL.md   # Claude subscription categorization skill
├── .claude/skills/advisor/SKILL.md      # Claude subscription advisor skill (digest → coaching)
├── docs/scheduling.md              # launchd + cron examples
├── docs/ws-api-notes.md            # written during T3.1 probe
├── src/bankapp/
│   ├── cli.py                      # typer app; thin, no business logic; THE sole write path
│   ├── config.py                   # TOML load/validate, env overrides, ~ expansion
│   ├── db.py                       # connect (foreign_keys ON), schema apply, meta.schema_version
│   ├── schema.sql                  # full DDL: tables, immutability triggers, views
│   ├── money.py                    # Decimal↔minor units, exponent map, share_split
│   ├── normalize.py                # norm_desc, content_dedup_key, occurrence counter
│   ├── ingest/core.py              # NormalizedTxn, insert_batch (INSERT OR IGNORE), import_log
│   ├── ingest/ofx.py               # ofxtools adapter; malformed → quarantine/
│   ├── ingest/csv_td.py            # TD chequing headerless CSV
│   ├── ingest/ws.py                # ws-api adapter + keyring auth (gated by T3.1 probe)
│   ├── ingest/plaid_td.py          # Plaid /transactions/sync adapter for TD (gated by TP.0 spike)
│   ├── classify/engine.py          # rule matching (substring/regex, priority)
│   ├── classify/review.py          # review queue query + JSON/markdown export
│   ├── match/transfers.py          # pure pairing fn + persistence + rebuild
│   ├── match/splits.py             # template periods, 3-leg rent chain, statuses
│   ├── report/analytics.py         # spend report + status dashboard
│   └── report/advisor.py           # net worth, cashflow/savings, subscriptions, leaks, budgets, goals, digest
└── tests/                          # conftest, per-module tests, test_acceptance.py (AT1–AT3)
    └── fixtures/                   # 100% SYNTHETIC (no real bank data ever)
```

## Load-bearing designs (exact)

### Schema (`src/bankapp/schema.sql`) — the contract everything builds on

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- rows: ('schema_version','1'), ('hash_version','1')

CREATE TABLE accounts (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,            -- config key, e.g. 'td-chequing'
  institution TEXT NOT NULL,           -- 'td' | 'wealthsimple'
  type TEXT NOT NULL CHECK (type IN ('chequing','savings','visa','cash','investment','crypto')),
  currency TEXT NOT NULL DEFAULT 'CAD',
  external_id TEXT                     -- OFX ACCTID or WS account id
);

CREATE TABLE raw_txn (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  posted_date TEXT NOT NULL,           -- 'YYYY-MM-DD' America/Vancouver local
  amount_minor INTEGER NOT NULL,       -- signed minor units
  currency TEXT NOT NULL,
  description_raw TEXT NOT NULL,
  description_norm TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('pending','posted')),
  dedup_key TEXT NOT NULL,             -- 'fitid:...' | 'wsid:...' | 'sha256:...'
  source TEXT NOT NULL,                -- 'ofx' | 'csv' | 'ws'
  imported_at TEXT NOT NULL,           -- ISO-8601 UTC
  UNIQUE (account_id, dedup_key)
);
CREATE INDEX idx_raw_txn_acct_date ON raw_txn(account_id, posted_date);

-- IMMUTABILITY: the core principle, enforced in the engine
CREATE TRIGGER raw_txn_no_update BEFORE UPDATE ON raw_txn
BEGIN SELECT RAISE(ABORT, 'raw_txn is immutable'); END;
CREATE TRIGGER raw_txn_no_delete BEFORE DELETE ON raw_txn
BEGIN SELECT RAISE(ABORT, 'raw_txn is immutable'); END;

CREATE TABLE import_log (
  id INTEGER PRIMARY KEY, filename TEXT NOT NULL, file_sha256 TEXT NOT NULL UNIQUE,
  imported_at TEXT NOT NULL, rows_inserted INTEGER NOT NULL, rows_skipped INTEGER NOT NULL
);

CREATE TABLE rules (
  id INTEGER PRIMARY KEY,
  match_kind TEXT NOT NULL CHECK (match_kind IN ('substring','regex')),
  pattern TEXT NOT NULL,               -- matched against description_norm (lowercase)
  category TEXT,
  role_hint TEXT CHECK (role_hint IN ('transfer','reimbursement','expense','income') OR role_hint IS NULL),
  counterparty TEXT,
  priority INTEGER NOT NULL DEFAULT 100,   -- lower wins
  source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','claude','seed')),
  created_at TEXT NOT NULL,
  UNIQUE (match_kind, pattern)
);

-- Interpretation layer: revisable, never touches raw_txn
CREATE TABLE txn_interp (
  raw_txn_id INTEGER PRIMARY KEY REFERENCES raw_txn(id),
  category TEXT, role_hint TEXT, counterparty TEXT,
  rule_id INTEGER REFERENCES rules(id),
  updated_at TEXT NOT NULL
);

CREATE TABLE recurring_templates (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,           -- upserted from config by name
  kind TEXT NOT NULL CHECK (kind IN ('split_expense')),
  expected_amount_minor INTEGER NOT NULL,   -- FULL expense (e.g. total rent, 2X)
  currency TEXT NOT NULL DEFAULT 'CAD',
  cadence TEXT NOT NULL DEFAULT 'monthly',
  share_numer INTEGER NOT NULL, share_denom INTEGER NOT NULL,  -- my share (50/50 → 1/2)
  expense_account TEXT NOT NULL,       -- accounts.key
  expense_pattern TEXT NOT NULL,       -- substring on description_norm
  reimburse_account TEXT NOT NULL,
  reimburser_pattern TEXT NOT NULL,    -- Interac SENDER-NAME pattern (config, not code)
  amount_tolerance_minor INTEGER NOT NULL DEFAULT 500,
  day_of_month INTEGER NOT NULL DEFAULT 1,
  window_days INTEGER NOT NULL DEFAULT 45,  -- reimbursement due window
  link_transfer INTEGER NOT NULL DEFAULT 1, -- claim TD→WS legs into this group
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE groups (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL CHECK (type IN ('transfer','split_expense')),
  status TEXT NOT NULL,
    -- transfer: 'matched'
    -- split_expense: 'open'|'settled'|'underpaid'|'amount_anomaly'|'missing_expense'
  template_id INTEGER REFERENCES recurring_templates(id),
  period_key TEXT,                     -- 'YYYY-MM' for template groups
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (template_id, period_key)
);

CREATE TABLE group_members (
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  raw_txn_id INTEGER NOT NULL REFERENCES raw_txn(id),
  role TEXT NOT NULL CHECK (role IN ('expense','income','transfer_out','transfer_in','reimbursement')),
  share_amount_minor INTEGER,          -- positive; only on 'expense' rows (my share)
  PRIMARY KEY (group_id, raw_txn_id),
  UNIQUE (raw_txn_id)                  -- one group per txn ⇒ structurally no double-count
);

-- Analytics: transfers net to 0, reimbursements 0, split expense counts MY SHARE only,
-- lone hinted-transfer legs excluded (pending, not lost)
CREATE VIEW v_effective AS
SELECT r.id, r.account_id, r.posted_date, r.currency, r.amount_minor, r.description_norm,
  i.category, gm.role AS group_role, g.type AS group_type,
  CASE
    WHEN gm.role IN ('transfer_in','transfer_out') THEN 0
    WHEN gm.role = 'reimbursement' THEN 0
    WHEN gm.role = 'expense' AND gm.share_amount_minor IS NOT NULL THEN -gm.share_amount_minor
    WHEN gm.raw_txn_id IS NULL AND i.role_hint = 'transfer' THEN 0
    ELSE r.amount_minor
  END AS effective_minor
FROM raw_txn r
LEFT JOIN txn_interp i ON i.raw_txn_id = r.id
LEFT JOIN group_members gm ON gm.raw_txn_id = r.id
LEFT JOIN groups g ON g.id = gm.group_id;

-- Lone transfer legs: NORMAL (TD weekly batches vs WS realtime), surfaced with age, not errored
CREATE VIEW v_pending_transfers AS
SELECT r.id, r.account_id, r.posted_date, r.amount_minor, r.description_norm,
       CAST(julianday('now') - julianday(r.posted_date) AS INTEGER) AS age_days
FROM raw_txn r
JOIN txn_interp i ON i.raw_txn_id = r.id AND i.role_hint = 'transfer'
LEFT JOIN group_members gm ON gm.raw_txn_id = r.id
WHERE gm.raw_txn_id IS NULL;

-- ADVISOR-LAYER TABLES ------------------------------------------------------

-- Append-only balance snapshots captured on every sync (WS accounts, Plaid balances,
-- OFX <LEDGERBAL> when file-dropped). Liabilities (visa) stored NEGATIVE.
CREATE TABLE balance_snapshot (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  as_of TEXT NOT NULL,                 -- 'YYYY-MM-DD'
  balance_minor INTEGER NOT NULL,      -- signed; visa owed = negative
  currency TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('ws','plaid','ofx','manual')),
  captured_at TEXT NOT NULL,
  UNIQUE (account_id, as_of, source)   -- one snapshot per account per day per source
);

CREATE TABLE budgets (                 -- upserted from config [budgets] by category
  id INTEGER PRIMARY KEY,
  category TEXT NOT NULL UNIQUE,
  monthly_limit_minor INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CAD',
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE goals (                   -- upserted from config [[goals]] by name
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  target_minor INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CAD',
  start_date TEXT NOT NULL,            -- progress = net savings since this date × allocation
  target_date TEXT,
  allocation_pct INTEGER NOT NULL DEFAULT 100,
  note TEXT,
  active INTEGER NOT NULL DEFAULT 1
);

-- Net worth: latest snapshot per account (prefer freshest as_of, any source), summed per currency
CREATE VIEW v_net_worth AS
SELECT b.currency, SUM(b.balance_minor) AS net_worth_minor, MAX(b.as_of) AS freshest_as_of
FROM balance_snapshot b
JOIN (SELECT account_id, MAX(as_of) AS as_of FROM balance_snapshot GROUP BY account_id) latest
  ON latest.account_id = b.account_id AND latest.as_of = b.as_of
GROUP BY b.currency;

-- Am I saving? Monthly income/spend/net from effective amounts (transfers already netted,
-- rent already reduced to my share). savings_rate = net/income, computed in Python.
CREATE VIEW v_monthly_cashflow AS
SELECT substr(posted_date, 1, 7) AS month, currency,
  SUM(CASE WHEN effective_minor > 0 THEN effective_minor ELSE 0 END) AS income_minor,
  SUM(CASE WHEN effective_minor < 0 THEN -effective_minor ELSE 0 END) AS spend_minor,
  SUM(effective_minor) AS net_minor
FROM v_effective
GROUP BY month, currency;

-- Receivables (AR-lite) with aging: expected = roommate's share = |expense| − my share
CREATE VIEW v_receivables AS
SELECT g.id AS group_id, t.name AS template, g.period_key, g.status,
  exp.expense_minor,
  (ABS(exp.expense_minor) - exp.share_minor) AS expected_minor,
  COALESCE(reimb.received_minor, 0) AS received_minor,
  (ABS(exp.expense_minor) - exp.share_minor) - COALESCE(reimb.received_minor, 0) AS outstanding_minor,
  CAST(julianday('now') - julianday(exp.expense_date) AS INTEGER) AS age_days
FROM groups g
JOIN recurring_templates t ON t.id = g.template_id
LEFT JOIN (SELECT gm.group_id, r.amount_minor AS expense_minor,
                  gm.share_amount_minor AS share_minor, r.posted_date AS expense_date
           FROM group_members gm JOIN raw_txn r ON r.id = gm.raw_txn_id
           WHERE gm.role = 'expense') exp ON exp.group_id = g.id
LEFT JOIN (SELECT gm.group_id, SUM(ABS(r.amount_minor)) AS received_minor
           FROM group_members gm JOIN raw_txn r ON r.id = gm.raw_txn_id
           WHERE gm.role = 'reimbursement' GROUP BY gm.group_id) reimb ON reimb.group_id = g.id
WHERE g.type = 'split_expense';
```

(All `CREATE` statements get `IF NOT EXISTS` in the real file so schema apply is idempotent.)

### Dedup-key recipe (`src/bankapp/normalize.py`)

Preference order per row: **FITID** (`fitid:<FITID>`, OFX/QFX) → **WS activity id** (`wsid:<id>`) → **content hash** (TD CSV, no ids):

```python
HASH_VERSION = "1"   # frozen once real data lands; changing norm_desc requires a bump + migration (out of scope)

def norm_desc(raw: str) -> str:
    # Frozen normalization used INSIDE the hash: lowercase + collapse whitespace only.
    # Richer merchant normalization for categorization may evolve; this must not.
    return " ".join(raw.split()).lower()

def content_dedup_key(account_key, posted_date, amount_minor, currency, desc_norm, occurrence) -> str:
    payload = "|".join([HASH_VERSION, account_key, posted_date,
                        str(amount_minor), currency, desc_norm, str(occurrence)])
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

def assign_occurrences(txns):   # txns in FILE ORDER, one import batch
    # Same-day-identical-txn disambiguation. Counter scoped to the FILE, never the DB
    # (DB-scoped counting would re-insert on re-run).
    seen = Counter()
    for t in txns:
        key = (t.account_key, t.posted_date, t.amount_minor, t.desc_norm)
        t.occurrence = seen[key]; seen[key] += 1
```

**Why stable across overlapping export windows:** TD exports are whole-day granular, so any window covering a day contains ALL of that day's rows; identical same-day txns always appear together and get occurrences 0,1 in every window → identical hashes → idempotent. Insert via `INSERT OR IGNORE` against `UNIQUE(account_id, dedup_key)`. README documents: "always export whole days."

### Transfer matching (`src/bankapp/match/transfers.py`)

**Rules-gated:** only txns with `txn_interp.role_hint='transfer'` enter (seed rules from config tag TD `TFR-TO/TFR-FR/EFT` and WS deposit/withdrawal descriptions) — prevents an unrelated $100 inflow pairing with a $100 purchase.

```
candidates = hinted txns NOT in any group
pairs = all (out, in) with different accounts,
        |amount diff| <= tolerance_minor,            # fee tolerance, default 0
        |date diff| <= window_days (EITHER order —   # default 7
          TD batch lag means the out can post after the in)
sort by (date_diff, amount_diff, out.id, in.id)      # deterministic tie-break
greedy one-to-one; each pair → groups(type='transfer', status='matched') + two members
```

- **`pending_unmatched` is derived, not stored**: a lone hinted leg = "not in a group" = `v_pending_transfers` (with age; warn styling past 2×window). Already excluded from spend via `v_effective`. When the counterpart lands days later, the next run pairs it. No state machine to corrupt.
- **Idempotent**: only adds groups over never-grouped legs; re-run is a no-op. `--rebuild` deletes non-template `type='transfer'` groups and rematches deterministically (interpretation layer — deletable by design).
- **Ordering**: `finance match splits` runs BEFORE `match transfers` (splits claim their transfer legs; `UNIQUE(raw_txn_id)` keeps the generic matcher off them). `finance match all` encodes this.

### Split / receivables — the 3-leg rent chain (`src/bankapp/match/splits.py`)

Templates defined in config, upserted by `name`. Per active template, per period (first-txn month → now), each run:

1. **Ensure group** for `(template_id, period_key)` once the period's expected date passes (lazy; UNIQUE makes it idempotent).
2. **Expense leg**: ungrouped txn in `expense_account` matching `expense_pattern` near `day_of_month`, amount within tolerance → attach role `expense`, `share_amount_minor = (abs(amount) * share_numer) // share_denom` (floor; odd cent lands on the receivable — deterministic). Amount out of tolerance → **attach anyway, status `amount_anomaly`** ("rent changed" flagged, not lost).
3. **Transfer legs** (`link_transfer=1`): ungrouped opposite-sign TD-out/WS-in pair ≈ full expense within the period window → attach `transfer_out`/`transfer_in`. One group ties all 4 raw rows of the 3 logical events. If absent, generic matcher pairs them later — spend math identical either way.
4. **Reimbursement legs (AR-lite)**: ungrouped inflows in `reimburse_account` matching `reimburser_pattern`, window `[expense_date − 14d, expense_date + window_days]`, **FIFO to the oldest unsettled period across month boundaries** — a late January payment arriving in February settles January; never lost, never double-counted.
5. **Status recompute every run** (derived interpretation): `settled` (received ≥ expected − tolerance) | `open` (in window) | `underpaid` (past window, short) | `missing_expense` (period elapsed + grace, no expense) | `amount_anomaly`.

Net in `v_effective`: expense → −my_share, reimbursement → 0, transfers → 0 ⇒ **month spend = my share exactly** (AT3).

### CLI (`finance`, typer) — the sole write path

```
finance init                                  # create db, apply schema, upsert templates + seed rules
finance accounts list
finance ingest <path>... [--account KEY]      # files or dir; .ofx/.qfx auto-map via ACCTID; .csv requires --account
finance ws login                              # interactive TOTP; token → OS keyring (service 'bankapp')
finance sync ws                               # fetch WS activities; degrade gracefully per-activity
finance plaid link                            # one-time: Link flow for TD; access_token → keyring
finance sync plaid                            # cursor-based /transactions/sync; posted txns → raw_txn
finance categorize [--all]                    # rules-first; idempotent
finance rules add --kind substring --pattern "netflix" --category subscriptions [--role transfer] [--source claude]
finance rules list
finance review export [--format json|markdown] [--out PATH]
finance review count
finance match splits|transfers|all [--rebuild]
finance status                                # uncategorized, pending transfers w/ age, receivables aging, last sync
finance report spend --month YYYY-MM [--by category]
finance report networth [--history]           # latest snapshots summed (per currency); --history = month-end series
finance report savings [--months N]           # income/spend/net + savings rate per month, trend
finance report subscriptions                  # recurring charges: cadence, monthly cost, price-creep flags
finance report leaks [--threshold 15.00]      # small frequent spends + fees, aggregated per merchant/month
finance budget status [--month YYYY-MM]       # per-category actual vs limit, month-pace warnings
finance goals status                          # per-goal: target, funded (net savings since start × allocation), pace vs target_date
finance digest [--format json|markdown]       # one-shot advisor bundle: all of the above + receivables + data-quality notes
finance refresh                               # sync plaid + ws → ingest inbox → categorize → match all → snapshot balances
```

### Plaid TD adapter (`src/bankapp/ingest/plaid_td.py`) — gated by spike TP.0

- Client: official `plaid-python` (pin the version current at build time; snippets below are `illustrative; verify against installed source + current Plaid docs` per api-grounding).
- **Secrets in keyring only**: `plaid-client-id`, `plaid-secret`, `plaid-access-token-td` under service `"bankapp"`. Nothing in config but `enabled = true` and environment name.
- **Link (one-time)**: `finance plaid link` — for a personal single-user tool the pragmatic flow is Plaid's Hosted Link or a tiny localhost page serving Link; the spike decides which. Resulting `access_token` → keyring.
- **Sync**: `finance sync plaid` calls `/transactions/sync` with the persisted cursor (stored in `meta` as `plaid_cursor` — interpretation-layer state, not bank truth). For each `added` transaction: **skip pending** (`pending == true`); map posted → `NormalizedTxn` with `dedup_key = 'plaid:<transaction_id>'`, account via `ofx_acctid`-style mapping of Plaid `account_id` → config key, date as `date` field (already local), amount sign normalized (Plaid: positive = money out → negate to our signed convention), currency from `iso_currency_code`. `modified`/`removed` for posted txns: **log a warning, never mutate** `raw_txn` (immutability); rare, and pending-skip avoids almost all of them.
- Cursor + `INSERT OR IGNORE` dedup makes re-sync idempotent even after a cursor reset (full re-pull just skips).
- Graceful degradation like WS: any Plaid API error → warn + exit 0 from `refresh` (scheduler-safe); `ITEM_LOGIN_REQUIRED` → surfaced in `finance status` as "re-run finance plaid link". File adapter remains the manual fallback during such gaps.

### Advisor layer design (`src/bankapp/report/advisor.py` + views above)

- **Net worth**: every `finance refresh` appends one balance snapshot per account (WS `get_accounts` balances incl. investment value; Plaid balances from the sync/accounts payload; OFX `<LEDGERBAL>` when a file is dropped). Idempotent per day via the UNIQUE constraint. Liabilities normalized negative at the adapter. `v_net_worth` = latest per account, summed per currency (no conversion — CAD and USD reported side by side). Cross-account transfers don't distort it: moving $500 TD→WS changes two balances oppositely, net worth unchanged — that's the "intelligent sync" between accounts, and it falls out of the snapshot design for free.
- **Am I saving?**: `v_monthly_cashflow` over `v_effective` — transfers already net to zero and rent already counts as my share, so income − spend here is *true* cashflow, not gross account activity. Savings rate = net/income.
- **Subscription detection** (pure analytics, no new state): group txns by counterparty/merchant token; flag groups with ≥3 charges at a near-regular cadence (monthly ±4d, weekly ±2d, annual ±10d) and stable amounts (±5%); report effective monthly cost, last charge, and **price-creep** (latest amount > trailing median). This is the "money you don't notice" engine.
- **Leak report**: transactions under a threshold (default $15, configurable) aggregated per merchant per month, plus everything categorized `fees` — the drip spending that never feels like a decision.
- **Budgets**: config-defined per-category monthly limits; `budget status` shows actual vs limit and a pace warning (spent 80% of budget 50% through the month).
- **Goals** ("real joy" fund): config-defined name/target/start_date/allocation_pct; funded = cumulative `net_minor` since start × allocation. Multiple goals split the same savings pool via allocations (validated ≤100% total). Pace = funded vs linear path to `target_date`.
- **Digest**: `finance digest` bundles net worth + delta vs last month, savings rate trend, budget status, new/changed subscriptions, top leaks, receivables, uncategorized count, pending transfer legs — as markdown (human) or JSON (advisor skill input).

### Advisor skill (`.claude/skills/advisor/SKILL.md`) — subscription, never API

Contract: run `finance digest --format json`; narrate it against the **frugally-luxurious** mission — call out unnoticed drains (new subscriptions, price creep, leak clusters, fee months), budget pace, savings-rate trend, goal progress ("skipping the unused streaming bundle funds 6% of the ski-trip goal"). Plain English, specific dollar figures, at most 3 recommended actions per run. **Hard boundary: no investment advice** (no buy/sell/allocate recommendations — not a licensed advisor; if asked, say so); spending, budgets, and goal pacing only. Read-only: the skill may add categorization rules via `finance rules add` if it spots miscategorized spending, nothing else. Cadence: on demand, or weekly via Task Scheduler running `claude -p "/advisor"` (documented in `docs/scheduling.md`, optional).

### Claude Code skill (`.claude/skills/categorize/SKILL.md`) — subscription, never API

Contract: (1) `finance review count`; stop if 0. (2) `finance review export --format json`. (3) Per item, choose category + the most *generalizable safe pattern* (merchant token, not whole string; transfer-looking → `--role transfer`). (4) Persist each verdict via `finance rules add ... --source claude` (the rule IS the learn-once cache). (5) Re-run `finance categorize` + `review count` to confirm shrinkage; list genuinely-ambiguous leftovers for the human. (6) **Never write the DB directly** — CLI only. Pipeline has zero Anthropic imports; if the skill never runs, unknowns just wait in the queue.

### Config (`config.example.toml`)

```toml
# $FINANCE_CONFIG > %APPDATA%\bankapp\config.toml (Windows) / ~/.config/bankapp/config.toml (else)
# DB: $FINANCE_DB > db_path. All paths ~-expanded (works on Windows and macOS).
timezone   = "America/Vancouver"
db_path    = "~/finance/finance.db"
ingest_dir = "~/finance/inbox"        # TD file-drop folder

[[accounts]]
key = "td-chequing"
institution = "td"
type = "chequing"
currency = "CAD"
ofx_acctid = ""                        # fill from a real export; enables OFX auto-mapping

[[accounts]]
key = "td-visa"
institution = "td"
type = "visa"
currency = "CAD"
ofx_acctid = ""

[[accounts]]
key = "ws-cash"
institution = "wealthsimple"
type = "cash"
currency = "CAD"

[plaid]
enabled     = false                    # flip on after the TP.0 spike succeeds
environment = "production"
# account mapping: Plaid account_id → accounts.key, filled by `finance plaid link`
# credentials live in the OS keyring, never here

[budgets]                              # per-category monthly limits (CAD)
groceries      = "600.00"
dining         = "250.00"
subscriptions  = "60.00"

[[goals]]                              # the "real joy" fund(s)
name           = "example-trip"
target         = "3000.00"
start_date     = "2026-07-01"
target_date    = "2027-02-01"
allocation_pct = 100                   # share of monthly net savings credited to this goal

[advisor]
leak_threshold = "15.00"               # txns under this feed the leak report

[transfers]
window_days   = 7
tolerance     = "0.00"
seed_patterns = ["tfr-to", "tfr-fr", "eft credit", "eft debit"]   # → rules(source='seed', role_hint='transfer')

[[templates]]                          # rent — the 3-leg chain (placeholder values; Graham fills real ones)
name = "rent"
kind = "split_expense"
expected_amount    = "2400.00"         # FULL rent (2X)
share              = "1/2"             # 50/50
day_of_month       = 1
expense_account    = "ws-cash"
expense_pattern    = "landlord pattern here"
reimburse_account  = "td-chequing"
reimburser_pattern = "e-transfer.*roommate name here"   # Interac SENDER NAME — config, not code
amount_tolerance   = "5.00"
window_days        = 45
link_transfer      = true
```

Secrets: **only** in OS keyring (`keyring`, service `"bankapp"`, entry `"ws-session"`). Nothing sensitive in repo/config.

---

## Tasks (TDD per task: failing test → run → implement → run green → commit)

### Phase 0 — Scaffolding
- [x] **T0.1 Project skeleton**: `git init`; `pyproject.toml` (name `bankapp`, `requires-python=">=3.10"`, pinned deps: `ws-api==0.35.0`, `ofxtools==1.1.1`, `keyring`, `typer`, `tomli; python_version<"3.11"`, dev `pytest`; script `finance = "bankapp.cli:app"`); `src/bankapp/__init__.py`; `.gitignore` (`*.db`, `*.db-journal`, `*.db-wal`, `inbox/`, `exports/`, `quarantine/`, `.env`, `__pycache__/`); README stub; `config.example.toml`. Venv: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`. Test `tests/test_smoke.py::test_import`. Commit.
- [x] **T0.2 Config loader** (`config.py`): tests for env override, default path, `~` expansion, `FINANCE_DB`, accounts parsing, share `"1/2"` → `(1,2)`, money strings → minor ints, actionable missing-file error. `pytest tests/test_config.py -q`. Commit.
- [x] **T0.3 Money helpers** (`money.py`): `to_minor("12.34","CAD")==1234`, rejects floats, `from_minor`, exponent map, `share_split(240000,1,2)==(120000,120000)`, odd cent `share_split(240001,1,2)==(120000,120001)` (my share floors; remainder → receivable). Decimal only. Commit.
- [x] **T0.4 DB + schema** (`db.py`, `schema.sql`): tests — schema applies twice cleanly; `meta.schema_version=='1'`; **UPDATE/DELETE on raw_txn → IntegrityError 'raw_txn is immutable'**; both UNIQUE constraints enforced; views exist. `db.connect` sets `PRAGMA foreign_keys=ON`. Commit.

### Phase 1 — raw_txn + idempotent ingest core
- [x] **T1.1 Normalize + dedup key** (`normalize.py`): norm_desc cases; hash determinism; hash sensitivity to every field; occurrence counter (identical pair → 0,1; re-run same list → same values; interleaving doesn't disturb). Commit.
- [x] **T1.2 Ingest core** (`ingest/core.py`): frozen `NormalizedTxn` dataclass; `insert_batch → (inserted, skipped)` via `INSERT OR IGNORE`, one transaction; `record_import` with file-sha256 short-circuit. Tests: 3→(3,0); re-run→(0,3); same-day identical pair both land; FITID rows dedup on FITID even if description drifts between exports; unknown account → clear error. Commit.

### Phase 2 — TD file adapter (real data first)
- [x] **T2.0 GATE (manual)**: Graham drops one real TD chequing CSV *outside the repo*; `head` it; confirm/correct the believed layout (headerless; Date MM/DD/YYYY, Description, Withdrawal, Deposit, Balance) before T2.2. If unavailable, proceed on believed layout with `# LAYOUT ASSUMED — verify (T2.0)` comment + README note.
- [x] **T2.1 OFX/QFX adapter** (`ingest/ofx.py`): synthetic fixtures `td_chequing_jan.ofx` (OFX 1.x SGML), `td_visa_jan.qfx`, `malformed.ofx`. Via `OFXTree().parse()` → `.convert()` → `statements[*].transactions` (`.dtposted/.trnamt/.fitid/.name/.memo`). Tests: `dedup_key='fitid:<FITID>'`; signed amounts; ACCTID→account via config `ofx_acctid`; unmapped ACCTID → error naming the id; **malformed → quarantined to `quarantine/`, no crash, no rows**. Commit.
- [x] **T2.2 TD CSV adapter** (`ingest/csv_td.py`): fixtures incl. same-day identical pair + overlapping-window file. Headerless, MM/DD/YYYY, withdrawal→negative/deposit→positive (Decimal), balance ignored, occurrence+hash dedup, requires `--account`. Tests: parse correctness; **file A then overlapping file B → only non-overlap inserts; the identical pair exists as exactly 2 rows ever**. Commit.
- [x] **T2.3 CLI init/accounts/ingest** (`cli.py`): dispatch by extension, per-file inserted/skipped/quarantined report. `CliRunner` tests with monkeypatched env. **AT1 lands now**: `test_acceptance.py::test_at1_reingest_zero_new_rows` — ingest all fixtures twice; raw_txn count unchanged; second run reports 0 inserted. Commit.

### Phase 3 — ws-api adapter + keyring + scheduling
- [x] **T3.1 GATE: probe installed ws-api** — read `.venv/.../site-packages/ws_api/` source; document real signatures for login/`from_token`, token-refresh persistence hook, `get_accounts()`/`get_activities()` fields + pagination + exception types in `docs/ws-api-notes.md`. **All ws-api snippets are `illustrative; verify against installed source` until this exists.** Commit notes.
- [x] **T3.2 Keyring auth** (`ingest/ws.py` + `finance ws login`): token via `keyring.set_password("bankapp","ws-session",...)` — or the lib's own keyring persistence if the probe shows one (prefer the lib's). Tests with in-memory fake keyring: login stores; sync reads; missing token → "run finance ws login". Commit.
- [x] **T3.3 WS activity mapper + graceful degradation**: pure `map_activity(act, account_key, tz) → NormalizedTxn | SkipResult` tested against synthetic `ws_activities_sample.json` (shaped from probe notes; no network in tests). `wsid:` dedup keys; UTC → America/Vancouver local date (test the midnight boundary); per-txn currency kept; pending skipped; **KeyError/AttributeError/TypeError → warn + SkipResult (schema drift degrades, never crashes)**; sync reports skipped count; last error in `finance status`. Commit.
- [x] **T3.4 `finance refresh` + scheduling docs**: refresh = sync ws (soft-skip if no token) → ingest inbox → categorize → match all; exit 0 with warnings on partial failure (scheduler-safe). `docs/scheduling.md`, **Windows Task Scheduler first** (the deploy target): task XML / `schtasks` example running `<venv>\Scripts\finance.exe refresh` a few times daily with **`StartWhenAvailable` = true** (missed runs fire when the PC comes back online — the user's explicitly desired behavior) + log redirect. Secondary sections: macOS launchd plist and cron one-liner for other hosts. All paths shown as placeholders, no machine-specific absolutes. Test: refresh with no token + empty inbox runs clean. Commit.

### Phase 3B — Plaid TD adapter (automated daily TD data; gated)
- [x] **TP.0 SPIKE (manual gate, ~30 min with Graham)**: create a Plaid account (free Trial plan, ≥10 Items allowance), confirm **TD Canada Trust is linkable on the Trial tier**, link the real TD login via Plaid Link (Hosted Link if available), pull one `/transactions/sync` page, and save a **redacted** sample payload shape to `docs/plaid-notes.md` (field names only, no real data). Decision recorded there: proceed / fall back to file-drop-only. **No adapter code before this note exists.**
- [x] **TP.1 Plaid client + keyring** (`ingest/plaid_td.py`): pin `plaid-python` (version per spike); credentials + access token in keyring (`plaid-client-id`, `plaid-secret`, `plaid-access-token-td`); `finance plaid link` stores token + writes Plaid `account_id` → config-key mapping. Tests with fake keyring + mocked client: missing creds → actionable error. Commit.
- [x] **TP.2 Sync mapper + cursor** : pure `map_plaid_txn(txn: dict, account_key) → NormalizedTxn | SkipResult` against synthetic `fixtures/plaid_sync_sample.json` (shaped from spike notes, no network). Pending skipped; sign negated to our convention; `dedup_key='plaid:<transaction_id>'`; currency from `iso_currency_code`; `modified`/`removed` → warn, never mutate. Cursor persisted in `meta['plaid_cursor']` only after a fully-applied page. Tests: happy map, pending skip, sign convention, idempotent re-sync after cursor reset (all skipped), malformed txn → SkipResult. Commit.
- [x] **TP.3 Wire into `refresh` + status**: `refresh` = sync plaid (if enabled) → sync ws → ingest inbox → categorize → match all; Plaid errors warn-and-continue; `ITEM_LOGIN_REQUIRED` surfaced in `finance status`. Test: refresh with plaid disabled unchanged; with mocked failing client → exit 0 + warning. Commit.

### Phase 4 — Rules categorizer + review queue + Claude skill
- [x] **T4.1 Rules engine** (`classify/engine.py`): first match by (priority, id); substring containment; regex `re.search` compiled once; invalid regex rejected at `rules add` time; patterns stored lowercase. Commit.
- [x] **T4.2 `finance categorize`** (idempotent): fills `txn_interp` for unprocessed txns; `--all` recomputes (safe — interpretation only); seed rules upserted at `finance init` from `[transfers].seed_patterns` (`source='seed'`, `role_hint='transfer'`). Tests: run twice → identical; new rule + `--all` recategorizes; raw_txn untouched. Commit.
- [x] **T4.3 Review queue + rules/review CLI** (`classify/review.py`): queue = txns with no category and no role_hint (derived, no extra state); JSON + markdown export; `rules add` validation + friendly duplicate no-op. Commit.
- [x] **T4.4 Claude Code skill**: write `.claude/skills/categorize/SKILL.md` per the contract above. Manual dry-read verification. Commit.

### Phase 5 — Transfer matching
- [x] **T5.1 Pairing as a pure function**: `pair_legs(legs, window_days, tolerance_minor)` over tuples, no DB. Tests: exact match; either date order (TD lag); window/tolerance boundaries inclusive; tie-break (closest date, then amount diff, then lowest id); greedy one-to-one; same-account rejected; deterministic under shuffled input. Commit.
- [x] **T5.2 Persistence + CLI + AT2**: `finance match transfers` (one transaction); `--rebuild` deletes non-template transfer groups first. Tests: re-run no-op; late counterpart pairs next run; `v_pending_transfers` ages. **AT2**: `test_at2_transfer_netted` — −$500 TD "TFR-TO" + +$500 WS via CLI → one group, both rows kept, `SUM(effective_minor)==0`. Commit.

### Phase 6 — Splits, templates, receivables
- [x] **T6.1 Template upsert from config** (by name; id stable across edits). Commit.
- [x] **T6.2 Period + expense leg + share math**: lazy group creation; attach + floor share; `amount_anomaly` attached-not-dropped; `missing_expense` after grace; idempotent re-run. Commit.
- [x] **T6.3 Reimbursement matching + statuses + receivables**: sender-name regex from template; FIFO-to-oldest-unsettled across month boundaries; partial payments accumulate; statuses recomputed every run. Tests: settled; underpaid w/ correct `outstanding_minor` + aging; **next-month payment settles prior period (late-flagged while open, never lost)**; two open periods + one payment → oldest first. Commit.
- [x] **T6.4 Transfer-leg linking + `match all` + AT3**: rent group claims its TD→WS pair; `match all` = splits then transfers. **AT3** `test_at3_rent_chain` (fixtures `rent_month/`): roommate $X→TD + TD→WS $2X + WS −$2X→landlord ⇒ **one group, 4 members**; month spend == −X; receivable settled. Variants: `test_at3_underpaid` (X−50 ⇒ `underpaid`, outstanding 5000 minor), `test_at3_late_cross_month`. Commit.

### Phase 7 — Analytics + status + wrap-up
- [x] **T7.1 Spend report** over `v_effective` (per-currency subtotals, no conversion; `(uncategorized)` bucket). Commit.
- [x] **T7.2 `finance status` dashboard** (uncategorized count, pending transfers w/ age + warn >2×window, receivables aging, last sync/import, last WS skip warning). Commit.
- [x] **T7.3 Core milestone**: README (setup, TD export how-to — **whole-day windows!**, scheduling pointer, Claude categorization workflow, immutability principle); `python -m pytest -q` all green incl. AT1–AT3. Tag `v0.1.0` (core pipeline usable).

### Phase 8 — Balances & net worth
- [x] **T8.1 Balance snapshots**: `balance_snapshot` capture wired into each adapter's sync path (WS `get_accounts` balances incl. investment value — field names per T3.1 probe; Plaid balances from its accounts payload — per TP.0 spike; OFX `<LEDGERBAL>` on file ingest). Liabilities normalized negative at the adapter. Tests: snapshot appended once per account/day/source (UNIQUE); re-sync same day → no duplicate; visa sign convention. Commit.
- [x] **T8.2 Net worth report**: `v_net_worth` + `finance report networth [--history]` (history = month-end series per currency from snapshots). Tests: latest-per-account selection; visa subtracts; TD→WS transfer leaves net worth unchanged across snapshots; per-currency separation (no conversion). Commit.

### Phase 9 — Budgets, savings tracking, leak detection (the "money you don't notice" engine)
- [x] **T9.1 Cashflow/savings**: `v_monthly_cashflow` + `finance report savings [--months N]` (income, spend, net, savings rate, simple trend arrow). Tests: transfers excluded from income/spend; rent month spend = my share; savings rate math incl. zero-income month (no div-by-zero). Commit.
- [x] **T9.2 Budgets**: config `[budgets]` upserted by category; `finance budget status [--month]` with actual-vs-limit and pace warning (e.g. 80% spent at 50% of month). Tests: upsert idempotent; over/under/pace states; unbudgeted categories listed separately. Commit.
- [x] **T9.3 Subscription + leak detection** (`report/advisor.py`, pure functions over txn tuples): recurring detector (≥3 charges, cadence monthly ±4d / weekly ±2d / annual ±10d, amount stable ±5%) with effective monthly cost and **price-creep flag** (latest > trailing median); leak report (txns < threshold aggregated per merchant/month + `fees` category always included). `finance report subscriptions` / `finance report leaks`. Tests: detects monthly sub from synthetic 4-month data; jittered dates within tolerance; price creep flagged; one-off purchases not flagged; leak aggregation totals. Commit.

### Phase 10 — Goals, digest, advisor skill
- [x] **T10.1 Goals**: config `[[goals]]` upserted by name (allocations validated ≤100% total); `finance goals status` — funded = cumulative net savings since `start_date` × allocation, pace vs linear path to `target_date`. Tests: funding math; multi-goal allocation split; behind/on-pace states; inactive goal excluded. Commit.
- [x] **T10.2 Digest**: `finance digest [--format json|markdown]` bundling net worth + month delta, savings trend, budget status, new/changed subscriptions, top leaks, receivables, uncategorized count, pending transfer legs, data-quality notes (last sync ages). JSON schema stable — it's the advisor skill's input contract. Tests: both formats render from a seeded DB; JSON keys stable. Commit.
- [x] **T10.3 Advisor skill**: `.claude/skills/advisor/SKILL.md` per the contract above (digest → frugally-luxurious coaching; ≤3 actions; **no investment advice**; may only write via `finance rules add`). Optional weekly `claude -p "/advisor"` Task Scheduler entry documented in `docs/scheduling.md`. Manual dry-read verification. Commit.
- [x] **T10.4 Final sweep**: README advisor section; `python -m pytest -q` all green. Tag `v0.2.0` (advisor layer complete).

---

## Verification

1. **Full suite**: `.venv/bin/python -m pytest -q` — every task keeps it green; AT1/AT2/AT3 in `tests/test_acceptance.py` are the spec's acceptance tests as literal end-to-end CLI tests over synthetic fixtures.
2. **Immutability**: `test_db_immutability.py` proves UPDATE/DELETE on `raw_txn` abort at the engine level.
3. **Real-data smoke (manual, post-build)**: Graham exports a real TD file → `finance ingest ~/finance/inbox` → `finance status` → re-ingest same file → 0 new rows. Then `finance ws login` + `finance sync ws` and `finance plaid link` + `finance sync plaid` against the real accounts (all read-only); run `finance sync plaid` twice → second run inserts 0.
4. **No secrets/no real data in repo**: `git log -p | grep`-style check that fixtures are synthetic; `.gitignore` covers `*.db*`, `inbox/`, `quarantine/`, `exports/`.
5. **Advisor layer (post-Phase-10, manual)**: after a couple of weeks of real syncs — `finance report networth` matches what the TD/WS apps show (per currency); `finance report subscriptions` finds the known real subscriptions; `finance digest` renders; run the advisor skill once and sanity-check its 3 actions are grounded in real digest numbers.

## Risks the executor must respect

1. **ws-api unverified** → T3.1 probe is a hard gate; snippets illustrative until `docs/ws-api-notes.md` exists.
2. **TD CSV layout believed, not verified** → T2.0 gate against a real export.
2b. **Plaid Trial-tier TD availability unverified** → TP.0 spike is a hard gate with a real link before any adapter code; if TD CA isn't linkable on the free tier, record the decision in `docs/plaid-notes.md` and the pipeline remains fully functional on file-drop (Phase 3B is additive). All `plaid-python` snippets are illustrative until the spike notes exist.
3. **`HASH_VERSION` freezes once real data lands** — never change `norm_desc` afterward without a migration (out of scope).
4. Occurrence-counter stability assumes whole-day exports (README-documented user contract).
