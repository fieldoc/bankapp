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


def test_ws_login_two_step_otp(app_env, memkeyring, monkeypatch):
    """WS only SENDS the 2FA code when a login attempt happens. The CLI must attempt
    first (no OTP), catch OTPRequiredException, and only then prompt for the code."""
    from ws_api import OTPRequiredException

    calls = []

    def fake_auth(username, password, otp=None, api_factory=None):
        calls.append(otp)
        if otp is None:
            raise OTPRequiredException("2FA code required")  # attempt triggers the code send
        wsmod.save_session('{"access_token": "cli-tok"}')

    monkeypatch.setattr(wsmod, "authenticate", fake_auth)
    # input: email, password, then the OTP (prompted only AFTER the first attempt)
    result = runner.invoke(app, ["ws", "login"], input="me@example.com\nsecret\n123456\n")
    assert result.exit_code == 0, result.output
    assert calls == [None, "123456"]  # attempt-without-otp first, then retry with it
    assert "code was just sent" in result.output.lower()
    assert "stored in keyring" in result.output
    assert wsmod.load_session_json() == '{"access_token": "cli-tok"}'


def test_ws_login_no_otp_needed(app_env, memkeyring, monkeypatch):
    """If WS doesn't ask for a code, no OTP prompt appears at all."""

    def fake_auth(username, password, otp=None, api_factory=None):
        wsmod.save_session('{"access_token": "cli-tok"}')

    monkeypatch.setattr(wsmod, "authenticate", fake_auth)
    result = runner.invoke(app, ["ws", "login"], input="me@example.com\nsecret\n")
    assert result.exit_code == 0, result.output
    assert "stored in keyring" in result.output


def test_ws_login_wrong_otp_fails_cleanly(app_env, memkeyring, monkeypatch):
    from ws_api import LoginFailedException, OTPRequiredException

    def fake_auth(username, password, otp=None, api_factory=None):
        if otp is None:
            raise OTPRequiredException("2FA code required")
        raise LoginFailedException("Login failed", {"error": "invalid_grant"})

    monkeypatch.setattr(wsmod, "authenticate", fake_auth)
    result = runner.invoke(app, ["ws", "login"], input="me@example.com\nsecret\n000000\n")
    assert result.exit_code == 1
    assert "Login failed" in result.output
