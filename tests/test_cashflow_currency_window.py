"""Regression: v_monthly_cashflow yields one row per (month, currency), so any
"last N" window must count MONTHS, not rows. A single foreign-currency row used
to both duplicate a month label and evict the oldest real month from the window.
"""

from datetime import date
from types import SimpleNamespace

from bankapp.report import advisor
from tests.conftest import insert_account, insert_raw_txn

CFG = SimpleNamespace(leak_threshold_minor=1500)


def _txn(conn, acct, date_s, amt, desc, dedup, category=None, currency="CAD"):
    tid = insert_raw_txn(conn, acct, posted_date=date_s, amount_minor=amt, currency=currency,
                         description_raw=desc, description_norm=desc.lower(), dedup_key=dedup)
    if category:
        conn.execute("INSERT INTO txn_interp(raw_txn_id, category, updated_at) VALUES (?,?,'t')", (tid, category))
    conn.commit()
    return tid


def _seven_months_with_a_usd_penny(conn):
    """2026-01..2026-07 of CAD activity, plus a 1-cent USD row inside 2026-03.

    Mirrors the live DB: 'stock lending earnings' USD 0.01 lands in one month.
    """
    a = insert_account(conn, key="td-chequing")
    u = insert_account(conn, key="ws-usd", institution="wealthsimple", type="cash", currency="USD")
    for i, mo in enumerate(["01", "02", "03", "04", "05", "06", "07"]):
        _txn(conn, a, f"2026-{mo}-05", 500000, "payroll", f"in-{mo}")
        _txn(conn, a, f"2026-{mo}-06", -100000, "spend", f"out-{mo}")
    _txn(conn, u, "2026-03-12", 1, "stock lending earnings", "usd-1", currency="USD")
    return a, u


# ---- the window counts months, not rows -------------------------------------

def test_monthly_cashflow_window_counts_distinct_months(conn):
    _seven_months_with_a_usd_penny(conn)
    rows = advisor.monthly_cashflow(conn, months=6)
    months = sorted({r.month for r in rows})
    assert months == ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]
    # the in-window foreign-currency row is retained, not dropped
    assert ("2026-03", "USD") in {(r.month, r.currency) for r in rows}


def test_monthly_cashflow_window_evicts_only_whole_months(conn):
    _seven_months_with_a_usd_penny(conn)
    rows = advisor.monthly_cashflow(conn, months=1)
    assert sorted({r.month for r in rows}) == ["2026-07"]


def test_monthly_cashflow_rows_sorted_by_month_then_currency(conn):
    _seven_months_with_a_usd_penny(conn)
    rows = advisor.monthly_cashflow(conn)
    keys = [(r.month, r.currency) for r in rows]
    assert keys == sorted(keys)  # deterministic; the USD row never precedes CAD in its month


def test_monthly_cashflow_unwindowed_returns_every_row(conn):
    _seven_months_with_a_usd_penny(conn)
    rows = advisor.monthly_cashflow(conn)
    assert len(rows) == 8  # 7 CAD months + 1 USD row


def test_monthly_cashflow_zero_or_negative_months_is_empty(conn):
    """'The last 0 months' is nothing. (The old row-slice `out[-0:]` returned everything.)"""
    _seven_months_with_a_usd_penny(conn)
    assert advisor.monthly_cashflow(conn, months=0) == []
    assert advisor.monthly_cashflow(conn, months=-1) == []


# ---- the digest window (what the dashboard chart plots) ---------------------

def test_digest_savings_keeps_six_distinct_months(conn):
    _seven_months_with_a_usd_penny(conn)
    d = advisor.digest(conn, CFG, today=date(2026, 7, 9))
    months = sorted({s["month"] for s in d["savings"]})
    # 2026-02 must NOT be evicted by the 1-cent USD row sharing 2026-03
    assert months == ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]


# ---- the advisor brief's "This month" --------------------------------------

def test_brief_this_month_uses_the_digest_month_not_the_last_row(conn):
    """No activity yet in the digest's month: the last row is June, and reporting it
    under '## This month' would silently pass off last month's numbers as this month's."""
    a = insert_account(conn, key="td-chequing")
    _txn(conn, a, "2026-06-05", 400000, "payroll", "jun-in")
    d = advisor.digest(conn, CFG, today=date(2026, 7, 9))
    md = advisor.render_digest_markdown(d)
    assert "4000.00 CAD" not in md                       # June is not "this month"
    assert "no activity recorded yet this month" in md


def test_brief_this_month_labels_foreign_currency_correctly(conn):
    a = insert_account(conn, key="td-chequing")
    u = insert_account(conn, key="ws-usd", institution="wealthsimple", type="cash", currency="USD")
    _txn(conn, a, "2026-07-05", 500000, "payroll", "jul-cad")
    _txn(conn, u, "2026-07-12", 1, "stock lending earnings", "jul-usd", currency="USD")
    d = advisor.digest(conn, CFG, today=date(2026, 7, 9))
    md = advisor.render_digest_markdown(d)
    assert "income 0.01 USD" in md      # the USD row is reported in USD...
    assert "0.01 CAD" not in md         # ...never mislabelled as CAD
    assert "income 5000.00 CAD" in md   # and the CAD row still reported


# ---- budgets are currency-scoped -------------------------------------------

def test_budget_status_excludes_foreign_currency_spend(conn):
    a = insert_account(conn, key="td-chequing")
    u = insert_account(conn, key="ws-usd", institution="wealthsimple", type="cash", currency="USD")
    advisor.upsert_budgets(conn, {"groceries": 60000}, currency="CAD")
    _txn(conn, a, "2026-07-03", -12000, "safeway", "cad-groc", category="groceries")
    _txn(conn, u, "2026-07-04", -9900, "us safeway", "usd-groc", category="groceries", currency="USD")

    rows = {r.category: r for r in advisor.budget_status(conn, "2026-07", today=date(2026, 7, 9))}
    assert rows["groceries"].actual_minor == 12000  # the 99.00 USD charge is not CAD spend


def test_budget_status_unbudgeted_list_is_currency_scoped(conn):
    u = insert_account(conn, key="ws-usd", institution="wealthsimple", type="cash", currency="USD")
    _txn(conn, u, "2026-07-04", -9900, "us pharmacy", "usd-health", category="health", currency="USD")
    rows = advisor.budget_status(conn, "2026-07", today=date(2026, 7, 9))
    assert "health" not in {r.category for r in rows}  # USD-only spend never appears in a CAD budget view
