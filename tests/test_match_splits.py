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


# ---- always-re-derive: allocations are a pure function of full history ----

def _period_of(conn, txn_id):
    return conn.execute(
        """SELECT g.period_key, g.status FROM group_members gm JOIN groups g ON g.id=gm.group_id
           WHERE gm.raw_txn_id = ?""", (txn_id,)
    ).fetchone()


def test_backfill_self_heals_allocation(rent_db):
    """A reimbursement allocated while history was partial must move to the
    correct (older) period on the next plain run after a backfill import."""
    conn, ids = rent_db
    ws, td = ids["ws-cash"], ids["td-chequing"]

    # Partial history: only February's expense is visible when the late-January
    # payment arrives, so FIFO can only park it against February.
    _expense(conn, ws, date_s="2026-02-01", dedup="e-feb")
    pay = _reimb(conn, td, date_s="2026-01-20", dedup="r-jan")
    splits.match_splits(conn, today=date(2026, 2, 10))
    assert _period_of(conn, pay)["period_key"] == "2026-02"

    # Backfill lands January's expense; the next ordinary run re-derives and
    # the payment settles January — no flag, no manual intervention.
    _expense(conn, ws, date_s="2026-01-01", dedup="e-jan")
    splits.match_splits(conn, today=date(2026, 2, 10))
    got = _period_of(conn, pay)
    assert got["period_key"] == "2026-01"
    assert got["status"] == "settled"


def test_rederive_preserves_transfer_groups(rent_db):
    """Re-deriving splits must not touch generic transfer groups."""
    from bankapp.match import transfers

    conn, ids = rent_db
    out = _add(conn, ids["td-chequing"], "2026-01-05", -50000, "TFR-TO WS", "gt-out")
    inn = _add(conn, ids["ws-cash"], "2026-01-05", 50000, "TFR-FR TD", "gt-in")
    for rid in (out, inn):
        conn.execute(
            "INSERT INTO txn_interp(raw_txn_id, role_hint, updated_at) "
            "VALUES (?, 'transfer', 't')", (rid,)
        )
    transfers.match_transfers(conn, window_days=7, tolerance_minor=0)
    before = conn.execute("SELECT COUNT(*) FROM groups WHERE type='transfer'").fetchone()[0]
    assert before == 1

    splits.match_splits(conn, today=date(2026, 1, 15))
    after = conn.execute("SELECT COUNT(*) FROM groups WHERE type='transfer'").fetchone()[0]
    assert after == 1


def test_match_all_rebuild_lets_split_reclaim_legs(rent_db):
    """The `match all --rebuild` sequence (clear generic groups -> splits ->
    transfers) must let a link_transfer template reclaim legs that an earlier
    run had paired into a generic transfer group."""
    from bankapp.match import transfers

    conn, ids = rent_db
    ws, td = ids["ws-cash"], ids["td-chequing"]

    # The rent chain's own TD->WS legs, hinted as transfers.
    out, inn = _transfer_pair(conn, ws, td)
    for rid in (out, inn):
        conn.execute(
            "INSERT INTO txn_interp(raw_txn_id, role_hint, updated_at) "
            "VALUES (?, 'transfer', 't')", (rid,)
        )
    # A run where the generic matcher grabbed them first (e.g. expense not yet synced).
    transfers.match_transfers(conn, window_days=7, tolerance_minor=0)
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE type='transfer'").fetchone()[0] == 1

    # Full history arrives; run the match-all --rebuild sequence.
    _expense(conn, ws)
    _reimb(conn, td)
    with conn:
        transfers.clear_generic_groups(conn)
    splits.match_splits(conn, today=date(2026, 1, 15))
    transfers.match_transfers(conn, window_days=7, tolerance_minor=0)

    roles = [r[0] for r in conn.execute(
        """SELECT gm.role FROM group_members gm JOIN groups g ON g.id=gm.group_id
           WHERE g.type='split_expense' ORDER BY gm.role"""
    )]
    assert roles == ["expense", "reimbursement", "transfer_in", "transfer_out"]
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE type='transfer'").fetchone()[0] == 0


