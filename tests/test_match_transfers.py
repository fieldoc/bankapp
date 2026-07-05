from bankapp.match import transfers
from tests.conftest import insert_account, insert_raw_txn


def _hinted(conn, acct, date, amount, dedup):
    tid = insert_raw_txn(conn, acct, posted_date=date, amount_minor=amount,
                         description_raw="transfer", description_norm="transfer", dedup_key=dedup)
    conn.execute(
        "INSERT INTO txn_interp(raw_txn_id, role_hint, updated_at) VALUES (?, 'transfer', 't')",
        (tid,),
    )
    conn.commit()
    return tid


def test_persists_one_group_two_members(conn):
    a1 = insert_account(conn, key="td-chequing")
    a2 = insert_account(conn, key="ws-cash", institution="wealthsimple", type="cash")
    _hinted(conn, a1, "2026-01-15", -50000, "d1")
    _hinted(conn, a2, "2026-01-15", 50000, "d2")

    assert transfers.match_transfers(conn, 7, 0) == 1
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE type='transfer'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM group_members").fetchone()[0] == 2
    roles = {r[0] for r in conn.execute("SELECT role FROM group_members")}
    assert roles == {"transfer_out", "transfer_in"}


def test_rerun_is_noop(conn):
    a1 = insert_account(conn, key="td-chequing")
    a2 = insert_account(conn, key="ws-cash", institution="wealthsimple", type="cash")
    _hinted(conn, a1, "2026-01-15", -50000, "d1")
    _hinted(conn, a2, "2026-01-15", 50000, "d2")
    transfers.match_transfers(conn, 7, 0)
    assert transfers.match_transfers(conn, 7, 0) == 0
    assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 1


def test_late_counterpart_pairs_next_run(conn):
    a1 = insert_account(conn, key="td-chequing")
    a2 = insert_account(conn, key="ws-cash", institution="wealthsimple", type="cash")
    _hinted(conn, a2, "2026-01-15", 50000, "d2")  # only the inflow so far
    assert transfers.match_transfers(conn, 7, 0) == 0  # lone leg, pending

    _hinted(conn, a1, "2026-01-18", -50000, "d1")  # counterpart lands later
    assert transfers.match_transfers(conn, 7, 0) == 1


def test_pending_transfers_view_shows_lone_leg(conn):
    a1 = insert_account(conn, key="td-chequing")
    _hinted(conn, a1, "2026-01-15", -50000, "d1")
    transfers.match_transfers(conn, 7, 0)
    rows = conn.execute("SELECT id, age_days FROM v_pending_transfers").fetchall()
    assert len(rows) == 1
    assert rows[0]["age_days"] is not None


def test_effective_view_nets_transfer_to_zero(conn):
    a1 = insert_account(conn, key="td-chequing")
    a2 = insert_account(conn, key="ws-cash", institution="wealthsimple", type="cash")
    o = _hinted(conn, a1, "2026-01-15", -50000, "d1")
    i = _hinted(conn, a2, "2026-01-15", 50000, "d2")
    transfers.match_transfers(conn, 7, 0)
    total = conn.execute(
        "SELECT SUM(effective_minor) FROM v_effective WHERE id IN (?,?)", (o, i)
    ).fetchone()[0]
    assert total == 0


def test_rebuild_deletes_and_rematches(conn):
    a1 = insert_account(conn, key="td-chequing")
    a2 = insert_account(conn, key="ws-cash", institution="wealthsimple", type="cash")
    _hinted(conn, a1, "2026-01-15", -50000, "d1")
    _hinted(conn, a2, "2026-01-15", 50000, "d2")
    transfers.match_transfers(conn, 7, 0)
    assert conn.execute("SELECT COUNT(*) FROM group_members").fetchone()[0] == 2
    # rebuild -> old generic transfer groups deleted (CASCADE clears members), then rematched
    transfers.match_transfers(conn, 7, 0, rebuild=True)
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE type='transfer'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM group_members").fetchone()[0] == 2
