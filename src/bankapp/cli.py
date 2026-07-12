"""finance CLI (typer). Thin dispatch over the adapters/engine; the SOLE write path.

Console output is plain ASCII (legacy Windows consoles choke on Unicode symbols).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

import typer

from bankapp import config as configmod
from bankapp import db as dbmod
from bankapp.config import Config
from bankapp.ingest import core, csv_td, ofx

app = typer.Typer(help="Local-first personal-finance pipeline.", no_args_is_help=True)
accounts_app = typer.Typer(help="Account commands.")
app.add_typer(accounts_app, name="accounts")
ws_app = typer.Typer(help="Wealthsimple commands.")
app.add_typer(ws_app, name="ws")
plaid_app = typer.Typer(help="Plaid (TD) commands.")
app.add_typer(plaid_app, name="plaid")
sync_app = typer.Typer(help="Sync data from providers.")
app.add_typer(sync_app, name="sync")
rules_app = typer.Typer(help="Categorization rules.")
app.add_typer(rules_app, name="rules")
review_app = typer.Typer(help="Review queue for uncategorized transactions.")
app.add_typer(review_app, name="review")
match_app = typer.Typer(help="Match transfers and split-expense groups.")
app.add_typer(match_app, name="match")
report_app = typer.Typer(help="Spend and advisor reports.")
app.add_typer(report_app, name="report")
budget_app = typer.Typer(help="Budget status.")
app.add_typer(budget_app, name="budget")
goals_app = typer.Typer(help="Savings goals.")
app.add_typer(goals_app, name="goals")
advice_app = typer.Typer(help="Persisted advisor briefs (Claude coaching output).")
app.add_typer(advice_app, name="advice")
fx_app = typer.Typer(help="Manually-entered FX rates.")
app.add_typer(fx_app, name="fx")
receivables_app = typer.Typer(help="Split-expense receivables (AR-lite).")
app.add_typer(receivables_app, name="receivables")

_OFX_EXTS = {".ofx", ".qfx"}
_CSV_EXTS = {".csv"}


def _load() -> tuple[Config, sqlite3.Connection]:
    cfg = configmod.load_config()
    conn = dbmod.connect(cfg.db_path)
    dbmod.apply_schema(conn)  # cheap + idempotent; keeps schema current
    return cfg, conn


def _quarantine_dir(cfg: Config) -> Path:
    return cfg.ingest_dir.parent / "quarantine"


def sync_accounts(conn: sqlite3.Connection, cfg: Config) -> None:
    """Upsert accounts from config (idempotent; INSERT OR IGNORE by key)."""
    for a in cfg.accounts:
        conn.execute(
            "INSERT OR IGNORE INTO accounts(key, institution, type, currency, external_id, locked) VALUES (?,?,?,?,?,?)",
            (a.key, a.institution, a.type, a.currency, a.ofx_acctid or None, int(a.locked)),
        )
        conn.execute("UPDATE accounts SET locked = ? WHERE key = ?", (int(a.locked), a.key))
    conn.commit()


@app.command()
def init() -> None:
    """Create the DB, apply schema, sync accounts, upsert seed rules + templates."""
    from bankapp import goals as goalsmod
    from bankapp.classify import engine as classify
    from bankapp.match import splits

    cfg = configmod.load_config()
    conn = dbmod.init_db(cfg.db_path)
    sync_accounts(conn, cfg)
    from bankapp.report import advisor

    seeded = classify.upsert_seed_rules(conn, cfg.transfers.seed_patterns)
    ntmpl = splits.upsert_templates(conn, cfg.templates)
    nbud = advisor.upsert_budgets(conn, cfg.budgets)
    try:
        # Seeds only names that don't exist yet. The DB owns a goal once it exists,
        # so editing a [[goals]] block will not change it -- use the Goals page.
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


@accounts_app.command("list")
def accounts_list() -> None:
    """List configured accounts in the DB."""
    _, conn = _load()
    rows = conn.execute(
        "SELECT key, institution, type, currency FROM accounts ORDER BY key"
    ).fetchall()
    if not rows:
        typer.echo("No accounts. Run `finance init` after editing config.")
        return
    for r in rows:
        typer.echo(f"{r['key']:16} {r['institution']:12} {r['type']:10} {r['currency']}")


def _expand_paths(paths: List[Path]) -> List[Path]:
    files: List[Path] = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            for child in sorted(p.iterdir()):
                if child.suffix.lower() in (_OFX_EXTS | _CSV_EXTS):
                    files.append(child)
        else:
            files.append(p)
    return files


def _account_currency(cfg: Config, key: str) -> str:
    for a in cfg.accounts:
        if a.key == key:
            return a.currency
    return "CAD"


class _CsvNeedsAccount(Exception):
    pass


def _infer_csv_account(cfg: Config, filename: str) -> Optional[str]:
    """Infer a CSV's account from a `<account-key>_*.csv` filename convention."""
    low = filename.lower()
    for a in cfg.accounts:
        if low.startswith(a.key.lower()):
            return a.key
    return None


