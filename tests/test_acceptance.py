"""End-to-end acceptance tests over synthetic fixtures (the spec's AT1-AT3)."""

import shutil

from typer.testing import CliRunner

from bankapp import db as dbmod
from bankapp.cli import app
from tests.conftest import FIXTURES

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
