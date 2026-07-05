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
    """Create the DB, apply schema, and sync accounts from config."""
    cfg = configmod.load_config()
    conn = dbmod.init_db(cfg.db_path)
    sync_accounts(conn, cfg)
    typer.echo(f"Initialized DB at {cfg.db_path}")
    typer.echo(f"Accounts: {len(cfg.accounts)} synced")


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
        ext = f.suffix.lower()
        try:
            if ext in _OFX_EXTS:
                result = ofx.ingest_ofx_file(f, acctid_to_key, quarantine_dir=_quarantine_dir(cfg))
                if result.quarantined:
                    total_quar += 1
                    typer.echo(f"{f.name}: QUARANTINED (malformed)")
                    continue
                txns = result.txns
            elif ext in _CSV_EXTS:
                if not account:
                    typer.echo(f"{f.name}: .csv requires --account KEY")
                    raise typer.Exit(code=1)
                txns = csv_td.parse_td_csv(f, account, currency=_account_currency(cfg, account))
            else:
                typer.echo(f"{f.name}: unsupported extension {ext}")
                continue
        except ofx.UnmappedAccountError as exc:
            typer.echo(f"{f.name}: {exc}")
            raise typer.Exit(code=1)

        inserted, skipped = core.insert_batch(conn, txns)
        core.record_import(conn, f.name, core.file_sha256(f), inserted, skipped)
        total_ins += inserted
        total_skip += skipped
        typer.echo(f"{f.name}: {inserted} inserted, {skipped} skipped")

    typer.echo(f"TOTAL: {total_ins} inserted, {total_skip} skipped, {total_quar} quarantined")


if __name__ == "__main__":
    app()
