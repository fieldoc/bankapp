from typer.testing import CliRunner

from bankapp import db as dbmod
from bankapp.cli import app
from tests.conftest import insert_raw_txn

runner = CliRunner()


def _seed(db_path):
    conn = dbmod.connect(db_path)
    acct = conn.execute("SELECT id FROM accounts WHERE key='td-chequing'").fetchone()[0]
    insert_raw_txn(conn, acct, posted_date="2026-01-05", amount_minor=500000,
                   description_raw="PAYROLL", description_norm="payroll", dedup_key="d0")
    for i, d in enumerate(["2026-01-03", "2026-02-02", "2026-03-04"]):
        insert_raw_txn(conn, acct, posted_date=d, amount_minor=-1599,
                       description_raw="NETFLIX.COM", description_norm="netflix.com", dedup_key=f"n{i}")
    conn.close()


def test_report_savings(app_env):
    runner.invoke(app, ["init"])
    _seed(app_env["db"])
    r = runner.invoke(app, ["report", "savings"])
    assert r.exit_code == 0, r.output
    assert "2026-01" in r.output
    assert "rate=" in r.output


def test_budget_status(app_env):
    runner.invoke(app, ["init"])  # example config has groceries/dining/subscriptions budgets
    r = runner.invoke(app, ["budget", "status", "--month", "2026-01"])
    assert r.exit_code == 0, r.output


def test_report_subscriptions(app_env):
    runner.invoke(app, ["init"])
    _seed(app_env["db"])
    r = runner.invoke(app, ["report", "subscriptions"])
    assert r.exit_code == 0, r.output
    assert "netflix.com" in r.output
    assert "monthly" in r.output


def test_report_leaks(app_env):
    runner.invoke(app, ["init"])
    _seed(app_env["db"])
    r = runner.invoke(app, ["report", "leaks", "--threshold", "20.00"])
    assert r.exit_code == 0, r.output
    assert "netflix" in r.output