def _capture_ofx_balances(cfg, conn, f: Path, acctid_to_key: dict) -> None:
    """Snapshot <LEDGERBAL> from an OFX file (liabilities normalized negative)."""
    from bankapp.report import advisor

    type_by_key = {a.key: a.type for a in cfg.accounts}
    id_by_key = {r["key"]: r["id"] for r in conn.execute("SELECT id, key FROM accounts")}
    try:
        balances = ofx.ofx_ledger_balances(f, acctid_to_key)
    except ofx.MalformedOFXError:
        return
    for b in balances:
        aid = id_by_key.get(b.account_key)
        if aid is None:
            continue
        minor = advisor.normalize_balance_for_type(b.balance_minor, type_by_key.get(b.account_key, ""))
        advisor.snapshot_balance(conn, aid, b.as_of, minor, b.currency, "ofx")


def _file_to_txns(cfg, conn, f: Path, account: Optional[str], acctid_to_key: dict) -> tuple[list, bool]:
    """Parse one file into txns. Returns (txns, quarantined). May raise
    ofx.UnmappedAccountError, _CsvNeedsAccount, or leave unsupported files as ([], False)."""
    ext = f.suffix.lower()
    if ext in _OFX_EXTS:
        result = ofx.ingest_ofx_file(f, acctid_to_key, quarantine_dir=_quarantine_dir(cfg))
        return (result.txns, result.quarantined)
    if ext in _CSV_EXTS:
        if not account:
            raise _CsvNeedsAccount(f.name)
        return (csv_td.parse_td_csv(f, account, currency=_account_currency(cfg, account)), False)
    return ([], False)


@app.command()
def ingest(
    paths: List[Path] = typer.Argument(..., help="Files or directories to ingest."),
    account: Optional[str] = typer.Option(None, "--account", help="Account key (required for .csv)."),
) -> None:
    """Ingest .ofx/.qfx (auto-mapped by ACCTID) and .csv (needs --account) files."""
    cfg, conn = _load()
    sync_accounts(conn, cfg)
    acctid_to_key = ofx.acctid_map(cfg.accounts)

    files = _expand_paths(paths)
    if not files:
        typer.echo("No .ofx/.qfx/.csv files found.")
        raise typer.Exit(code=1)

    total_ins = total_skip = total_quar = 0
    for f in files:
        if not f.exists():
            typer.echo(f"{f.name}: NOT FOUND")
            raise typer.Exit(code=1)
        try:
            txns, quarantined = _file_to_txns(cfg, conn, f, account, acctid_to_key)
        except ofx.UnmappedAccountError as exc:
            typer.echo(f"{f.name}: {exc}")
            raise typer.Exit(code=1)
        except _CsvNeedsAccount:
            typer.echo(f"{f.name}: .csv requires --account KEY")
            raise typer.Exit(code=1)
        if quarantined:
            total_quar += 1
            typer.echo(f"{f.name}: QUARANTINED (malformed)")
            continue

        inserted, skipped = core.insert_batch(conn, txns)
        core.record_import(conn, f.name, core.file_sha256(f), inserted, skipped)
        if f.suffix.lower() in _OFX_EXTS:
            _capture_ofx_balances(cfg, conn, f, acctid_to_key)
        total_ins += inserted
        total_skip += skipped
        typer.echo(f"{f.name}: {inserted} inserted, {skipped} skipped")

    typer.echo(f"TOTAL: {total_ins} inserted, {total_skip} skipped, {total_quar} quarantined")


def _ingest_inbox(cfg, conn) -> tuple[int, int, int, list[str]]:
    """Auto-ingest inbox: .ofx/.qfx by ACCTID, .csv by `<account-key>_*.csv` convention."""
    acctid_to_key = ofx.acctid_map(cfg.accounts)
    ins = skip = quar = 0
    msgs: list[str] = []
    inbox = cfg.ingest_dir
    if not inbox.exists():
        return (0, 0, 0, msgs)
    for f in sorted(inbox.iterdir()):
        if f.suffix.lower() not in (_OFX_EXTS | _CSV_EXTS):
            continue
        account = _infer_csv_account(cfg, f.name) if f.suffix.lower() in _CSV_EXTS else None
        try:
            txns, quarantined = _file_to_txns(cfg, conn, f, account, acctid_to_key)
        except ofx.UnmappedAccountError as exc:
            msgs.append(f"{f.name}: {exc}")
            continue
        except _CsvNeedsAccount:
            msgs.append(f"{f.name}: skipped (name it <account-key>_*.csv to auto-ingest)")
            continue
        if quarantined:
            quar += 1
            msgs.append(f"{f.name}: QUARANTINED (malformed)")
            continue
        i, s = core.insert_batch(conn, txns)
        core.record_import(conn, f.name, core.file_sha256(f), i, s)
        if f.suffix.lower() in _OFX_EXTS:
            _capture_ofx_balances(cfg, conn, f, acctid_to_key)
        ins += i
        skip += s
    return (ins, skip, quar, msgs)


