import shutil

from typer.testing import CliRunner

from bankapp.cli import app
from tests.conftest import FIXTURES

runner = CliRunner()


def test_refresh_no_token_empty_inbox_runs_clean(app_env, memkeyring):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["refresh"])
    assert result.exit_code == 0, result.output
    assert "refresh complete" in result.output
    assert "run `finance ws login`" in result.output  # soft-skip warning


def test_refresh_ingests_inbox_ofx(app_env, memkeyring):
    runner.invoke(app, ["init"])
    shutil.copy(FIXTURES / "td_chequing_jan.ofx", app_env["inbox"] / "td_chequing_jan.ofx")
    result = runner.invoke(app, ["refresh"])
    assert result.exit_code == 0, result.output
    assert "inbox: 3 inserted" in result.output


def test_refresh_ingests_inbox_csv_by_filename(app_env, memkeyring):
    runner.invoke(app, ["init"])
    # name it <account-key>_*.csv so refresh can infer the account
    shutil.copy(FIXTURES / "td_chequing_A.csv", app_env["inbox"] / "td-chequing_jan.csv")
    result = runner.invoke(app, ["refresh"])
    assert result.exit_code == 0, result.output
    assert "inbox: 4 inserted" in result.output
