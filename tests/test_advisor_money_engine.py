from datetime import date

from bankapp.report import advisor
from tests.conftest import insert_account, insert_raw_txn


def _txn(conn, acct, date_s, amt, desc, dedup, category=None):
    tid = insert_raw_txn(conn, acct, posted_date=date_s, amount_minor=amt,
                         description_raw=desc, description_norm=desc.lower(), dedup_key=dedup)
    if category:
        conn.execute("INSERT INTO txn_interp(raw_txn_id, category, updated_at) VALUES (?,?,'t')", (tid, category))
    conn.commit()
    return tid


# ---- T9.1 cashflow / savings ----

def test_cashflow_excludes_transfers(conn):
    a = insert_account(conn, key="td-chequing")
    b = insert_account(conn, key="ws-cash", institution="wealthsimple", type="cash")
    _txn(conn, a, "2026-01-05", 500000, "payroll", "d1")       # income
    _txn(conn, a, "2026-01-06", -100000, "rent-ish", "d2")     # spend
    # a transfer pair that should NOT count as income/spend
    o = _txn(conn, a, "2026-01-10", -50000, "tfr-to", "d3")
    i = _txn(conn, b, "2026-01-10", 50000, "tfr-fr", "d4")
    conn.execute("INSERT INTO txn_interp(raw_txn_id, role_hint, updated_at) VALUES (?, 'transfer','t')", (o,))
    conn.execute("INSERT INTO txn_interp(raw_txn_id, role_hint, updated_at) VALUES (?, 'transfer','t')", (i,))
    conn.execute("INSERT INTO groups(id,type,status,created_at,updated_at) VALUES (1,'transfer','matched','t','t')")
    conn.execute("INSERT INTO group_members(group_id,raw_txn_id,role) VALUES (1,?, 'transfer_out')", (o,))
    conn.execute("INSERT INTO group_members(group_id,raw_txn_id,role) VALUES (1,?, 'transfer_in')", (i,))
    conn.commit()

    rows = {r.month: r for r in advisor.monthly_cashflow(conn)}
    jan = rows["2026-01"]
    assert jan.income_minor == 500000   # transfer inflow excluded
    assert jan.spend_minor == 100000    # transfer outflow excluded


def test_savings_rate_zero_income_no_div_by_zero(conn):
    a = insert_account(conn, key="td-chequing")
    _txn(conn, a, "2026-01-06", -100000, "spend", "d1")
    jan = advisor.monthly_cashflow(conn)[0]
    assert jan.income_minor == 0
    assert jan.savings_rate == 0.0  # no ZeroDivisionError


# ---- T9.2 budgets ----

def test_budget_upsert_idempotent(conn):
    assert advisor.upsert_budgets(conn, {"groceries": 60000}) == 1
    advisor.upsert_budgets(conn, {"groceries": 70000})
    row = conn.execute("SELECT monthly_limit_minor FROM budgets WHERE category='groceries'").fetchone()[0]
    assert row == 70000
    assert conn.execute("SELECT COUNT(*) FROM budgets").fetchone()[0] == 1


def test_budget_over_under_and_pace(conn):
    a = insert_account(conn, key="td-chequing")
    advisor.upsert_budgets(conn, {"groceries": 60000, "dining": 25000})
    _txn(conn, a, "2026-01-05", -70000, "loblaws", "d1", category="groceries")  # over 60000
    _txn(conn, a, "2026-01-05", -20000, "restaurant", "d2", category="dining")  # under, but pace at mid-month
    rows = {r.category: r for r in advisor.budget_status(conn, "2026-01", today=date(2026, 1, 15))}
    assert rows["groceries"].over is True
    assert rows["dining"].over is False
    # 20000/25000 = 80% spent, ~48% through month -> pace warn
    assert rows["dining"].pace_warn is True


def test_budget_unbudgeted_listed_separately(conn):
    a = insert_account(conn, key="td-chequing")
    advisor.upsert_budgets(conn, {"groceries": 60000})
    _txn(conn, a, "2026-01-05", -3000, "random shop", "d1", category="shopping")
    rows = advisor.budget_status(conn, "2026-01", today=date(2026, 1, 31))
    unbudgeted = [r for r in rows if r.limit_minor is None]
    assert any(r.category == "shopping" and r.actual_minor == 3000 for r in unbudgeted)


# ---- T9.3 subscriptions + leaks (pure) ----

def _sub_charges(amount, dates):
    return [(d, amount, "netflix.com monthly", "CAD") for d in dates]


def test_detects_monthly_subscription():
    txns = _sub_charges(-1599, ["2026-01-03", "2026-02-02", "2026-03-04", "2026-04-03"])
    subs = advisor.detect_subscriptions(txns)
    assert len(subs) == 1
    assert subs[0].merchant == "netflix.com"
    assert subs[0].cadence == "monthly"
    assert subs[0].monthly_cost_minor == 1599


def test_jittered_dates_within_tolerance_still_detected():
    txns = _sub_charges(-1599, ["2026-01-01", "2026-01-29", "2026-03-02", "2026-04-01"])
    assert len(advisor.detect_subscriptions(txns)) == 1


def test_price_creep_flagged():
    txns = [
        ("2026-01-03", -1500, "spotify premium", "CAD"),
        ("2026-02-03", -1500, "spotify premium", "CAD"),
        ("2026-03-03", -1550, "spotify premium", "CAD"),  # latest > trailing median
    ]
    subs = advisor.detect_subscriptions(txns)
    assert subs[0].price_creep is True


def test_one_off_not_flagged():
    txns = [("2026-01-03", -5000, "one time thing", "CAD")]
    assert advisor.detect_subscriptions(txns) == []


def test_leak_aggregation_and_fees():
    txns = [
        ("2026-01-03", -400, "tim hortons", None, "CAD"),
        ("2026-01-10", -350, "tim hortons", None, "CAD"),
        ("2026-01-15", -5000, "big purchase", None, "CAD"),   # over threshold, not a fee -> excluded
        ("2026-01-20", -1200, "monthly fee", "fees", "CAD"),  # fee always included
    ]
    rows = advisor.leak_report(txns, threshold_minor=1500)
    by_merchant = {r.merchant: r for r in rows}
    assert by_merchant["tim"].total_minor == 750
    assert by_merchant["tim"].count == 2
    assert "monthly" in by_merchant  # the fee row
    assert "big" not in by_merchant