@ws_app.command("login")
def ws_login() -> None:
    """Interactive Wealthsimple login (email/password, then 2FA). Token -> OS keyring.

    You type your own credentials into this prompt; only the resulting session token is
    stored (in the OS keyring), never the password.

    Two-step on purpose: WS only SENDS the 2FA code when a login attempt is made, so we
    attempt first, and prompt for the code after OTPRequiredException tells us it's out.
    """
    from ws_api import OTPRequiredException

    from bankapp.ingest import ws as wsmod

    username = typer.prompt("Wealthsimple email")
    password = typer.prompt("Password", hide_input=True)
    try:
        try:
            wsmod.authenticate(username, password)
        except OTPRequiredException:
            typer.echo("A 2FA code was just sent to you by Wealthsimple.")
            otp = typer.prompt("2FA code")
            wsmod.authenticate(username, password, otp=otp.strip())
    except Exception as exc:  # noqa: BLE001 - surface any auth failure to the user
        typer.echo(f"Login failed: {exc}")
        raise typer.Exit(code=1)
    typer.echo("WS session stored in keyring.")


@plaid_app.command("keys")
def plaid_keys() -> None:
    """Store your Plaid Client ID + Production secret in the OS keyring.

    You paste your own keys into this prompt; they go straight to the keyring (encrypted,
    local-only) and never touch the repo or me.
    """
    from bankapp.ingest import plaid_td

    client_id = typer.prompt("Plaid Client ID")
    secret = typer.prompt("Plaid Production secret", hide_input=True)
    plaid_td.store_credentials(client_id.strip(), secret.strip())
    typer.echo("Plaid credentials stored in keyring.")


@plaid_app.command("link")
def plaid_link() -> None:
    """One-time: open Plaid Link in your browser to connect TD. Token -> keyring.

    You sign in to TD inside Plaid's secure window; I never see those credentials.
    """
    from bankapp.ingest import plaid_td

    cfg, conn = _load()
    sync_accounts(conn, cfg)
    try:
        mapping = plaid_td.run_link_flow(conn, cfg)
    except plaid_td.PlaidCredsError as exc:
        typer.echo(f"{exc}")
        raise typer.Exit(code=1)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Link failed: {exc}")
        raise typer.Exit(code=1)
    if not mapping:
        typer.echo("Linked, but no TD chequing/Visa accounts matched your config keys.")
    else:
        typer.echo("Linked TD. Account mapping:")
        for acct_id, key in mapping.items():
            typer.echo(f"  {key}  <-  {acct_id[:6]}...")
    typer.echo("Run `finance sync plaid` to pull transactions.")


@sync_app.command("plaid")
def sync_plaid_cmd() -> None:
    """Fetch TD transactions via Plaid /transactions/sync. Soft-skips on any error."""
    from bankapp.ingest import plaid_td

    cfg, conn = _load()
    sync_accounts(conn, cfg)
    report = plaid_td.sync_plaid(conn, cfg)
    for e in report.errors:
        typer.echo(f"WARNING: {e}")
    typer.echo(f"Plaid sync: {report.inserted} inserted, {report.skipped} skipped")


@sync_app.command("ws")
def sync_ws_cmd(
    all_history: bool = typer.Option(
        False, "--all", help="Backfill ALL history (paginate every page). Run once to set "
        "the baseline; scheduled/incremental syncs don't need it."
    ),
    how_many: int = typer.Option(
        200, "--how-many", help="Max recent activities per account when not using --all."
    ),
) -> None:
    """Fetch Wealthsimple activities into raw_txn. Soft-skips on any WS error."""
    from bankapp.ingest import ws as wsmod

    cfg, conn = _load()
    sync_accounts(conn, cfg)
    report = wsmod.sync_ws(conn, cfg, how_many=how_many, load_all=all_history)
    for e in report.errors:
        typer.echo(f"WARNING: {e}")
    extra = (f", {report.suspicious_signs} suspicious sign(s) — see warnings"
             if report.suspicious_signs else "")
    typer.echo(f"WS sync: {report.inserted} inserted, {report.skipped} skipped{extra}")


@app.command()
def categorize(all: bool = typer.Option(False, "--all", help="Recompute every txn from current rules.")) -> None:
    """Apply rules to raw_txn -> txn_interp. Idempotent; --all recomputes."""
    from bankapp.classify import engine as classify

    _, conn = _load()
    n = classify.categorize(conn, recompute_all=all)
    typer.echo(f"Categorized {n} transaction(s).")


@rules_app.command("add")
def rules_add(
    kind: str = typer.Option("substring", "--kind", help="substring | regex"),
    pattern: str = typer.Option(..., "--pattern"),
    category: Optional[str] = typer.Option(None, "--category"),
    role: Optional[str] = typer.Option(None, "--role", help="transfer|reimbursement|expense|income"),
    counterparty: Optional[str] = typer.Option(None, "--counterparty"),
    priority: int = typer.Option(100, "--priority", help="Lower wins; ties: longer pattern first, then lower id."),
    source: str = typer.Option("manual", "--source", help="manual|claude|seed"),
) -> None:
    """Add a categorization rule (the learn-once cache). Duplicate patterns no-op."""
    from bankapp.classify import engine as classify

    _, conn = _load()
    try:
        added = classify.add_rule(
            conn, kind, pattern, category=category, role_hint=role,
            counterparty=counterparty, priority=priority, source=source,
        )
    except classify.InvalidPatternError as exc:
        typer.echo(f"Invalid rule: {exc}")
        raise typer.Exit(code=1)
    typer.echo("Rule added." if added else "Rule already exists (no-op).")


