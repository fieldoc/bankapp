from datetime import date

from typer.testing import CliRunner

from bankapp import db as dbmod
from bankapp.cli import app
from bankapp.report import analytics
from tests.conftest import insert_account, insert_raw_txn

runner = CliRunner()


def _txn(conn, acct, date_s, amt, desc, dedup, category=None):
    tid = insert_raw_txn(conn, acct, posted_date=date_s, amount_minor=amt,
                         description_raw=desc, description_norm=desc.lower(), dedup_key=dedup)
    if category:
        conn.execute(
            "INSERT INTO txn_interp(raw_txn_id, category, updated_at) VALUES (?,?,'t')", (tid, category)
        )
    conn.commit()
    return tid


def test_spend_total_per_currency(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-01-05", -1000, "coffee", "d1")
    _txn(conn, a, "2026-01-06", -2000, "lunch", "d2")
    _txn(conn, a, "2026-01-07", 5000, "income", "d3")  # inflow not counted as spend
    rows = analytics.spend_total(conn, "2026-01")
    assert len(rows) == 1
    assert rows[0].spend_minor == 3000
    assert rows[0].currency == "CAD"


def test_spend_by_category_with_uncategorized(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-01-05", -1000, "netflix", "d1", category="subscriptions")
    _txn(conn, a, "2026-01-06", -2000, "mystery", "d2")  # uncategorized
    rows = analytics.spend_by_category(conn, "2026-01")
    cats = {r.category: r.spend_minor for r in rows}
    assert cats["subscriptions"] == 1000
    assert cats["(uncategorized)"] == 2000


def test_status_report_fields(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-01-05", -1000, "mystery", "d1")  # uncategorized -> queue
    st = analytics.status(conn, transfer_window_days=7)
    assert st.uncategorized == 1
    assert st.pending_transfers == []
    assert st.receivables == []


def test_cli_report_spend(app_env):
    runner.invoke(app, ["init"])
    conn = dbmod.connect(app_env["db"])
    acct = conn.execute("SELECT id FROM accounts WHERE key='td-chequing'").fetchone()[0]
    _txn(conn, acct, "2026-01-05", -4500, "netflix", "d1")
    conn.close()
    r = runner.invoke(app, ["report", "spend", "--month", "2026-01"])
    assert r.exit_code == 0, r.output
    assert "45.00" in r.output


def test_cli_status_runs(app_env, memkeyring):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert "Uncategorized transactions:" in r.output
