from datetime import date

import pytest
from typer.testing import CliRunner

from bankapp import db as dbmod
from bankapp.cli import app
from bankapp.report import analytics
from tests.conftest import insert_account, insert_raw_txn

runner = CliRunner()


def _txn(conn, acct, date_s, amt, desc, dedup, category=None, role_hint=None):
    tid = insert_raw_txn(conn, acct, posted_date=date_s, amount_minor=amt,
                         description_raw=desc, description_norm=desc.lower(), dedup_key=dedup)
    if category or role_hint:
        conn.execute(
            "INSERT INTO txn_interp(raw_txn_id, category, role_hint, updated_at) VALUES (?,?,?,'t')",
            (tid, category, role_hint),
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


# ---- cash-flow Sankey (month_flows) ----------------------------------------

@pytest.mark.parametrize("desc,expected", [
    ("direct deposit: from cloud produce a", "Cloud Produce A"),
    ("direct deposit: from motherwell elec", "Motherwell Elec"),
    ("interest", "Interest"),
    ("stock lending earnings", "Stock lending"),
    ("deposit: cheque", "Cheque deposit"),
    ("payroll run", "Other income"),
    ("", "Other income"),
    (None, "Other income"),
])
def test_income_source_label(desc, expected):
    assert analytics.income_source_label(desc) == expected


def _links_from(mf, prefix):
    return [l for l in mf.links if l.source.startswith(prefix)]


def _links_to(mf, prefix):
    return [l for l in mf.links if l.target.startswith(prefix)]


def test_month_flows_basic_shape(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-01-05", 500000, "direct deposit: from cloud produce a", "i1", category="income")
    _txn(conn, a, "2026-01-06", -220000, "landlord", "s1", category="rent")
    _txn(conn, a, "2026-01-07", -6000, "loblaws", "s2", category="groceries")
    mf = analytics.month_flows(conn, "2026-01", {"rent": "Housing", "groceries": "Food"})
    assert mf is not None
    assert mf.month == "2026-01" and mf.currency == "CAD"
    keys = set(mf.labels)
    assert "src:Cloud Produce A" in keys
    assert "inc:Income" in keys
    assert "grp:Housing" in keys and "grp:Food" in keys
    assert "cat:rent" in keys and "cat:groceries" in keys
    assert "sav:Savings" in keys  # 500000 income - 226000 spend = 274000 saved
    assert mf.labels["cat:rent"] == "rent"
    # employer -> Income
    assert {(l.source, l.target, l.flow_minor) for l in _links_to(mf, "inc:")} == {
        ("src:Cloud Produce A", "inc:Income", 500000)
    }
    # every flow positive
    assert all(l.flow_minor > 0 for l in mf.links)


def test_month_flows_reconciles_with_v_monthly_cashflow(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-02-01", 400000, "direct deposit: from cloud produce a", "i1", category="income")
    _txn(conn, a, "2026-02-02", 90000, "interest", "i2", category="income")
    _txn(conn, a, "2026-02-03", -120000, "landlord", "s1", category="rent")
    _txn(conn, a, "2026-02-04", -30000, "loblaws", "s2", category="groceries")
    # ungrouped reimbursement inflow: reduces dining spend, is NOT income
    _txn(conn, a, "2026-02-05", -20000, "dinner", "s3", category="dining")
    _txn(conn, a, "2026-02-06", 8000, "etransfer from friend", "s4",
         category="dining", role_hint="reimbursement")
    mf = analytics.month_flows(conn, "2026-02", {"rent": "Housing"})

    row = conn.execute(
        "SELECT income_minor, spend_minor FROM v_monthly_cashflow WHERE month='2026-02' AND currency='CAD'"
    ).fetchone()
    assert mf.income_total_minor == row["income_minor"]
    assert mf.spend_total_minor == row["spend_minor"]
    # partition invariants
    assert sum(l.flow_minor for l in _links_to(mf, "inc:")) == mf.income_total_minor
    assert sum(l.flow_minor for l in _links_to(mf, "cat:")) == mf.spend_total_minor
    inc_to_grp = sum(l.flow_minor for l in _links_from(mf, "inc:") if l.target.startswith("grp:"))
    assert inc_to_grp + max(mf.savings_minor, 0) == mf.income_total_minor


def test_month_flows_ungrouped_reimbursement_nets_category(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-03-01", 100000, "direct deposit: from cloud produce a", "i1", category="income")
    _txn(conn, a, "2026-03-02", -10000, "dinner", "s1", category="dining")
    _txn(conn, a, "2026-03-03", 4000, "etransfer from friend", "s2",
         category="dining", role_hint="reimbursement")
    mf = analytics.month_flows(conn, "2026-03", {})
    dining = [l for l in mf.links if l.target == "cat:dining"]
    assert len(dining) == 1 and dining[0].flow_minor == 6000  # 100.00 - 40.00
    # reimbursement is not on the income side
    assert all("reimburs" not in mf.labels[l.source].lower() for l in _links_to(mf, "inc:"))


def test_month_flows_overspent_month(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-04-01", 100000, "direct deposit: from cloud produce a", "i1", category="income")
    _txn(conn, a, "2026-04-02", -150000, "landlord", "s1", category="rent")
    mf = analytics.month_flows(conn, "2026-04", {"rent": "Housing"})
    assert mf.savings_minor == -50000
    assert "sav:Savings" not in mf.labels
    assert not any(l.target == "sav:Savings" for l in mf.links)
    row = conn.execute(
        "SELECT income_minor, spend_minor FROM v_monthly_cashflow WHERE month='2026-04' AND currency='CAD'"
    ).fetchone()
    assert mf.income_total_minor == row["income_minor"]
    assert mf.spend_total_minor == row["spend_minor"]


def test_month_flows_uncategorized_and_unmapped_fallback(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-05-01", 100000, "direct deposit: from cloud produce a", "i1", category="income")
    _txn(conn, a, "2026-05-02", -5000, "mystery", "s1")               # uncategorized
    _txn(conn, a, "2026-05-03", -7000, "arcade", "s2", category="entertainment")  # unmapped
    mf = analytics.month_flows(conn, "2026-05", {})  # empty mapping -> all "Other"
    assert "grp:Other" in mf.labels
    assert "cat:(uncategorized)" in mf.labels
    # both land under Other
    other_cats = {l.target for l in mf.links if l.source == "grp:Other"}
    assert "cat:(uncategorized)" in other_cats
    assert "cat:entertainment" in other_cats


def test_month_flows_dominant_currency(conn):
    a = insert_account(conn, key="cad", currency="CAD")
    b = insert_account(conn, key="usd", currency="USD")
    _txn(conn, a, "2026-06-01", 100000, "direct deposit: from cloud produce a", "i1", category="income")
    _txn(conn, a, "2026-06-02", -50000, "landlord", "s1", category="rent")
    # tiny USD activity
    insert_raw_txn(conn, b, posted_date="2026-06-03", amount_minor=3, currency="USD",
                   description_raw="stock lending", description_norm="stock lending earnings", dedup_key="u1")
    conn.commit()
    mf = analytics.month_flows(conn, "2026-06", {"rent": "Housing"})
    assert mf.currency == "CAD"
    assert mf.other_currencies == ["USD"]
    assert all("USD" not in k for k in mf.labels)


def test_month_flows_negative_net_category_omitted(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-08-01", 100000, "direct deposit: from cloud produce a", "i1", category="income")
    _txn(conn, a, "2026-08-02", -5000, "dinner", "s1", category="dining")
    # reimbursement EXCEEDS the outflow -> dining nets negative
    _txn(conn, a, "2026-08-03", 8000, "etransfer from friend", "s2",
         category="dining", role_hint="reimbursement")
    mf = analytics.month_flows(conn, "2026-08", {})
    assert not any(l.target == "cat:dining" for l in mf.links)  # unrenderable negative band omitted
    # totals still reflect the view (documented visual-vs-total discrepancy)
    row = conn.execute(
        "SELECT spend_minor FROM v_monthly_cashflow WHERE month='2026-08' AND currency='CAD'"
    ).fetchone()
    assert mf.spend_total_minor == row["spend_minor"]


def test_month_flows_empty_month_none(conn):
    insert_account(conn)
    assert analytics.month_flows(conn, "2099-01", {}) is None