@rules_app.command("list")
def rules_list() -> None:
    """List categorization rules in match order (priority, then longer pattern, then id)."""
    from bankapp.classify import engine as classify

    _, conn = _load()
    rules = sorted(classify.load_rules(conn), key=classify.match_order_key)
    if not rules:
        typer.echo("No rules.")
        return
    for r in rules:
        cp = f" counterparty={r.counterparty}" if r.counterparty else ""
        typer.echo(
            f"[{r.priority:>3}] {r.match_kind:9} {r.pattern!r} -> "
            f"category={r.category} role={r.role_hint}{cp}"
        )


@rules_app.command("rm")
def rules_rm(
    kind: str = typer.Option("substring", "--kind", help="substring | regex"),
    pattern: str = typer.Option(..., "--pattern", help="The pattern exactly as `rules list` shows it."),
) -> None:
    """Remove a rule and re-categorize: labels that only existed because of this rule
    are dropped (manual overrides survive)."""
    from bankapp.classify import engine as classify

    _, conn = _load()
    if not classify.remove_rule(conn, kind, pattern):
        typer.echo(f"No such rule: {kind} {pattern!r}")
        raise typer.Exit(code=1)
    n = classify.categorize(conn, recompute_all=True)
    typer.echo(f"Rule removed; {n} transaction(s) re-categorized.")


@rules_app.command("set-counterparty")
def rules_set_counterparty(
    kind: str = typer.Option("substring", "--kind", help="substring | regex"),
    pattern: str = typer.Option(..., "--pattern", help="The pattern exactly as `rules list` shows it."),
    counterparty: str = typer.Option(..., "--counterparty",
                                     help="Canonical merchant name (merges vendor renames in "
                                          "subscriptions/leaks). Pass '' to clear."),
) -> None:
    """Set a rule's counterparty and re-stamp matching transactions."""
    from bankapp.classify import engine as classify

    _, conn = _load()
    if not classify.set_rule_counterparty(conn, kind, pattern, counterparty or None):
        typer.echo(f"No such rule: {kind} {pattern!r}")
        raise typer.Exit(code=1)
    n = classify.categorize(conn, recompute_all=True)
    typer.echo(f"Counterparty {'cleared' if not counterparty else 'set to ' + counterparty!r}; "
               f"{n} transaction(s) re-stamped.")


@receivables_app.command("list")
def receivables_list() -> None:
    """List all split-expense receivables (template, period, expected/received/settled/outstanding, age)."""
    from bankapp import money

    _, conn = _load()
    rows = conn.execute(
        """SELECT template, period_key, status, expected_minor, received_minor,
                  settled_minor, outstanding_minor, age_days
           FROM v_receivables ORDER BY period_key DESC"""
    ).fetchall()
    if not rows:
        typer.echo("No receivables.")
        return
    for r in rows:
        typer.echo(
            f"{r['template']} {r['period_key']} [{r['status']}] "
            f"expected={money.from_minor(r['expected_minor'], 'CAD')} "
            f"received={money.from_minor(r['received_minor'], 'CAD')} "
            f"settled={money.from_minor(r['settled_minor'], 'CAD')} "
            f"outstanding={money.from_minor(r['outstanding_minor'], 'CAD')} "
            f"age={r['age_days']}d"
        )


@receivables_app.command("settle")
def receivables_settle(
    template: str = typer.Option(..., "--template", help="Template name, as in `rules list` / config."),
    period: str = typer.Option(..., "--period", help="Period key, 'YYYY-MM'."),
    amount: Optional[str] = typer.Option(
        None, "--amount", help="Major units to record as settled. Default: full outstanding non-bank amount."
    ),
    note: Optional[str] = typer.Option(None, "--note"),
) -> None:
    """Mark a receivable settled (e.g. a roommate paid their share back in cash).
    Survives `finance match all --rebuild` -- it is keyed on template+period, not
    on the (rebuildable) group id."""
    from bankapp import money
    from bankapp import receivables as receivablesmod

    _, conn = _load()
    amount_minor = money.to_minor(amount, "CAD") if amount is not None else None
    try:
        result = receivablesmod.settle_by_template(
            conn, template, period, amount_minor=amount_minor, note=note
        )
    except receivablesmod.ReceivableNotFound as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)
    typer.echo(
        f"Settled {money.from_minor(result['settled_minor'], 'CAD')} for "
        f"{result['template']} {result['period_key']}; "
        f"outstanding now {money.from_minor(result['outstanding_minor'], 'CAD')}."
    )