def test_rederive_skips_wipe_when_no_templates(conn):
    """If no active template loads, existing split groups must survive the run
    (a config hiccup must not transiently un-share every historical expense)."""
    from tests.conftest import insert_account

    ws = insert_account(conn, key="ws-cash", institution="wealthsimple", type="cash")
    insert_account(conn, key="td-chequing", institution="td", type="chequing")
    splits.upsert_templates(conn, [RENT])
    tid = conn.execute("SELECT id FROM recurring_templates WHERE name='rent'").fetchone()[0]
    _add(conn, ws, "2026-01-01", -240000, "LANDLORD RENT PAYMENT", "e1")
    splits.match_splits(conn, today=date(2026, 1, 15))
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE type='split_expense'").fetchone()[0] > 0

    conn.execute("UPDATE recurring_templates SET active = 0 WHERE id = ?", (tid,))
    conn.commit()
    splits.match_splits(conn, today=date(2026, 1, 15))
    survived = conn.execute("SELECT COUNT(*) FROM groups WHERE type='split_expense'").fetchone()[0]
    assert survived > 0


# ---- multiple reimburse accounts ----

def test_reimbursement_claimed_from_second_account(conn):
    """A rent payment landing in a second watched account (e.g. WS cash instead of
    TD chequing) must still be claimed into the period group and settle it."""
    from tests.conftest import insert_account

    ws = insert_account(conn, key="ws-cash", institution="wealthsimple", type="cash")
    insert_account(conn, key="td-chequing", institution="td", type="chequing")
    multi = dataclasses.replace(RENT, reimburse_account="td-chequing,ws-cash")
    splits.upsert_templates(conn, [multi])

    _add(conn, ws, "2026-01-01", -240000, "LANDLORD RENT PAYMENT", "e1")
    pay = _add(conn, ws, "2026-01-03", 120000, "ETRANSFER FROM ROOMMATE VANESSA", "r1")
    splits.match_splits(conn, today=date(2026, 1, 15))

    got = conn.execute(
        """SELECT g.period_key, g.status, gm.role FROM group_members gm
           JOIN groups g ON g.id=gm.group_id WHERE gm.raw_txn_id = ?""", (pay,)
    ).fetchone()
    assert got is not None and got["role"] == "reimbursement"
    assert got["period_key"] == "2026-01"
    assert got["status"] == "settled"


def test_self_transfer_inflow_never_claimed_as_reimbursement(conn):
    """An inflow already role-hinted 'transfer' (moving my own money between my
    accounts) must not be FIFO-claimed as a roommate reimbursement, even when it
    matches the reimburser pattern and clears the amount gate."""
    from tests.conftest import insert_account

    ws = insert_account(conn, key="ws-cash", institution="wealthsimple", type="cash")
    insert_account(conn, key="td-chequing", institution="td", type="chequing")
    multi = dataclasses.replace(
        RENT, reimburse_account="td-chequing,ws-cash",
        reimburser_pattern="e-transfer", reimburse_min_minor=90000,
    )
    splits.upsert_templates(conn, [multi])

    _add(conn, ws, "2026-01-01", -240000, "LANDLORD RENT PAYMENT", "e1")
    mine = _add(conn, ws, "2026-01-02", 150000, "E-TRANSFER FROM GRAHAM METCALFE", "self1")
    conn.execute(
        "INSERT INTO txn_interp(raw_txn_id, role_hint, updated_at) VALUES (?, 'transfer','t')", (mine,)
    )
    theirs = _add(conn, ws, "2026-01-03", 120000, "E-TRANSFER FROM VANESSA PEARCE", "roomie1")
    splits.match_splits(conn, today=date(2026, 1, 15))

    claimed = [r[0] for r in conn.execute(
        """SELECT r.dedup_key FROM group_members gm JOIN raw_txn r ON r.id=gm.raw_txn_id
           WHERE gm.role='reimbursement'"""
    )]
    assert claimed == ["roomie1"]
