import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bankapp import config as configmod
from bankapp import db as dbmod
from bankapp.cli import sync_accounts
from bankapp.ingest import plaid_td
from bankapp.ingest.core import NormalizedTxn
from bankapp.ingest.ws import SkipResult

FIX = Path(__file__).resolve().parent / "fixtures"
SAMPLE = json.loads((FIX / "plaid_sync_sample.json").read_text())


# ---- TP.2 pure mapper ----

def test_map_posted_spend_sign_negated():
    t = plaid_td.map_plaid_txn(SAMPLE[0], "td-chequing")
    assert isinstance(t, NormalizedTxn)
    assert t.amount_minor == -1234  # Plaid +12.34 (out) -> negative
    assert t.dedup_key == "plaid:txn-0001"
    assert t.source == "plaid"
    assert t.posted_date == "2026-01-10"
    assert "shoppers drug mart" in t.description_norm


def test_map_posted_deposit_positive():
    t = plaid_td.map_plaid_txn(SAMPLE[1], "td-chequing")
    assert t.amount_minor == 50000  # Plaid -500.00 (in) -> positive
    assert "payroll" in t.description_norm  # falls back to name when merchant_name null


def test_map_pending_skipped():
    r = plaid_td.map_plaid_txn(SAMPLE[2], "td-chequing")
    assert isinstance(r, SkipResult)
    assert r.reason == "pending"


def test_map_malformed_skips():
    r = plaid_td.map_plaid_txn(SAMPLE[3], "td-chequing")
    assert isinstance(r, SkipResult)
    assert "schema-drift" in r.reason


# ---- TP.1 credentials ----

def test_missing_creds_actionable(memkeyring):
    with pytest.raises(plaid_td.PlaidCredsError, match="finance plaid keys"):
        plaid_td.load_credentials()


def test_store_and_load_creds(memkeyring):
    plaid_td.store_credentials("cid-123", "sec-456")
    assert plaid_td.load_credentials() == ("cid-123", "sec-456")


# ---- account map ----

def test_resolve_account_map_by_subtype(app_env):
    cfg = configmod.load_config(app_env["config"])
    accounts = [
        SimpleNamespace(account_id="acc-chq", subtype="checking"),
        SimpleNamespace(account_id="acc-visa", subtype="credit card"),
        SimpleNamespace(account_id="acc-other", subtype="savings"),
    ]
    mapping = plaid_td.resolve_account_map(accounts, cfg)
    assert mapping == {"acc-chq": "td-chequing", "acc-visa": "td-visa"}


# ---- sync (mocked client) ----

class _FakeResp:
    def __init__(self, added, accounts, has_more=False, next_cursor="cursor-1"):
        self.added = [SimpleNamespace(to_dict=lambda a=a: a) for a in added]
        self.modified = []
        self.removed = []
        self.has_more = has_more
        self.next_cursor = next_cursor
        self.accounts = accounts


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.calls = 0

    def transactions_sync(self, request):
        self.calls += 1
        return self._resp


@pytest.fixture
def plaid_db(app_env, memkeyring):
    cfg = configmod.load_config(app_env["config"])
    conn = dbmod.init_db(cfg.db_path)
    sync_accounts(conn, cfg)
    dbmod.set_meta(conn, "plaid_account_map", json.dumps({"acc-chq": "td-chequing"}))
    plaid_td.store_access_token("access-tok")
    return cfg, conn


def test_sync_inserts_and_skips_pending(plaid_db):
    cfg, conn = plaid_db
    bal = SimpleNamespace(current=4200.00, iso_currency_code="CAD")
    accounts = [SimpleNamespace(account_id="acc-chq", balances=bal)]
    client = _FakeClient(_FakeResp(SAMPLE, accounts))

    report = plaid_td.sync_plaid(conn, cfg, client=client)

    assert report.inserted == 2         # 4 items: 1 pending, 1 malformed -> 2 posted
    assert conn.execute("SELECT COUNT(*) FROM raw_txn").fetchone()[0] == 2
    assert dbmod.get_meta(conn, "plaid_cursor") == "cursor-1"
    # balance snapshot captured
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshot WHERE source='plaid'").fetchone()[0] == 1


def test_sync_idempotent_after_cursor_reset(plaid_db):
    cfg, conn = plaid_db
    accounts = [SimpleNamespace(account_id="acc-chq", balances=SimpleNamespace(current=1.0, iso_currency_code="CAD"))]
    client = _FakeClient(_FakeResp(SAMPLE, accounts))
    plaid_td.sync_plaid(conn, cfg, client=client)
    # re-sync returns the same page -> INSERT OR IGNORE makes it a no-op
    report2 = plaid_td.sync_plaid(conn, cfg, client=client)
    assert report2.inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM raw_txn").fetchone()[0] == 2


def test_sync_not_linked_soft_skips(app_env, memkeyring):
    cfg = configmod.load_config(app_env["config"])
    conn = dbmod.init_db(cfg.db_path)
    sync_accounts(conn, cfg)
    report = plaid_td.sync_plaid(conn, cfg, client=object())  # no access token stored
    assert report.inserted == 0
    assert any("finance plaid link" in e for e in report.errors)


def test_sync_api_error_soft_skips(plaid_db):
    cfg, conn = plaid_db

    class Boom:
        def transactions_sync(self, request):
            raise RuntimeError("ITEM_LOGIN_REQUIRED")

    report = plaid_td.sync_plaid(conn, cfg, client=Boom())
    assert report.inserted == 0
    assert any("ITEM_LOGIN_REQUIRED" in e for e in report.errors)