@fx_app.command("set")
def fx_set(
    pair: str = typer.Option(..., "--pair", help="BASE/QUOTE, e.g. USD/CAD"),
    rate: str = typer.Option(..., "--rate"),
    as_of: Optional[str] = typer.Option(None, "--as-of", help="YYYY-MM-DD; default today."),
) -> None:
    """Set (upsert) a manually-entered FX rate. Re-running for the same pair/day
    updates it -- latest wins, no duplicate rows."""
    from bankapp import fx

    _, conn = _load()
    try:
        base, quote = pair.upper().split("/", 1)
    except ValueError:
        typer.echo(f"Invalid --pair {pair!r}; expected BASE/QUOTE, e.g. USD/CAD")
        raise typer.Exit(code=1)
    fx.set_rate(conn, base, quote, rate, as_of=as_of)
    typer.echo(f"Rate set: 1 {base} = {rate} {quote} (as of {as_of or date.today().isoformat()})")


@fx_app.command("list")
def fx_list() -> None:
    """List stored FX rates (latest per pair)."""
    from bankapp import fx

    _, conn = _load()
    rates = fx.list_rates(conn)
    if not rates:
        typer.echo("No FX rates set.")
        return
    for r in rates:
        typer.echo(f"{r['base']}/{r['quote']} = {r['rate']} (as of {r['as_of']})")


@review_app.command("count")
def review_count() -> None:
    """Print the number of uncategorized transactions."""
    from bankapp.classify import review

    _, conn = _load()
    typer.echo(str(review.count(conn)))


@review_app.command("export")
def review_export(
    format: str = typer.Option("json", "--format", help="json | markdown"),
    out: Optional[Path] = typer.Option(None, "--out", help="Write to a file instead of stdout."),
) -> None:
    """Export the review queue for the Claude categorize skill."""
    from bankapp.classify import review

    _, conn = _load()
    text = review.export_markdown(conn) if format == "markdown" else review.export_json(conn)
    if out:
        Path(out).expanduser().write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {out}")
    else:
        typer.echo(text)


@match_app.command("transfers")
def match_transfers_cmd(
    rebuild: bool = typer.Option(False, "--rebuild", help="Delete generic transfer groups and rematch."),
) -> None:
    """Pair hinted transfer legs across accounts into transfer groups."""
    from bankapp.match import transfers

    cfg, conn = _load()
    n = transfers.match_transfers(
        conn, cfg.transfers.window_days, cfg.transfers.tolerance_minor, rebuild=rebuild
    )
    typer.echo(f"Matched {n} transfer pair(s).")


@match_app.command("splits")
def match_splits_cmd() -> None:
    """Build split-expense groups (rent chain, receivables) from templates.

    Split groups are always re-derived from full history, so a backfill import
    self-corrects on the next run — no rebuild flag needed.
    """
    from bankapp.match import splits

    cfg, conn = _load()
    splits.upsert_templates(conn, cfg.templates)
    n = splits.match_splits(conn)
    typer.echo(f"Processed {n} split period(s).")


@match_app.command("all")
def match_all_cmd(
    rebuild: bool = typer.Option(
        False, "--rebuild",
        help="Release generic transfer groups first so split templates can reclaim "
        "their legs, then re-pair the rest (split groups always re-derive).",
    ),
) -> None:
    """Run splits BEFORE transfers (splits claim their own transfer legs first)."""
    from bankapp.match import splits, transfers

    cfg, conn = _load()
    splits.upsert_templates(conn, cfg.templates)
    if rebuild:
        # Free generic-group legs BEFORE splits runs: a leg stuck in a generic
        # transfer group is invisible to _attach_transfer_legs, and deleting the
        # generic groups after splits would strand it there forever.
        with conn:
            transfers.clear_generic_groups(conn)
    ns = splits.match_splits(conn)
    nt = transfers.match_transfers(
        conn, cfg.transfers.window_days, cfg.transfers.tolerance_minor
    )
    typer.echo(f"splits: {ns} period(s); transfers: {nt} pair(s)")


@report_app.command("spend")
def report_spend(
    month: str = typer.Option(..., "--month", help="YYYY-MM"),
    by: Optional[str] = typer.Option(None, "--by", help="category"),
) -> None:
    """Spend for a month, per currency; --by category breaks it down."""
    from bankapp import money
    from bankapp.report import analytics

    _, conn = _load()
    rows = analytics.spend_by_category(conn, month) if by == "category" else analytics.spend_total(conn, month)
    if not rows:
        typer.echo(f"No spend recorded for {month}.")
        return
    for r in rows:
        typer.echo(f"{r.category:20} {money.from_minor(r.spend_minor, r.currency):>12} {r.currency}")


@report_app.command("networth")
def report_networth(
    history: bool = typer.Option(False, "--history", help="Month-end series per currency."),
) -> None:
    """Net worth = latest snapshot per account, summed per currency (no conversion)."""
    from bankapp import money
    from bankapp.report import advisor

    _, conn = _load()
    if history:
        rows = advisor.net_worth_history(conn)
        if not rows:
            typer.echo("No balance snapshots yet.")
            return
        for r in rows:
            typer.echo(f"{r['month']} {money.from_minor(r['net_worth_minor'], r['currency']):>14} {r['currency']}")
        return
    rows = advisor.net_worth(conn)
    if not rows:
        typer.echo("No balance snapshots yet. Sync or ingest an OFX with a ledger balance.")
        return
    split = {s["currency"]: s for s in advisor.net_worth_split(conn)}
    for r in rows:
        typer.echo(f"{money.from_minor(r.net_worth_minor, r.currency):>14} {r.currency}  (as of {r.freshest_as_of})")
        s = split.get(r.currency)
        if s and s["locked_minor"]:
            typer.echo(f"    accessible {money.from_minor(s['accessible_minor'], r.currency):>12} {r.currency}")
            typer.echo(f"    locked     {money.from_minor(s['locked_minor'], r.currency):>12} {r.currency}  (not spendable)")


