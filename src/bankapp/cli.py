"""finance CLI (typer). Thin dispatch over the adapters/engine; the SOLE write path.

Console output is plain ASCII (legacy Windows consoles choke on Unicode symbols).
"""

from __future__ import annotations

import sqlite3
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
sync_app = typer.Typer(help="Sync data from providers.")
app.add_typer(sync_app, name="sync")
rules_app = typer.Typer(help="Categorization rules.")
app.add_typer(rules_app, name="rules")
review_app = typer.Typer(help="Review queue for uncategorized transactions.")
app.add_typer(review_app, name="review")
match_app = typer.Typer(help="Match transfers and split-expense groups.")
app.add_typer(match_app, name="match")

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
            "INSERT OR IGNORE INTO accounts(key, institution, type, currency, external_id) VALUES (?,?,?,?,?)",
            (a.key, a.institution, a.type, a.currency, a.ofx_acctid or None),
        )
    conn.commit()


@app.command()
def init() -> None:
    """Create the DB, apply schema, sync accounts, and upsert seed transfer rules."""
    from bankapp.classify import engine as classify

    cfg = configmod.load_config()
    conn = dbmod.init_db(cfg.db_path)
    sync_accounts(conn, cfg)
    seeded = classify.upsert_seed_rules(conn, cfg.transfers.seed_patterns)
    typer.echo(f"Initialized DB at {cfg.db_path}")
    typer.echo(f"Accounts: {len(cfg.accounts)} synced")
    typer.echo(f"Seed transfer rules: {seeded} added")


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
        ins += i
        skip += s
    return (ins, skip, quar, msgs)


@ws_app.command("login")
def ws_login() -> None:
    """Interactive Wealthsimple login (email/password/TOTP). Token -> OS keyring.

    You type your own credentials into this prompt; only the resulting session token is
    stored (in the OS keyring), never the password.
    """
    from bankapp.ingest import ws as wsmod

    username = typer.prompt("Wealthsimple email")
    password = typer.prompt("Password", hide_input=True)
    otp = typer.prompt("2FA code (TOTP), blank if not prompted", default="")
    try:
        wsmod.authenticate(username, password, otp=otp or None)
    except Exception as exc:  # noqa: BLE001 - surface any auth failure to the user
        typer.echo(f"Login failed: {exc}")
        raise typer.Exit(code=1)
    typer.echo("WS session stored in keyring.")


@sync_app.command("ws")
def sync_ws_cmd() -> None:
    """Fetch Wealthsimple activities into raw_txn. Soft-skips on any WS error."""
    from bankapp.ingest import ws as wsmod

    cfg, conn = _load()
    sync_accounts(conn, cfg)
    report = wsmod.sync_ws(conn, cfg)
    for e in report.errors:
        typer.echo(f"WARNING: {e}")
    typer.echo(f"WS sync: {report.inserted} inserted, {report.skipped} skipped")


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
    priority: int = typer.Option(100, "--priority"),
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
    """List categorization rules in match order."""
    from bankapp.classify import engine as classify

    _, conn = _load()
    rules = sorted(classify.load_rules(conn), key=lambda r: (r.priority, r.id))
    if not rules:
        typer.echo("No rules.")
        return
    for r in rules:
        typer.echo(
            f"[{r.priority:>3}] {r.match_kind:9} {r.pattern!r} -> "
            f"category={r.category} role={r.role_hint}"
        )


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


@app.command()
def refresh() -> None:
    """One-shot pipeline: sync WS -> ingest inbox -> (categorize -> match -> snapshot).

    Scheduler-safe: partial failures warn but the command still exits 0. Later phases
    extend this with categorize, match-all, Plaid sync, and balance snapshots.
    """
    from bankapp.ingest import ws as wsmod

    cfg, conn = _load()
    sync_accounts(conn, cfg)

    # 1. Wealthsimple (soft-skip if no token / API down)
    ws_report = wsmod.sync_ws(conn, cfg)
    for e in ws_report.errors:
        typer.echo(f"WARNING: ws: {e}")
    typer.echo(f"ws: {ws_report.inserted} inserted, {ws_report.skipped} skipped")

    # 2. Ingest inbox files (OFX/QFX auto-mapped; CSV by filename convention)
    ins, skip, quar, msgs = _ingest_inbox(cfg, conn)
    for m in msgs:
        typer.echo(f"WARNING: inbox: {m}")
    typer.echo(f"inbox: {ins} inserted, {skip} skipped, {quar} quarantined")

    # 3. Categorize new transactions (rules-first, idempotent).
    from bankapp.classify import engine as classify

    n = classify.categorize(conn)
    typer.echo(f"categorized: {n}")

    # 4+. match all / snapshot balances wired in later phases.
    typer.echo("refresh complete")


if __name__ == "__main__":
    app()
