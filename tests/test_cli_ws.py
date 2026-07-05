from typer.testing import CliRunner

from bankapp.cli import app
from bankapp.ingest import ws as wsmod

runner = CliRunner()


def test_sync_ws_without_login_soft_skips(app_env, memkeyring):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["sync", "ws"])
    assert result.exit_code == 0  # scheduler-safe: never hard-fails
    assert "run `finance ws login`" in result.output
    assert "0 inserted" in result.output


def test_ws_login_stores_session(app_env, memkeyring, monkeypatch):
    def fake_auth(username, password, otp=None, api_factory=None):
        wsmod.save_session('{"access_token": "cli-tok"}')

    monkeypatch.setattr(wsmod, "authenticate", fake_auth)
    result = runner.invoke(app, ["ws", "login"], input="me@example.com\nsecret\n123456\n")
    assert result.exit_code == 0, result.output
    assert "stored in keyring" in result.output
    assert wsmod.load_session_json() == '{"access_token": "cli-tok"}'
