import json
from pathlib import Path

import pytest

from bankapp import config as configmod
from bankapp import db as dbmod
from bankapp.cli import sync_accounts
from bankapp.ingest import ws
from bankapp.ingest.core import NormalizedTxn

FIX = Path(__file__).resolve().parent / "fixtures"
SAMPLE = json.loads((FIX / "ws_activities_sample.json").read_text())


# ---- map_activity (pure) ----

def test_map_negative_spend():
    t = ws.map_activity(SAMPLE[0], "ws-cash", "America/Vancouver")
    assert isinstance(t, NormalizedTxn)
    assert t.amount_minor == -1234
    assert t.dedup_key == "wsid:act-0001"
    assert t.source == "ws"
    assert t.posted_date == "2026-01-10"


def test_map_positive_deposit():
    t = ws.map_activity(SAMPLE[1], "ws-cash", "America/Vancouver")
    assert t.amount_minor == 50000
    assert "interac e-transfer" in t.description_norm


def test_map_midnight_boundary_utc_to_vancouver():
    # 2026-01-16T03:30Z is 2026-01-15 19:30 in Vancouver (PST, UTC-8)
    t = ws.map_activity(SAMPLE[2], "ws-cash", "America/Vancouver")
    assert t.posted_date == "2026-01-15"


def test_map_pending_skipped():
    r = ws.map_activity(SAMPLE[3], "ws-cash", "America/Vancouver")
    assert isinstance(r, ws.SkipResult)
    assert r.reason == "pending"
    assert r.activity_id == "act-0004"


def test_map_schema_drift_skips_not_crashes():
    broken = {"canonicalId": "x", "amount": "1.00"}  # missing currency/occurredAt/etc.
    r = ws.map_activity(broken, "ws-cash", "America/Vancouver")
    assert isinstance(r, ws.SkipResult)
    assert "schema-drift" in r.reason


# ---- keyring auth ----

def test_save_and_load_session(memkeyring):
    ws.save_session('{"access_token": "tok"}')
    assert ws.load_session_json() == '{"access_token": "tok"}'


def test_client_from_keyring_no_session(memkeyring):
    with pytest.raises(ws.NoSessionError, match="finance ws login"):
        ws.client_from_keyring(api_factory=_FakeFactory([], {}))


def test_authenticate_persists(memkeyring):
    factory = _FakeFactory([], {})
    ws.authenticate("user", "pass", otp="123456", api_factory=factory)
    assert ws.load_session_json() == factory.session_json


# ---- sync orchestration ----

class _FakeClient:
    def __init__(self, accounts, activities_by_id):
        self._accounts = accounts
        self._activities = activities_by_id

    def get_accounts(self):
        return self._accounts

    def get_activities(self, ws_id, how_many=200):
        return list(self._activities.get(ws_id, []))


class _FakeFactory:
    """Stands in for WealthsimpleAPI: login persists a session, from_token rebuilds a client."""

    def __init__(self, accounts, activities_by_id):
        self.client = _FakeClient(accounts, activities_by_id)
        self.session_json = '{"access_token": "faketok"}'

    def login(self, username, password, otp_answer=None, persist_session_fct=None):
        if persist_session_fct:
            persist_session_fct(self.session_json)
        return self.client

    def from_token(self, sess, persist_session_fct=None, username=None):
        return self.client


@pytest.fixture
def synced_db(app_env):
    cfg = configmod.load_config(app_env["config"])
    conn = dbmod.init_db(cfg.db_path)
    sync_accounts(conn, cfg)
    return cfg, conn


def test_sync_ws_ingests_and_skips_pending(synced_db):
    cfg, conn = synced_db
    ws_accounts = [{"id": "ws-acct-cash-1", "unifiedAccountType": "CASH"}]
    client = _FakeClient(ws_accounts, {"ws-acct-cash-1": SAMPLE})

    report = ws.sync_ws(conn, cfg, client=client)

    assert report.inserted == 3        # 4 activities, 1 pending skipped
    assert report.skipped == 1
    assert report.errors == []
    assert conn.execute("SELECT COUNT(*) FROM raw_txn").fetchone()[0] == 3
    assert dbmod.get_meta(conn, "ws_last_sync")


def test_sync_ws_idempotent(synced_db):
    cfg, conn = synced_db
    ws_accounts = [{"id": "ws-acct-cash-1", "unifiedAccountType": "CASH"}]
    client = _FakeClient(ws_accounts, {"ws-acct-cash-1": SAMPLE})
    ws.sync_ws(conn, cfg, client=client)
    report2 = ws.sync_ws(conn, cfg, client=client)
    assert report2.inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM raw_txn").fetchone()[0] == 3


def test_sync_ws_records_external_id(synced_db):
    cfg, conn = synced_db
    ws_accounts = [{"id": "ws-acct-cash-1", "unifiedAccountType": "CASH"}]
    ws.sync_ws(conn, cfg, client=_FakeClient(ws_accounts, {"ws-acct-cash-1": []}))
    ext = conn.execute("SELECT external_id FROM accounts WHERE key='ws-cash'").fetchone()[0]
    assert ext == "ws-acct-cash-1"


def test_sync_ws_captures_balance_snapshot(synced_db):
    cfg, conn = synced_db
    ws_accounts = [{"id": "ws-acct-cash-1", "unifiedAccountType": "CASH"}]

    class ClientWithBalances(_FakeClient):
        def get_account_balances(self, ws_id):
            return {"sec-c-cad": "1234.56"}

    ws.sync_ws(conn, cfg, client=ClientWithBalances(ws_accounts, {"ws-acct-cash-1": []}))
    row = conn.execute(
        "SELECT balance_minor, source FROM balance_snapshot"
    ).fetchone()
    assert row["balance_minor"] == 123456
    assert row["source"] == "ws"


def test_sync_ws_api_error_soft_skips(synced_db):
    cfg, conn = synced_db

    class Boom:
        def get_accounts(self):
            raise RuntimeError("WS is down")

    report = ws.sync_ws(conn, cfg, client=Boom())
    assert report.inserted == 0
    assert any("WS is down" in e for e in report.errors)
    assert dbmod.get_meta(conn, "ws_last_error")