@report_app.command("savings")
def report_savings(months: Optional[int] = typer.Option(None, "--months", help="Last N months.")) -> None:
    """Income / spend / net / savings-rate per month, with a trend arrow."""
    from bankapp import money
    from bankapp.report import advisor

    _, conn = _load()
    rows = advisor.monthly_cashflow(conn, months=months)
    if not rows:
        typer.echo("No cashflow yet.")
        return
    prev: dict[str, int] = {}  # per currency — a USD net is not comparable to a CAD one
    for r in rows:
        p = prev.get(r.currency)
        arrow = "=" if p is None or r.net_minor == p else ("+" if r.net_minor > p else "-")
        typer.echo(
            f"{r.month} {r.currency}  income={money.from_minor(r.income_minor, r.currency):>10}  "
            f"spend={money.from_minor(r.spend_minor, r.currency):>10}  "
            f"net={money.from_minor(r.net_minor, r.currency):>10}  "
            f"rate={r.savings_rate * 100:5.1f}%  {arrow}"
        )
        prev[r.currency] = r.net_minor


@report_app.command("projection")
def report_projection() -> None:
    """Per-currency safe-to-spend for the current month: expected income minus
    spent-so-far minus committed-remaining minus the need-to-save/like-to-save
    goal buckets, floored at 0."""
    from bankapp import money
    from bankapp.report import projection

    _, conn = _load()
    rows = projection.month_projection(conn)
    if not rows:
        typer.echo("No data yet.")
        return
    for r in rows:
        typer.echo(f"{r.month} {r.currency}")
        typer.echo(f"  expected income      {money.from_minor(r.expected_income_minor, r.currency):>12} {r.currency}")
        typer.echo(f"  spent so far         {money.from_minor(r.spent_so_far_minor, r.currency):>12} {r.currency}")
        typer.echo(f"  committed remaining  {money.from_minor(r.committed_remaining_minor, r.currency):>12} {r.currency}")
        if r.need_to_save_minor > 0:
            typer.echo(f"  need to save         {money.from_minor(r.need_to_save_minor, r.currency):>12} {r.currency}")
        if r.like_to_save_minor > 0:
            typer.echo(f"  like to save         {money.from_minor(r.like_to_save_minor, r.currency):>12} {r.currency}")
        typer.echo(f"  safe to spend        {money.from_minor(r.safe_to_spend_minor, r.currency):>12} {r.currency}")
        if r.savings_shortfall_minor > 0:
            typer.echo(
                f"  plan short by        {money.from_minor(r.savings_shortfall_minor, r.currency):>12} "
                f"{r.currency}  (lowest-priority goals cut)"
            )
            for gf in r.goal_funding:
                if gf.status != "funded":
                    typer.echo(
                        f"    {gf.name}: {gf.status} "
                        f"({money.from_minor(gf.allocated_minor, r.currency)}/"
                        f"{money.from_minor(gf.ask_minor, r.currency)} {r.currency})"
                    )


@budget_app.command("status")
def budget_status_cmd(month: str = typer.Option(..., "--month", help="YYYY-MM")) -> None:
    """Per-category actual vs limit for a month, with over/pace warnings."""
    from bankapp import money
    from bankapp.report import advisor

    _, conn = _load()
    rows = advisor.budget_status(conn, month)
    if not rows:
        typer.echo("No budgets configured or no spend this month.")
        return
    for r in rows:
        if r.limit_minor is None:
            typer.echo(f"  {r.category:20} {money.from_minor(r.actual_minor, 'CAD'):>10}  (unbudgeted)")
            continue
        flag = "  [OVER]" if r.over else ("  [pace]" if r.pace_warn else "")
        typer.echo(
            f"  {r.category:20} {money.from_minor(r.actual_minor, 'CAD'):>10} / "
            f"{money.from_minor(r.limit_minor, 'CAD'):>10}{flag}"
        )


@report_app.command("subscriptions")
def report_subscriptions() -> None:
    """Recurring charges: cadence, effective monthly cost, price-creep flags."""
    from bankapp import money
    from bankapp.report import advisor

    _, conn = _load()
    subs = advisor.subscriptions_from_db(conn)
    if not subs:
        typer.echo("No recurring charges detected.")
        return
    for s in subs:
        creep = "  [price up]" if s.price_creep else ""
        typer.echo(
            f"  {s.merchant:20} {s.cadence:8} ~{money.from_minor(s.monthly_cost_minor, s.currency)}/mo "
            f"{s.currency}  (x{s.count}, last {s.last_charge}){creep}"
        )


