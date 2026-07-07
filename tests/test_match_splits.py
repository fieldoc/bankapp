import dataclasses
from datetime import date

import pytest

from bankapp.config import TemplateConfig
from bankapp.match import splits
from tests.conftest import insert_account, insert_raw_txn

RENT = TemplateConfig(
    name="rent", kind="split_expense", expected_amount_minor=240000, currency="CAD",
    share_numer=1, share_denom=2, day_of_month=1, expense_account="ws-cash",
    expense_pattern="landlord", reimburse_account="td-chequing",
    reimburser_pattern="etransfer from roommate", amount_tolerance_minor=500,
    window_days=45, link_transfer=True, cadence="monthly",
)


@pytest.fixture
def rent_db(conn):
    ws = insert_account(conn, key="ws-cash", institution="wealthsimple", type="cash")
    td = insert_account(conn, key="td-chequing", institution="td", type="chequing")
    splits.upsert_templates(conn, [RENT])
    return conn, {"ws-cash": ws, "td-chequing": td}


def _add(conn, acct_id, date_s, amount, desc, dedup):
    return insert_raw_txn(
        conn, acct_id, posted_date=date_s, amount_minor=amount,
        description_raw=desc, description_norm=desc.lower(), dedup_key=dedup,
    )


def _expense(conn, ws, date_s="2026-01-01", amt=-240000, dedup="e1"):
    return _add(conn, ws, date_s, amt, "LANDLORD RENT PAYMENT", dedup)


def _reimb(conn, td, date_s="2026-01-03", amt=120000, dedup="r1"):
    return _add(conn, td, date_s, amt, "ETRANSFER FROM ROOMMATE JOHN", dedup)


def _transfer_pair(conn, ws, td, date_s="2026-01-01"):
    out = _add(conn, td, date_s, -240000, "TFR-TO WEALTHSIMPLE", "tout")
    inn = _add(conn, ws, date_s, 240000, "TRANSFER FROM TD", "tin")
    return out, inn


# ---- T6.2 expense leg + share ----

