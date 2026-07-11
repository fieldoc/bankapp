"""Safe-to-spend projection: expected income (median of trailing complete months)
minus spent-so-far minus committed-remaining (unpaid split my-shares + subscriptions
predicted to charge later this month), floored at 0.
"""

from datetime import date

from typer.testing import CliRunner

from bankapp import db as dbmod
from bankapp.cli import app
from bankapp.config import TemplateConfig
from bankapp.match import splits
from bankapp.report import projection
from tests.conftest import insert_account, insert_raw_txn

runner = CliRunner()

RENT = TemplateConfig(
    name="rent", kind="split_expense", expected_amount_minor=100000, currency="CAD",
    share_numer=1, share_denom=2, day_of_month=1, expense_account="ws-cash",
    expense_pattern="landlord", reimburse_account="td-chequing",
    reimburser_pattern="etransfer from roommate", amount_tolerance_minor=500,
    window_days=45, link_transfer=True, cadence="monthly",
)


def _txn(conn, acct, date_s, amt, desc, dedup, currency="CAD"):
    return insert_raw_txn(
        conn, acct, posted_date=date_s, amount_minor=amt, currency=currency,
        description_raw=desc, description_norm=desc.lower(), dedup_key=dedup,
    )


# ---- A1: expected income = median of last 3 complete months -----------------

def test_expected_income_is_median_of_trailing_complete_months(conn):
    a = insert_account(conn)
    # A stale outlier month that must NOT be pulled into the trailing-3 window.
    _txn(conn, a, "2025-12-05", 999999, "payroll", "d-dec-in")
    _txn(conn, a, "2025-12-06", -100, "spend", "d-dec-out")
    incomes = {"2026-01": 500000, "2026-02": 520000, "2026-03": 510000}
    for mo, amt in incomes.items():
        _txn(conn, a, f"{mo}-05", amt, "payroll", f"d-{mo}-in")
        _txn(conn, a, f"{mo}-06", -1000, "spend", f"d-{mo}-out")
    # Current month (incomplete) — must not affect expected_income.
    _txn(conn, a, "2026-04-01", 1, "payroll", "d-apr-in")
    conn.commit()

    rows = projection.month_projection(conn, today=date(2026, 4, 2))
    row = next(r for r in rows if r.currency == "CAD")
    assert row.month == "2026-04"
    assert row.expected_income_minor == 510000  # median(500000, 520000, 510000)


# ---- A2: safe-to-spend floors at 0 -------------------------------------------

def test_safe_to_spend_floors_at_zero(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-01-05", 100000, "payroll", "d-jan-in")
    _txn(conn, a, "2026-04-02", -999999, "big spend", "d-apr-out")
    conn.commit()

    rows = projection.month_projection(conn, today=date(2026, 4, 5))
    row = next(r for r in rows if r.currency == "CAD")
    assert row.expected_income_minor == 100000
    assert row.spent_so_far_minor == 999999
    assert row.safe_to_spend_minor == 0  # never negative


# ---- A3: split-expense my-share, unpaid vs already grouped ------------------

def _rent_db(conn):
    ws = insert_account(conn, key="ws-cash", institution="wealthsimple", type="cash")
    td = insert_account(conn, key="td-chequing", institution="td", type="chequing")
    splits.upsert_templates(conn, [RENT])
    return ws, td


def test_committed_includes_unpaid_split_my_share(conn):
    _rent_db(conn)
    # No cashflow at all for CAD -- currency must still surface via the template.
    rows = projection.month_projection(conn, today=date(2026, 4, 10))
    row = next(r for r in rows if r.currency == "CAD")
    assert row.committed_remaining_minor == 50000  # floor(100000 * 1/2)
    assert row.safe_to_spend_minor == 0  # 0 income - 0 spent - 50000 committed, floored


def test_committed_excludes_split_already_grouped_this_month(conn):
    ws, td = _rent_db(conn)
    _txn(conn, ws, "2026-04-01", -100000, "LANDLORD RENT PAYMENT", "e-apr")
    conn.commit()
    splits.match_splits(conn, today=date(2026, 4, 10))  # posts the expense leg + group

    rows = projection.month_projection(conn, today=date(2026, 4, 10))
    row = next(r for r in rows if r.currency == "CAD")
    assert row.committed_remaining_minor == 0  # this month's expense is already posted


# ---- A3: subscription predicted to charge later this month ------------------

def test_committed_includes_subscription_due_later_this_month(conn):
    a = insert_account(conn)
    for i, d in enumerate(["2026-01-05", "2026-02-05", "2026-03-05"]):
        _txn(conn, a, d, -1599, "netflix.com", f"n{i}")
    conn.commit()

    # last_charge 2026-03-05 + 30d = 2026-04-04: lands in April, after "today".
    rows = projection.month_projection(conn, today=date(2026, 4, 2))
    row = next(r for r in rows if r.currency == "CAD")
    assert row.committed_remaining_minor == 1599


def test_committed_excludes_subscription_charged_already_or_due_next_month(conn):
    a = insert_account(conn)
    # Charged on the 1st of each month, including this month already -- next
    # predicted charge (2026-05-01) falls in NEXT month, not this one.
    for i, d in enumerate(["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"]):
        _txn(conn, a, d, -3000, "gym membership", f"g{i}")
    conn.commit()

    rows = projection.month_projection(conn, today=date(2026, 4, 2))
    row = next(r for r in rows if r.currency == "CAD")
    assert row.committed_remaining_minor == 0


def test_committed_includes_month_end_monthly_biller(conn):
    # Regression: a monthly biller charged on the 31st. A fixed 30-day step would
    # predict Jan-31 + 30d = Mar-2 and wrongly drop the ~Feb-28 charge from Feb's
    # projection. Calendar-month stepping lands it on Feb 28 (clamped).
    a = insert_account(conn)
    for i, d in enumerate(["2025-11-30", "2025-12-31", "2026-01-31"]):
        _txn(conn, a, d, -2000, "month-end biller", f"m{i}")
    conn.commit()

    rows = projection.month_projection(conn, today=date(2026, 2, 15))
    row = next(r for r in rows if r.currency == "CAD")
    assert row.committed_remaining_minor == 2000


def test_committed_counts_every_remaining_weekly_charge(conn):
    # A weekly sub can bill several more times before month-end; count them all,
    # not just the next one.
    a = insert_account(conn)
    for i, d in enumerate(["2026-03-04", "2026-03-11", "2026-03-18", "2026-03-25", "2026-04-01"]):
        _txn(conn, a, d, -1000, "weekly service", f"w{i}")
    conn.commit()

    # today Apr 2 -> remaining weekly charges Apr 8/15/22/29 = 4 x per-charge (~1000).
    rows = projection.month_projection(conn, today=date(2026, 4, 2))
    row = next(r for r in rows if r.currency == "CAD")
    assert row.committed_remaining_minor == 4000


# ---- CLI smoke test ----------------------------------------------------------

def test_cli_report_projection(app_env):
    runner.invoke(app, ["init"])
    conn = dbmod.connect(app_env["db"])
    acct = conn.execute("SELECT id FROM accounts WHERE key='td-chequing'").fetchone()[0]
    insert_raw_txn(conn, acct, posted_date="2026-01-05", amount_minor=500000,
                   description_raw="PAYROLL", description_norm="payroll", dedup_key="d0")
    conn.close()

    r = runner.invoke(app, ["report", "projection"])
    assert r.exit_code == 0, r.output
    assert "expected income" in r.output
    assert "spent so far" in r.output
    assert "committed" in r.output
    assert "safe to spend" in r.output
