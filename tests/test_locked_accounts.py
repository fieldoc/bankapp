"""Locked accounts (e.g. a TFSA): counted in net worth, never ingested, reported apart."""

import pytest

from bankapp import config as configmod
from bankapp import db as dbmod
from bankapp.cli import sync_accounts
from bankapp.ingest import ws
from bankapp.report import advisor

TFSA_CONFIG = """
[[accounts]]
key = "ws-tfsa"
institution = "wealthsimple"
type = "investment"
currency = "CAD"
locked = true
ws_account_type = "TFSA"

[[accounts]]
key = "ws-invest"
institution = "wealthsimple"
type = "investment"
currency = "CAD"
ws_account_type = "NON_REGISTERED"
"""


@pytest.fixture
def tfsa_env(app_env):
    app_env["config"].write_text(app_env["config"].read_text() + TFSA_CONFIG)
    cfg = configmod.load_config(app_env["config"])
    conn = dbmod.init_db(cfg.db_path)
    sync_accounts(conn, cfg)
    return cfg, conn


WS_ACCOUNTS = [
    {
        "id": "ws-tfsa-1",
        "unifiedAccountType": "MANAGED_TFSA",
        "financials": {"currentCombined": {"netLiquidationValue": {"amount": "5432.10", "cents": 543210, "currency": "CAD"}}},
    },
    {
        "id": "ws-nonreg-1",
        "unifiedAccountType": "SELF_DIRECTED_NON_REGISTERED",
        "financials": {"currentCombined": {"netLiquidationValue": {"cents": 4976, "currency": "CAD"}}},
    },
    {
        "id": "ws-cash-1",
        "unifiedAccountType": "CASH",
        "financials": {"currentCombined": {"netLiquidationValue": {"cents": 100000, "currency": "CAD"}}},
    },
]


class _Client:
    def __init__(self):
        self.activity_calls = []

    def get_accounts(self):
        return WS_ACCOUNTS

    def get_activities(self, ws_id, how_many=200, load_all=False):
        self.activity_calls.append(ws_id)
        return []


def test_config_parses_locked_and_hint(tfsa_env):
    cfg, _ = tfsa_env
    tfsa = next(a for a in cfg.accounts if a.key == "ws-tfsa")
    assert tfsa.locked is True
    assert tfsa.ws_account_type == "TFSA"


def test_hint_mapping_disambiguates_same_type(tfsa_env):
    cfg, conn = tfsa_env
    mapping = ws.resolve_ws_account_map(conn, cfg, WS_ACCOUNTS)
    assert mapping["ws-tfsa-1"] == "ws-tfsa"       # TFSA hint
    assert mapping["ws-nonreg-1"] == "ws-invest"   # NON_REGISTERED hint
    assert mapping["ws-cash-1"] == "ws-cash"       # exact type


def test_locked_account_activities_never_fetched(tfsa_env):
    cfg, conn = tfsa_env
    client = _Client()
    ws.sync_ws(conn, cfg, client=client)
    assert "ws-tfsa-1" not in client.activity_calls      # locked: balance-only
    assert "ws-cash-1" in client.activity_calls


def test_net_liquidation_value_snapshot(tfsa_env):
    cfg, conn = tfsa_env
    ws.sync_ws(conn, cfg, client=_Client())
    bal = conn.execute(
        """SELECT b.balance_minor FROM balance_snapshot b
           JOIN accounts a ON a.id = b.account_id WHERE a.key = 'ws-tfsa'"""
    ).fetchone()[0]
    assert bal == 543210  # full market value via cents, not the cash sliver


def test_net_worth_split(tfsa_env):
    cfg, conn = tfsa_env
    ws.sync_ws(conn, cfg, client=_Client())
    split = {s["currency"]: s for s in advisor.net_worth_split(conn)}
    cad = split["CAD"]
    assert cad["locked_minor"] == 543210
    assert cad["accessible_minor"] == 100000 + 4976
    # total still includes locked money (acknowledged, just not spendable)
    total = advisor.net_worth(conn)[0].net_worth_minor
    assert total == cad["locked_minor"] + cad["accessible_minor"]
