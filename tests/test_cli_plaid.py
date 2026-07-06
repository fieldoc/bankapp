from typer.testing import CliRunner

from bankapp.cli import app
from bankapp.ingest import plaid_td

runner = CliRunner()


def test_plaid_keys_stores(app_env, memkeyring):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["plaid", "keys"], input="my-client-id\nmy-secret\n")
    assert r.exit_code == 0, r.output
    assert "stored in keyring" in r.output
    assert plaid_td.load_credentials() == ("my-client-id", "my-secret")


def test_sync_plaid_without_link_soft_skips(app_env, memkeyring):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["sync", "plaid"])
    assert r.exit_code == 0  # scheduler-safe
    assert "finance plaid link" in r.output
    assert "0 inserted" in r.output


def test_refresh_plaid_disabled_by_default(app_env, memkeyring):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["refresh"])
    assert r.exit_code == 0, r.output
    # [plaid] not enabled in the test config -> plaid step skipped entirely
    assert "plaid:" not in r.output
    assert "refresh complete" in r.output


def test_status_shows_plaid_line(app_env, memkeyring):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert "Last Plaid sync:" in r.output
