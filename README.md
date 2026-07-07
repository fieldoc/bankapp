# bankapp

Local-first personal-finance sync, categorization, and advisor pipeline.

Two layers:
1. **Data engine** — ingests transactions from TD Canada Trust (Plaid daily sync +
   OFX/QFX/CSV file drop) and Wealthsimple (`ws-api`), stores them immutably in
   SQLite, and layers revisable interpretation on top (categories, transfer links,
   split-expense groups with receivables).
2. **Advisor layer** — net worth, monthly savings rate, budgets, subscription/leak
   detection, savings goals, a one-shot digest, and a Claude Code advisor skill
   (subscription-billed) coaching toward the mission: **frugally luxurious** — catch
   money slipping away unnoticed so it can fund things that bring real joy.

## Core principle

**Bank lines are immutable truth; interpretation is a revisable layer on top; every
classifier is idempotently re-runnable without corrupting balances.** Immutability is
enforced by SQLite triggers on `raw_txn`, not by convention.

## Privacy / secrets

- **Zero Anthropic API usage** — categorization and advice run via a Claude Code
  subscription skill, never the API. No `ANTHROPIC_API_KEY` anywhere.
- Secrets (WS token, Plaid credentials) live **only** in the OS keyring, never in the
  repo or config.
- Fixtures are 100% synthetic. Real bank data is `.gitignore`d.

## Setup

```sh
python -m venv .venv
# macOS/Linux:
.venv/bin/pip install -e ".[dev]"
# Windows:
.venv\Scripts\pip install -e ".[dev]"

cp config.example.toml ~/.config/bankapp/config.toml   # edit for your accounts
finance init
```

See the implementation plan at `docs/plans/2026-07-05-personal-finance-pipeline.md`
and `docs/scheduling.md` (written during build) for automation.

## TD export contract

**Always export whole-day windows.** The content-hash dedup for CSV (which has no
transaction ids) is stable only when each day's rows appear together — which TD's
whole-day export granularity guarantees. OFX/QFX carry a stable `FITID`, so they dedup
regardless.

## Commands

```
finance init                       # create db, apply schema, sync accounts, seed rules + templates
finance accounts list
finance ingest <path>... [--account KEY]   # .ofx/.qfx auto-map by ACCTID; .csv needs --account
finance ws login                   # interactive TOTP; session token -> OS keyring
finance sync ws                    # fetch Wealthsimple activities
finance plaid keys                 # store Plaid Client ID + secret in OS keyring
finance plaid link                 # one-time: connect TD via Plaid Link (browser)
finance sync plaid                 # cursor-based /transactions/sync for TD
finance categorize [--all]         # rules-first; idempotent
finance rules add --kind substring --pattern netflix --category subscriptions
finance rules list
finance review count | export [--format json|markdown] [--out PATH]
finance match splits | transfers [--rebuild] | all [--rebuild]   # splits always re-derive from full history
finance status                     # uncategorized, pending transfers (aged), receivables, last sync
finance report spend --month YYYY-MM [--by category]
finance refresh                    # sync ws -> ingest inbox -> categorize -> match all
```

Automation: see [`docs/scheduling.md`](docs/scheduling.md) (Windows Task Scheduler first).

## Categorization workflow (Claude subscription, never the API)

1. After a sync, unknowns collect in the review queue: `finance review count`.
2. The repo-local skill `.claude/skills/categorize/SKILL.md` reads the queue
   (`finance review export --format json`), decides categories, and writes them back as
   **rules** via `finance rules add ... --source claude`.
3. `finance categorize` applies the rules. A rule is the learn-once cache — the same
   merchant never needs classifying again.

The pipeline runs fully with **no Anthropic credentials**; if the skill never runs,
unknowns simply wait in the queue.

## Advisor layer

Built on the same immutable ledger + append-only balance snapshots:

```
finance report networth [--history]     # assets - visa liability, per currency (no conversion)
finance report savings [--months N]     # income / spend / net / savings-rate per month
finance report subscriptions            # recurring charges, effective monthly cost, price-creep
finance report leaks [--threshold 15.00]# small frequent spends + fees, per merchant/month
finance budget status --month YYYY-MM    # actual vs limit, over/pace warnings
finance goals status                     # funded (net savings since start x allocation), pace
finance digest [--format json|markdown]  # one-shot bundle; JSON is the advisor skill's input
```

The advisor skill (`.claude/skills/advisor/SKILL.md`, subscription-billed) reads
`finance digest --format json` and coaches toward **frugally luxurious** — surfacing
unnoticed drains so they can fund savings goals. **It gives no investment advice** and
is read-only apart from adding categorization rules.

## Data model (one line)

`raw_txn` = immutable bank truth (UPDATE/DELETE abort via triggers). Everything else —
`txn_interp` (categories), `groups`/`group_members` (transfers, split-expense), the
advisor tables — is a revisable layer, safe to recompute. `v_effective` nets transfers
to 0, reimbursements to 0, and counts a split expense as *my share only*.
