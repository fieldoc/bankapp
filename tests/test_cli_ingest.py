import shutil
from pathlib import Path

from typer.testing import CliRunner

from bankapp import db as dbmod
from bankapp.cli import app
from tests.conftest import FIXTURES

runner = CliRunner()


def test_init_creates_accounts(app_env):
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    conn = dbmod.connect(app_env["db"])
    keys = {r[0] for r in conn.execute("SELECT key FROM accounts")}
    assert {"td-chequing", "td-visa", "ws-cash"} <= keys
    conn.close()


def test_accounts_list(app_env):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["accounts", "list"])
    assert result.exit_code == 0
    assert "td-chequing" in result.output
    assert "ws-cash" in result.output


def test_ingest_ofx_auto_maps(app_env):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["ingest", str(FIXTURES / "td_chequing_jan.ofx")])
    assert result.exit_code == 0, result.output
    assert "3 inserted" in result.output


def test_ingest_csv_requires_account(app_env):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["ingest", str(FIXTURES / "td_chequing_A.csv")])
    assert result.exit_code == 1
    assert "requires --account" in result.output


def test_ingest_csv_with_account(app_env):
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app, ["ingest", str(FIXTURES / "td_chequing_A.csv"), "--account", "td-chequing"]
    )
    assert result.exit_code == 0, result.output
    assert "4 inserted" in result.output


def test_ingest_directory_mixed(app_env):
    """A directory of ofx + qfx + csv, one --account applied to the csv."""
    runner.invoke(app, ["init"])
    inbox = app_env["inbox"]
    for name in ["td_chequing_jan.ofx", "td_visa_jan.qfx", "td_chequing_A.csv"]:
        shutil.copy(FIXTURES / name, inbox / name)
    result = runner.invoke(app, ["ingest", str(inbox), "--account", "td-chequing"])
    assert result.exit_code == 0, result.output
    # 3 (chequing ofx) + 3 (visa qfx) + 4 (csv) = 10
    assert "TOTAL: 10 inserted" in result.output


def test_ingest_quarantines_malformed(app_env):
    runner.invoke(app, ["init"])
    inbox = app_env["inbox"]
    shutil.copy(FIXTURES / "malformed.ofx", inbox / "malformed.ofx")
    result = runner.invoke(app, ["ingest", str(inbox / "malformed.ofx")])
    assert result.exit_code == 0, result.output
    assert "QUARANTINED" in result.output
    assert not (inbox / "malformed.ofx").exists()
    assert (app_env["tmp"] / "finance" / "quarantine" / "malformed.ofx").exists()
