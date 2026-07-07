# Moving bankapp to another machine (e.g. the Windows PC)

The routine must run **where the credentials live**: Plaid and Wealthsimple tokens sit
in the OS keyring (macOS Keychain / Windows Credential Manager), which never leaves the
machine. Today that's the Mac (a launchd agent runs `finance refresh` at 08:07 and
18:07 with catch-up on wake). To move the home base:

## Why not the cloud

Deliberately ruled out: a cloud runner would need the Plaid access token, the WS session
token, and the full transaction database uploaded into third-party compute on every run.
That inverts the project's #1 priority (security) and its local-first design — bank
tokens stay in an OS keyring, data stays in a local SQLite file you physically control.
If Canada's open-banking framework (post-Bill C-69) ever ships proper scoped read-only
API keys, revisit; until then, keep it on a machine you own.

## Migration steps (fresh machine)

1. **Install**: clone https://github.com/fieldoc/bankapp, `python -m venv .venv`,
   `.venv\Scripts\pip install -e .` (Windows) / `.venv/bin/pip install -e .` (macOS).
2. **Config**: copy your `~/.config/bankapp/config.toml` to the new machine
   (`%APPDATA%\bankapp\config.toml` on Windows). It contains no secrets.
3. **Database** — pick one:
   - **Copy** `~/finance/finance.db` over (keeps everything: rules, categories, groups,
     snapshots). Preferred.
   - Or start fresh: `finance init` re-seeds; Plaid backfills ~24 months of TD on the
     next sync; you'd re-run the categorize skill once (rules live in the DB, so copy
     beats fresh).
4. **Re-link credentials on the new machine** (keyrings don't transfer):
   - `finance plaid keys` (same Client ID/secret from the Plaid dashboard)
   - `finance plaid link` (re-links TD; a re-link on the Trial plan consumes another of
     the 10 Item slots — fine, we use 1-2)
   - `finance ws login` (fresh WS session)
5. **Schedule**: `docs/scheduling.md` — Windows Task Scheduler XML with
   `StartWhenAvailable=true` (catch-up on missed runs) is the primary recipe.
6. **Retire the old machine's schedule** (macOS):
   `launchctl bootout gui/$(id -u)/com.bankapp.refresh && rm ~/Library/LaunchAgents/com.bankapp.refresh.plist`
   Optionally revoke the old machine's Plaid Item from the Plaid dashboard and log the
   old WS session out from the WS app.

## Current Mac setup (for reference)

- Repo + venv: `~/BankApp/.venv/bin/finance`
- Config: `~/.config/bankapp/config.toml`; DB: `~/finance/finance.db`
- Schedule: `~/Library/LaunchAgents/com.bankapp.refresh.plist` (08:07 + 18:07,
  RunAtLoad catch-up); logs: `~/finance/logs/refresh.{log,err}`
