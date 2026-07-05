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
