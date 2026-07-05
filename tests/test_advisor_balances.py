from pathlib import Path

from typer.testing import CliRunner

from bankapp import db as dbmod
from bankapp.cli import app
from bankapp.ingest import ofx
from bankapp.report import advisor
from tests.conftest import insert_account

runner = CliRunner()
FIX = Path(__file__).resolve().parent / "fixtures"
ACCTID_TO_KEY = {"1111111": "td-chequing", "4519111122223333": "td-visa"}


def test_snapshot_unique_per_account_day_source(conn):
    a = insert_account(conn, key="td-chequing")
    assert advisor.snapshot_balance(conn, a, "2026-01-31", 420000, "CAD", "ofx") is True
    # same account/day/source -> no duplicate
    assert advisor.snapshot_balance(conn, a, "2026-01-31", 999999, "CAD", "ofx") is False
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshot").fetchone()[0] == 1


def test_liability_normalized_negative():
    assert advisor.normalize_balance_for_type(105388, "visa") == -105388
    assert advisor.normalize_balance_for_type(-105388, "visa") == -105388
    assert advisor.normalize_balance_for_type(420000, "chequing") == 420000


def test_ofx_ledger_balances():
    chq = ofx.ofx_ledger_balances(FIX / "td_chequing_jan.ofx", ACCTID_TO_KEY)
    assert chq[0].account_key == "td-chequing"
    assert chq[0].balance_minor == 420000
    assert chq[0].as_of == "2026-01-31"
    visa = ofx.ofx_ledger_balances(FIX / "td_visa_jan.qfx", ACCTID_TO_KEY)
    assert visa[0].balance_minor == -105388  # already negative in the export


def test_net_worth_latest_per_account_visa_subtracts(conn):
    chq = insert_account(conn, key="td-chequing", type="chequing")
    visa = insert_account(conn, key="td-visa", type="visa")
    advisor.snapshot_balance(conn, chq, "2026-01-31", 420000, "CAD", "ofx")
    advisor.snapshot_balance(conn, visa, "2026-01-31", -105388, "CAD", "ofx")
    nw = advisor.net_worth(conn)
    assert len(nw) == 1
    assert nw[0].net_worth_minor == 420000 - 105388


def test_net_worth_uses_freshest_snapshot(conn):
    chq = insert_account(conn, key="td-chequing", type="chequing")
    advisor.snapshot_balance(conn, chq, "2026-01-31", 420000, "CAD", "ofx")
    advisor.snapshot_balance(conn, chq, "2026-02-28", 500000, "CAD", "ofx")
    nw = advisor.net_worth(conn)
    assert nw[0].net_worth_minor == 500000
    assert nw[0].freshest_as_of == "2026-02-28"


def test_transfer_leaves_net_worth_unchanged(conn):
    td = insert_account(conn, key="td-chequing", type="chequing")
    ws = insert_account(conn, key="ws-cash", institution="wealthsimple", type="cash")
    advisor.snapshot_balance(conn, td, "2026-01-01", 100000, "CAD", "ofx")
    advisor.snapshot_balance(conn, ws, "2026-01-01", 200000, "CAD", "ws")
    before = advisor.net_worth(conn)[0].net_worth_minor
    # move $500 TD -> WS: both balances change oppositely
    advisor.snapshot_balance(conn, td, "2026-01-02", 50000, "CAD", "ofx")
    advisor.snapshot_balance(conn, ws, "2026-01-02", 250000, "CAD", "ws")
    after = advisor.net_worth(conn)[0].net_worth_minor
    assert before == after == 300000


def test_per_currency_separation_no_conversion(conn):
    cad = insert_account(conn, key="td-chequing", type="chequing", currency="CAD")
    usd = insert_account(conn, key="us-acct", institution="td", type="chequing", currency="USD")
    advisor.snapshot_balance(conn, cad, "2026-01-31", 420000, "CAD", "ofx")
    advisor.snapshot_balance(conn, usd, "2026-01-31", 100000, "USD", "ofx")
    nw = {r.currency: r.net_worth_minor for r in advisor.net_worth(conn)}
    assert nw == {"CAD": 420000, "USD": 100000}


def test_cli_ingest_captures_ofx_balance_and_networth(app_env):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["ingest", str(FIX / "td_chequing_jan.ofx")])
    r = runner.invoke(app, ["report", "networth"])
    assert r.exit_code == 0, r.output
    assert "4200.00" in r.output
    assert "CAD" in r.output
