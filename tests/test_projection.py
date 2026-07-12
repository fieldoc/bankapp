"""Safe-to-spend projection: expected income (median of trailing complete months)
minus spent-so-far minus committed-remaining (unpaid split my-shares + subscriptions
predicted to charge later this month), floored at 0.
"""

import calendar
from datetime import date

from typer.testing import CliRunner

from bankapp import db as dbmod
from bankapp import goals as goalsmod
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


# ---- B: four-bucket savings waterfall ----------------------------------------

def _fixed_goal(conn, name, monthly_minor, priority=100, currency="CAD", start_date="2026-01-01"):
    """A fixed_monthly goal whose ask is exactly monthly_minor (target_minor=0 is
    a legal perpetual bucket; allocation_pct=0 keeps funded_minor irrelevant to
    the ask, which for fixed_monthly is a flat pass-through of monthly_minor)."""
    return goalsmod.create(
        conn, name=name, target_minor=0, currency=currency, start_date=start_date,
        target_date=None, allocation_pct=0, funding_mode="fixed_monthly",
        monthly_minor=monthly_minor, priority=priority,
    )


def _target_goal(conn, name, ask_minor, today, priority=100, currency="CAD", start_date="2026-01-01"):
    """A target_date goal whose ask is exactly ask_minor: allocation_pct=0 keeps
    funded_minor at 0 regardless of ledger activity, and a target_date inside
    `today`'s month makes months_left == 1, so the whole remainder (target_minor)
    is asked for right now."""
    month_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    return goalsmod.create(
        conn, name=name, target_minor=ask_minor, currency=currency, start_date=start_date,
        target_date=month_end.isoformat(), allocation_pct=0, funding_mode="target_date",
        priority=priority,
    )


def _assert_safe_to_spend_invariant(row):
    expected = max(0, row.expected_income_minor - row.spent_so_far_minor
                    - row.committed_remaining_minor - row.savings_allocated_minor)
    assert row.safe_to_spend_minor == expected
    assert row.safe_to_spend_minor >= 0


