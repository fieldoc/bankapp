"""End-to-end acceptance tests over synthetic fixtures (the spec's AT1-AT3)."""

import shutil

from typer.testing import CliRunner

from bankapp import db as dbmod
from bankapp.cli import app
from tests.conftest import FIXTURES, insert_raw_txn

runner = CliRunner()

_TXN_FIXTURES = ["td_chequing_jan.ofx", "td_visa_jan.qfx", "td_chequing_A.csv", "td_chequing_B.csv"]


def _row_count(db_path) -> int:
    conn = dbmod.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM raw_txn").fetchone()[0]
    conn.close()
    return n


def test_at1_reingest_zero_new_rows(app_env):
    """Ingest all (good) fixtures twice; count unchanged; second run inserts 0."""
    runner.invoke(app, ["init"])
    inbox = app_env["inbox"]
    for name in _TXN_FIXTURES:
        shutil.copy(FIXTURES / name, inbox / name)

    first = runner.invoke(app, ["ingest", str(inbox), "--account", "td-chequing"])
    assert first.exit_code == 0, first.output
    count_after_first = _row_count(app_env["db"])
    assert count_after_first > 0

    second = runner.invoke(app, ["ingest", str(inbox), "--account", "td-chequing"])
    assert second.exit_code == 0, second.output
    assert "TOTAL: 0 inserted" in second.output
    assert _row_count(app_env["db"]) == count_after_first


def test_at2_transfer_netted(app_env, memkeyring):
    """-$500 TD 'TFR-TO' + +$500 WS -> one transfer group, both rows kept, net effective 0."""
    runner.invoke(app, ["init"])

    # TD leg via CLI: td_chequing_jan.ofx has CHQFIT003 'TFR-TO ... TRANSFER TO WEALTHSIMPLE' -500.
    runner.invoke(app, ["ingest", str(FIXTURES / "td_chequing_jan.ofx")])

    # WS +500 counterpart (as if synced): insert directly on ws-cash with a 'tfr-fr' desc.
    conn = dbmod.connect(app_env["db"])
    ws_id = conn.execute("SELECT id FROM accounts WHERE key='ws-cash'").fetchone()[0]
    ws_leg = insert_raw_txn(
        conn, ws_id, posted_date="2026-01-18", amount_minor=50000, currency="CAD",
        description_raw="TFR-FR TD CHEQUING", description_norm="tfr-fr td chequing",
        dedup_key="wsid:at2-in", source="ws",
    )
    conn.close()

    # Categorize: seed rules 'tfr-to'/'tfr-fr' tag both legs role_hint=transfer.
    assert runner.invoke(app, ["categorize"]).exit_code == 0
    assert runner.invoke(app, ["match", "transfers"]).exit_code == 0

    conn = dbmod.connect(app_env["db"])
    groups = conn.execute("SELECT COUNT(*) FROM groups WHERE type='transfer'").fetchone()[0]
    members = conn.execute("SELECT COUNT(*) FROM group_members").fetchone()[0]
    td_leg = conn.execute(
        "SELECT id FROM raw_txn WHERE dedup_key='fitid:CHQFIT003'"
    ).fetchone()[0]
    net = conn.execute(
        "SELECT SUM(effective_minor) FROM v_effective WHERE id IN (?,?)", (td_leg, ws_leg)
    ).fetchone()[0]
    # both raw rows still present (kept)
    kept = conn.execute("SELECT COUNT(*) FROM raw_txn WHERE id IN (?,?)", (td_leg, ws_leg)).fetchone()[0]
    conn.close()

    assert groups == 1
    assert members == 2
    assert net == 0
    assert kept == 2


# ---- AT3: the 3-leg rent chain ----

def _seed_rent_rows(db_path, *, expense=-240000, reimb=120000, reimb_date="2026-01-03"):
    """Seed one month's rent chain directly (WS + transfer legs can't come from files)."""
    conn = dbmod.connect(db_path)
    ws = conn.execute("SELECT id FROM accounts WHERE key='ws-cash'").fetchone()[0]
    td = conn.execute("SELECT id FROM accounts WHERE key='td-chequing'").fetchone()[0]

    def add(acct, date_s, amt, desc, dedup, source="ws"):
        return insert_raw_txn(conn, acct, posted_date=date_s, amount_minor=amt, currency="CAD",
                              description_raw=desc, description_norm=desc.lower(),
                              dedup_key=dedup, source=source)

    add(ws, "2026-01-01", expense, "LANDLORD RENT PAYMENT", "wsid:exp")            # expense (full rent)
    add(td, "2026-01-01", -240000, "TFR-TO WEALTHSIMPLE", "fitid:tout", "ofx")     # transfer out
    add(ws, "2026-01-01", 240000, "TRANSFER FROM TD", "wsid:tin")                  # transfer in
    add(td, reimb_date, reimb, "ETRANSFER FROM ROOMMATE JOHN", "fitid:reimb", "ofx")  # roommate
    conn.close()


def test_at3_rent_chain(app_env, memkeyring):
    """Rent chain -> one group with 4 members; Jan spend == -my_share; receivable settled."""
    runner.invoke(app, ["init"])   # upserts the rent template from config
    _seed_rent_rows(app_env["db"])
    assert runner.invoke(app, ["match", "all"]).exit_code == 0

    conn = dbmod.connect(app_env["db"])
    gid = conn.execute("SELECT id FROM groups WHERE type='split_expense' AND period_key='2026-01'").fetchone()[0]
    members = conn.execute("SELECT COUNT(*) FROM group_members WHERE group_id=?", (gid,)).fetchone()[0]
    jan_spend = conn.execute(
        "SELECT net_minor FROM v_monthly_cashflow WHERE month='2026-01'"
    ).fetchone()[0]
    rec = conn.execute("SELECT status, outstanding_minor FROM v_receivables WHERE group_id=?", (gid,)).fetchone()
    conn.close()

    assert members == 4
    assert jan_spend == -120000       # my 50% share only
    assert rec["status"] == "settled"
    assert rec["outstanding_minor"] == 0


def test_at3_underpaid(app_env, memkeyring):
    """Roommate pays X-50 -> underpaid, outstanding 5000 minor."""
    from datetime import date

    from bankapp import config as configmod
    from bankapp.match import splits

    runner.invoke(app, ["init"])
    _seed_rent_rows(app_env["db"], reimb=115000)  # $50 short

    cfg = configmod.load_config(app_env["config"])
    conn = dbmod.init_db(cfg.db_path)
    splits.match_splits(conn, today=date(2026, 3, 1))  # past the 45-day window
    r = conn.execute("SELECT status, outstanding_minor FROM v_receivables WHERE period_key='2026-01'").fetchone()
    conn.close()
    assert r["status"] == "underpaid"
    assert r["outstanding_minor"] == 5000


def test_at3_late_cross_month(app_env, memkeyring):
    """Roommate pays in February -> settles January (late-flagged while open, never lost)."""
    from datetime import date

    from bankapp import config as configmod
    from bankapp.match import splits

    runner.invoke(app, ["init"])
    _seed_rent_rows(app_env["db"], reimb=120000, reimb_date="2026-02-05")

    cfg = configmod.load_config(app_env["config"])
    conn = dbmod.init_db(cfg.db_path)
    splits.match_splits(conn, today=date(2026, 2, 10))
    jan = conn.execute("SELECT status FROM groups WHERE period_key='2026-01'").fetchone()["status"]
    conn.close()
    assert jan == "settled"