@report_app.command("leaks")
def report_leaks(threshold: str = typer.Option("15.00", "--threshold", help="Dollar threshold.")) -> None:
    """Small frequent spends + fees, aggregated per merchant/month."""
    from bankapp import money
    from bankapp.report import advisor

    cfg, conn = _load()
    thr = money.to_minor(threshold, "CAD") if threshold else cfg.leak_threshold_minor
    rows = advisor.leaks_from_db(conn, thr)
    if not rows:
        typer.echo("No leaks detected.")
        return
    for r in rows:
        typer.echo(
            f"  {r.merchant:20} {r.month}  {money.from_minor(r.total_minor, r.currency):>10} {r.currency}  (x{r.count})"
        )


@report_app.command("reconcile")
def report_reconcile() -> None:
    """Per account: does the bank-provided balance match the summed ledger between
    snapshots? Flags drift with the amount."""
    from bankapp import money
    from bankapp.report import advisor

    _, conn = _load()
    rows = advisor.reconcile(conn)
    if not rows:
        typer.echo("No balance snapshots yet.")
        return
    any_drift = False
    for r in rows:
        if r.status == "unverified":
            typer.echo(f"  {r.account_key:20} only one snapshot ({r.anchor_as_of}) — cannot reconcile")
            continue
        flag = "  [DRIFT]" if r.status == "drift" else ""
        if r.status == "drift":
            any_drift = True
        typer.echo(
            f"  {r.account_key:20} {r.anchor_as_of} -> {r.target_as_of}  "
            f"expected {money.from_minor(r.expected_delta_minor, r.currency):>10} {r.currency}  "
            f"ledger {money.from_minor(r.ledger_delta_minor, r.currency):>10} {r.currency}  "
            f"drift {money.from_minor(r.drift_minor, r.currency):>10} {r.currency}{flag}"
        )
    if not any_drift:
        typer.echo("All accounts reconciled.")


@report_app.command("anomalies")
def report_anomalies() -> None:
    """Money-affecting oddities: unusual charges, stopped subscriptions, duplicates."""
    from bankapp.report import anomalies

    _, conn = _load()
    rows = anomalies.anomalies_from_db(conn)
    if not rows:
        typer.echo("No anomalies.")
        return
    for a in rows:
        typer.echo(f"  [{a.kind}] {a.merchant} - {a.detail}")


@goals_app.command("status")
def goals_status_cmd() -> None:
    """Per-goal funded (net savings since start x allocation), % complete, pace."""
    from bankapp import money
    from bankapp.report import advisor

    _, conn = _load()
    rows = advisor.goals_status(conn)
    if not rows:
        typer.echo("No goals configured.")
        return
    for g in rows:
        mode_label = "fixed" if g.funding_mode == "fixed_monthly" else "target"
        typer.echo(
            f"  {g.name:20} {money.from_minor(g.funded_minor, g.currency):>12} / "
            f"{money.from_minor(g.target_minor, g.currency):>12} {g.currency}  "
            f"({g.pct_complete:.0f}%, {g.pace})"
            f" [{mode_label} · asks {money.from_minor(g.monthly_ask_minor, g.currency)}/mo · p{g.priority}]"
        )


@app.command()
def digest(format: str = typer.Option("markdown", "--format", help="markdown | json")) -> None:
    """One-shot advisor bundle: net worth, savings, budgets, subscriptions, leaks, goals."""
    import json as _json

    from bankapp.report import advisor

    cfg, conn = _load()
    d = advisor.digest(conn, cfg)
    if format == "json":
        typer.echo(_json.dumps(d, indent=2))
    else:
        typer.echo(advisor.render_digest_markdown(d))


@app.command()
def status() -> None:
    """Dashboard: uncategorized, pending transfers (aged), receivables, last sync/import."""
    from bankapp import money
    from bankapp.report import advisor, analytics

    cfg, conn = _load()
    st = analytics.status(conn, cfg.transfers.window_days)
    typer.echo(f"Uncategorized transactions: {st.uncategorized}")

    typer.echo(f"Pending transfer legs: {len(st.pending_transfers)}")
    for p in st.pending_transfers:
        flag = "  [WARN: stale]" if p["warn"] else ""
        typer.echo(f"  txn {p['id']} {money.from_minor(p['amount_minor'], 'CAD')} age={p['age_days']}d{flag}")

    outstanding = [r for r in st.receivables]
    typer.echo(f"Outstanding receivables: {len(outstanding)}")
    for r in outstanding:
        typer.echo(
            f"  {r['template']} {r['period_key']} {r['status']} "
            f"owed={money.from_minor(r['outstanding_minor'], 'CAD')} age={r['age_days']}d"
        )

    typer.echo(f"Last import:   {st.last_import or '(never)'}")
    typer.echo(f"Last WS sync:  {st.last_ws_sync or '(never)'}")
    if st.ws_last_error:
        typer.echo(f"Last WS error: {st.ws_last_error}")
    plaid_sync = dbmod.get_meta(conn, "plaid_last_sync")
    plaid_err = dbmod.get_meta(conn, "plaid_last_error")
    typer.echo(f"Last Plaid sync: {plaid_sync or '(never)'}")
    if plaid_err:
        typer.echo(f"Last Plaid error: {plaid_err}")

    drift_count = sum(1 for r in advisor.reconcile(conn) if r.status == "drift")
    if drift_count:
        typer.echo(f"Reconciliation: {drift_count} account(s) with drift — run 'finance report reconcile'")
    else:
        typer.echo("Reconciliation: all accounts reconciled")