def test_expense_attached_with_floored_share(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"])
    splits.match_splits(conn, today=date(2026, 1, 15))
    row = conn.execute(
        "SELECT role, share_amount_minor FROM group_members WHERE role='expense'"
    ).fetchone()
    assert row["share_amount_minor"] == 120000  # 240000 * 1/2


def test_odd_cent_share_floors(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"], amt=-240001, dedup="e_odd")
    # tolerance 500 so 240001 is within tolerance of 240000 -> not anomaly
    splits.match_splits(conn, today=date(2026, 1, 15))
    share = conn.execute("SELECT share_amount_minor FROM group_members WHERE role='expense'").fetchone()[0]
    assert share == 120000  # floor(240001/2)


def test_amount_anomaly_attached_not_dropped(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"], amt=-250000, dedup="e_big")  # $100 over, > tolerance
    splits.match_splits(conn, today=date(2026, 1, 15))
    g = conn.execute("SELECT status FROM groups WHERE type='split_expense'").fetchone()
    assert g["status"] == "amount_anomaly"
    # still attached (not lost)
    assert conn.execute("SELECT COUNT(*) FROM group_members WHERE role='expense'").fetchone()[0] == 1


def test_missing_expense_after_grace(rent_db):
    conn, ids = rent_db
    # a reimburse-account txn exists (so periods have a data anchor) but no expense
    _add(conn, ids["td-chequing"], "2026-01-02", 5000, "SOMETHING ELSE", "x1")
    splits.match_splits(conn, today=date(2026, 1, 20))  # well past Jan 1 + grace
    g = conn.execute("SELECT status FROM groups WHERE period_key='2026-01'").fetchone()
    assert g["status"] == "missing_expense"


def test_idempotent_rerun(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"])
    _reimb(conn, ids["td-chequing"])
    splits.match_splits(conn, today=date(2026, 1, 15))
    n1 = conn.execute("SELECT COUNT(*) FROM group_members").fetchone()[0]
    splits.match_splits(conn, today=date(2026, 1, 15))
    assert conn.execute("SELECT COUNT(*) FROM group_members").fetchone()[0] == n1


# ---- T6.3 reimbursement + statuses + receivables ----

def test_settled(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"])
    _reimb(conn, ids["td-chequing"])
    splits.match_splits(conn, today=date(2026, 1, 15))
    r = conn.execute("SELECT status, outstanding_minor FROM v_receivables").fetchone()
    assert r["status"] == "settled"
    assert r["outstanding_minor"] == 0


def test_underpaid_with_outstanding_and_aging(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"])
    _reimb(conn, ids["td-chequing"], amt=115000, dedup="r_short")  # $50 short
    splits.match_splits(conn, today=date(2026, 3, 1))  # past 45-day window
    r = conn.execute("SELECT status, outstanding_minor, age_days FROM v_receivables").fetchone()
    assert r["status"] == "underpaid"
    assert r["outstanding_minor"] == 5000
    assert r["age_days"] > 45


def test_next_month_payment_settles_prior_period(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"], date_s="2026-01-01")
    # roommate pays in February (within Jan's 45-day window) -> settles January
    _reimb(conn, ids["td-chequing"], date_s="2026-02-05", amt=120000, dedup="r_late")
    splits.match_splits(conn, today=date(2026, 2, 10))
    jan = conn.execute("SELECT status FROM groups WHERE period_key='2026-01'").fetchone()
    assert jan["status"] == "settled"


def test_fifo_oldest_period_first(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"], date_s="2026-01-01", dedup="e_jan")
    _expense(conn, ids["ws-cash"], date_s="2026-02-01", dedup="e_feb")
    # one payment, eligible for both windows -> settles the OLDER (January)
    _reimb(conn, ids["td-chequing"], date_s="2026-02-03", amt=120000, dedup="r_one")
    splits.match_splits(conn, today=date(2026, 2, 15))
    jan = conn.execute("SELECT status FROM groups WHERE period_key='2026-01'").fetchone()["status"]
    feb = conn.execute("SELECT status FROM groups WHERE period_key='2026-02'").fetchone()["status"]
    assert jan == "settled"
    assert feb in ("open", "underpaid")


def test_partial_payments_accumulate(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"])
    _reimb(conn, ids["td-chequing"], date_s="2026-01-03", amt=60000, dedup="r_a")
    _reimb(conn, ids["td-chequing"], date_s="2026-01-10", amt=60000, dedup="r_b")
    splits.match_splits(conn, today=date(2026, 1, 20))
    r = conn.execute("SELECT status, received_minor, outstanding_minor FROM v_receivables").fetchone()
    assert r["received_minor"] == 120000
    assert r["outstanding_minor"] == 0
    assert r["status"] == "settled"


# ---- amount-gated reimbursement matching (anonymized e-transfer senders) ----

RENT_AMOUNT_GATED = dataclasses.replace(
    RENT,
    reimburser_pattern="e-transfer",      # TD anonymizes senders -> broad pattern...
    reimburse_min_minor=90000,            # ...gated by amount (>= $900)
)


def test_reimburse_min_gates_small_etransfers(rent_db):
    conn, ids = rent_db
    splits.upsert_templates(conn, [RENT_AMOUNT_GATED])
    _expense(conn, ids["ws-cash"])
    # small unrelated e-transfer inflows in the window must NOT be claimed as rent
    _add(conn, ids["td-chequing"], "2026-01-04", 5000, "E-TRANSFER ***abc", "small1")
    _add(conn, ids["td-chequing"], "2026-01-08", 20000, "E-TRANSFER ***def", "small2")
    # the roommate-sized one is claimed
    _add(conn, ids["td-chequing"], "2026-01-05", 120000, "E-TRANSFER ***kgt", "rent1")

    splits.match_splits(conn, today=date(2026, 1, 15))

    claimed = conn.execute(
        """SELECT r.dedup_key FROM group_members gm JOIN raw_txn r ON r.id = gm.raw_txn_id
           WHERE gm.role = 'reimbursement'"""
    ).fetchall()
    assert [c[0] for c in claimed] == ["rent1"]
    assert conn.execute("SELECT status FROM groups").fetchone()["status"] == "settled"


def test_reimburse_min_zero_keeps_old_behavior(rent_db):
    conn, ids = rent_db  # RENT has reimburse_min_minor=0 (default)
    _expense(conn, ids["ws-cash"])
    _reimb(conn, ids["td-chequing"], amt=60000, dedup="r_a")
    _reimb(conn, ids["td-chequing"], date_s="2026-01-10", amt=60000, dedup="r_b")
    splits.match_splits(conn, today=date(2026, 1, 15))
    n = conn.execute("SELECT COUNT(*) FROM group_members WHERE role='reimbursement'").fetchone()[0]
    assert n == 2  # partial payments still accumulate when no gate is set


def test_reimburse_min_survives_upsert_roundtrip(conn):
    splits.upsert_templates(conn, [RENT_AMOUNT_GATED])
    t = splits.load_templates(conn)[0]
    assert t.reimburse_min_minor == 90000


# ---- T6.4 transfer-leg linking ----

def test_transfer_legs_linked_four_members(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"])
    _transfer_pair(conn, ids["ws-cash"], ids["td-chequing"])
    _reimb(conn, ids["td-chequing"])
    splits.match_splits(conn, today=date(2026, 1, 15))
    roles = [r[0] for r in conn.execute("SELECT role FROM group_members ORDER BY role")]
    assert sorted(roles) == ["expense", "reimbursement", "transfer_in", "transfer_out"]