# B1: tier order -- fixed_monthly funds before target_date regardless of priority.
def test_fixed_tier_funds_before_target_tier_regardless_of_priority(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-04-01", 40000, "payroll", "d-in")
    conn.commit()
    today = date(2026, 4, 5)
    # Fixed goal has a much HIGHER (later-funding) priority number than the target
    # goal, yet must still be funded first because its tier funds first.
    _fixed_goal(conn, "z-fixed", 40000, priority=900)
    _target_goal(conn, "a-target", 40000, today, priority=1)
    conn.commit()

    rows = projection.month_projection(conn, today=today)
    row = next(r for r in rows if r.currency == "CAD")
    by_name = {gf.name: gf for gf in row.goal_funding}
    assert by_name["z-fixed"].status == "funded"
    assert by_name["z-fixed"].allocated_minor == 40000
    assert by_name["a-target"].status == "starved"
    assert by_name["a-target"].allocated_minor == 0
    _assert_safe_to_spend_invariant(row)


def test_within_tier_orders_by_priority_then_name(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-04-01", 25000, "payroll", "d-in")
    conn.commit()
    today = date(2026, 4, 5)
    _fixed_goal(conn, "a-p10", 10000, priority=10)
    _fixed_goal(conn, "c-p20", 10000, priority=20)
    _fixed_goal(conn, "b-p20", 10000, priority=20)
    conn.commit()

    rows = projection.month_projection(conn, today=today)
    row = next(r for r in rows if r.currency == "CAD")
    assert [gf.name for gf in row.goal_funding] == ["a-p10", "b-p20", "c-p20"]
    _assert_safe_to_spend_invariant(row)


# B2: partial + starved statuses, exact amounts.
def test_partial_and_starved_goals(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-04-01", 15000, "payroll", "d-in")
    conn.commit()
    today = date(2026, 4, 5)
    _fixed_goal(conn, "goal-a", 10000, priority=10)
    _fixed_goal(conn, "goal-b", 10000, priority=20)
    _fixed_goal(conn, "goal-c", 10000, priority=30)
    conn.commit()

    rows = projection.month_projection(conn, today=today)
    row = next(r for r in rows if r.currency == "CAD")
    by_name = {gf.name: gf for gf in row.goal_funding}
    assert by_name["goal-a"].status == "funded"
    assert by_name["goal-a"].allocated_minor == 10000
    assert by_name["goal-b"].status == "partial"
    assert by_name["goal-b"].allocated_minor == 5000
    assert by_name["goal-c"].status == "starved"
    assert by_name["goal-c"].allocated_minor == 0
    assert row.safe_to_spend_minor == 0
    assert row.savings_shortfall_minor == 30000 - 15000
    _assert_safe_to_spend_invariant(row)


# B3: fully funded -- pool exceeds the total ask.
def test_all_goals_fully_funded_when_pool_exceeds_asks(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-04-01", 100000, "payroll", "d-in")
    conn.commit()
    today = date(2026, 4, 5)
    _fixed_goal(conn, "goal-a", 10000, priority=10)
    _target_goal(conn, "goal-b", 20000, today, priority=10)
    conn.commit()

    rows = projection.month_projection(conn, today=today)
    row = next(r for r in rows if r.currency == "CAD")
    assert all(gf.status == "funded" for gf in row.goal_funding)
    assert row.need_to_save_minor == 10000
    assert row.like_to_save_minor == 20000
    assert row.savings_allocated_minor == 30000
    assert row.safe_to_spend_minor == 100000 - 30000
    assert row.savings_shortfall_minor == 0
    _assert_safe_to_spend_invariant(row)


# B4: negative available -- every asking goal starves, safe floors at 0.
def test_negative_available_starves_every_goal(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-04-01", 10000, "payroll", "d-in")
    _txn(conn, a, "2026-04-02", -50000, "big spend", "d-out")
    conn.commit()
    today = date(2026, 4, 5)
    _fixed_goal(conn, "goal-a", 5000, priority=10)
    _target_goal(conn, "goal-b", 3000, today, priority=10)
    conn.commit()

    rows = projection.month_projection(conn, today=today)
    row = next(r for r in rows if r.currency == "CAD")
    assert (row.expected_income_minor - row.spent_so_far_minor
            - row.committed_remaining_minor) < 0
    assert all(gf.status == "starved" for gf in row.goal_funding)
    assert all(gf.allocated_minor == 0 for gf in row.goal_funding)
    assert row.safe_to_spend_minor == 0
    assert row.savings_shortfall_minor == 5000 + 3000
    # need_to_save/like_to_save are the SUM OF ASKS, not allocated -- every goal
    # here is starved (allocated_minor == 0), yet the tier totals still reflect
    # what was asked for.
    assert row.need_to_save_minor == 5000
    assert row.like_to_save_minor == 3000
    _assert_safe_to_spend_invariant(row)


# B5: invariant holds across every scenario above (each also asserted inline).
def test_safe_to_spend_invariant_across_scenarios(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-04-01", 15000, "payroll", "d-in")
    conn.commit()
    today = date(2026, 4, 5)
    _fixed_goal(conn, "goal-a", 10000, priority=10)
    _fixed_goal(conn, "goal-b", 10000, priority=20)
    _fixed_goal(conn, "goal-c", 10000, priority=30)
    conn.commit()

    rows = projection.month_projection(conn, today=today)
    for row in rows:
        _assert_safe_to_spend_invariant(row)


# B6: a goal with no target_date asks 0 and is excluded from goal_funding.
def test_zero_ask_goal_excluded_from_goal_funding(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-04-01", 50000, "payroll", "d-in")
    conn.commit()
    today = date(2026, 4, 5)
    goalsmod.create(
        conn, name="no-target", target_minor=100000, currency="CAD",
        start_date="2026-01-01", target_date=None, allocation_pct=100,
        funding_mode="target_date",
    )
    conn.commit()

    rows = projection.month_projection(conn, today=today)
    row = next(r for r in rows if r.currency == "CAD")
    assert row.goal_funding == []
    assert row.need_to_save_minor == 0
    assert row.like_to_save_minor == 0
    assert row.savings_allocated_minor == 0
    assert row.savings_shortfall_minor == 0
    assert row.safe_to_spend_minor == 50000


# B7: a goal's currency never draws another currency's pool; a goal-only currency
# with no cashflow activity still yields a ProjectionRow.
def test_goal_currency_isolated_and_goal_only_currency_yields_row(conn):
    a = insert_account(conn)
    _txn(conn, a, "2026-04-01", 50000, "payroll", "d-in")  # CAD income only
    conn.commit()
    today = date(2026, 4, 5)
    _fixed_goal(conn, "cad-goal", 10000, priority=10, currency="CAD")
    _fixed_goal(conn, "usd-goal", 999999, priority=10, currency="USD")
    conn.commit()

    rows = projection.month_projection(conn, today=today)
    by_cur = {r.currency: r for r in rows}
    assert "USD" in by_cur  # goal-only currency still surfaces a row

    cad_row = by_cur["CAD"]
    assert {gf.name for gf in cad_row.goal_funding} == {"cad-goal"}
    assert cad_row.goal_funding[0].status == "funded"
    assert cad_row.safe_to_spend_minor == 50000 - 10000

    usd_row = by_cur["USD"]
    assert {gf.name for gf in usd_row.goal_funding} == {"usd-goal"}
    assert usd_row.expected_income_minor == 0
    assert usd_row.goal_funding[0].status == "starved"


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
