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
whole-day export granularity guarantees.