@app.command()
def refresh() -> None:
    """One-shot pipeline: sync WS -> ingest inbox -> (categorize -> match -> snapshot).

    Scheduler-safe: partial failures warn but the command still exits 0. Later phases
    extend this with categorize, match-all, Plaid sync, and balance snapshots.
    """
    from bankapp.ingest import ws as wsmod

    cfg, conn = _load()
    sync_accounts(conn, cfg)

    # 0. Plaid TD (only if enabled; soft-skip on any error)
    if cfg.plaid.enabled:
        from bankapp.ingest import plaid_td

        p_report = plaid_td.sync_plaid(conn, cfg)
        for e in p_report.errors:
            typer.echo(f"WARNING: plaid: {e}")
        typer.echo(f"plaid: {p_report.inserted} inserted, {p_report.skipped} skipped")

    # 1. Wealthsimple (soft-skip if no token / API down)
    ws_report = wsmod.sync_ws(conn, cfg)
    for e in ws_report.errors:
        typer.echo(f"WARNING: ws: {e}")
    ws_extra = (f", {ws_report.suspicious_signs} suspicious sign(s) — see warnings"
                if ws_report.suspicious_signs else "")
    typer.echo(f"ws: {ws_report.inserted} inserted, {ws_report.skipped} skipped{ws_extra}")

    # 2. Ingest inbox files (OFX/QFX auto-mapped; CSV by filename convention)
    ins, skip, quar, msgs = _ingest_inbox(cfg, conn)
    for m in msgs:
        typer.echo(f"WARNING: inbox: {m}")
    typer.echo(f"inbox: {ins} inserted, {skip} skipped, {quar} quarantined")

    # 3. Categorize new transactions (rules-first, idempotent).
    from bankapp.classify import engine as classify

    n = classify.categorize(conn)
    typer.echo(f"categorized: {n}")

    # 4. Match splits (which claim their transfer legs) then generic transfers.
    from bankapp.match import splits, transfers

    splits.upsert_templates(conn, cfg.templates)
    ns = splits.match_splits(conn)
    nt = transfers.match_transfers(conn, cfg.transfers.window_days, cfg.transfers.tolerance_minor)
    typer.echo(f"match: {ns} split period(s), {nt} transfer pair(s)")

    # 5+. snapshot balances wired in later phases.
    typer.echo("refresh complete")


@advice_app.command("add")
def advice_add(
    file: Optional[Path] = typer.Option(None, "--file", help="Read brief content from this file."),
    as_of: str = typer.Option(None, "--as-of", help="Digest as-of date (YYYY-MM-DD). Default: today."),
    source: str = typer.Option("claude", "--source", help="claude | manual"),
) -> None:
    """Persist an advisor brief (Claude coaching output). Reads --file or stdin."""
    import json

    from bankapp.report import advisor, briefs

    if as_of is None:
        as_of = date.today().isoformat()
    content_md = Path(file).read_text(encoding="utf-8") if file is not None else sys.stdin.read()

    cfg, conn = _load()
    d = advisor.digest(conn, cfg)
    d.pop("changes_since_brief", None)  # store the PURE snapshot -- no recursion
    digest_json = json.dumps(d)
    try:
        brief_id = briefs.add_brief(conn, content_md, as_of, source=source, digest_json=digest_json)
    except ValueError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(1)
    typer.echo(f"Brief #{brief_id} saved (as of {as_of}).")


@advice_app.command("show")
def advice_show() -> None:
    """Print the latest brief's content (raw markdown)."""
    from bankapp.report import briefs

    _, conn = _load()
    brief = briefs.latest(conn)
    if brief is None:
        typer.echo("No briefs yet.")
        return
    typer.echo(brief["content_md"])


@advice_app.command("list")
def advice_list() -> None:
    """List briefs, newest first."""
    from bankapp.report import briefs

    _, conn = _load()
    rows = briefs.list_briefs(conn)
    if not rows:
        typer.echo("No briefs yet.")
        return
    for r in rows:
        first_line = r["content_md"].splitlines()[0] if r["content_md"] else ""
        snippet = first_line[:60]
        typer.echo(f"#{r['id']}  {r['created_at']}  as-of {r['digest_as_of']}  {snippet}")


@app.command()
def serve(port: int = typer.Option(8377, "--port"), no_open: bool = typer.Option(False, "--no-open")) -> None:
    """Launch the local web dashboard (127.0.0.1 only)."""
    from bankapp.web import app as webapp

    cfg, conn = _load()
    conn.close()
    webapp.serve(cfg, port=port, open_browser=not no_open)


if __name__ == "__main__":
    app()
