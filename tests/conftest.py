"""Shared test fixtures."""

import sqlite3
from pathlib import Path

import keyring
from keyring.backend import KeyringBackend
import pytest

from bankapp import db as dbmod

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class MemoryKeyring(KeyringBackend):
    """In-memory keyring backend for tests (no OS credential store touched)."""

    priority = 1

    def __init__(self):
        super().__init__()
        self._store: dict = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        self._store.pop((service, username), None)


@pytest.fixture
def memkeyring():
    kr = MemoryKeyring()
    prev = keyring.get_keyring()
    keyring.set_keyring(kr)
    yield kr
    keyring.set_keyring(prev)

# A config that maps the OFX fixtures' ACCTIDs to real account keys and points db/inbox
# at a temp dir. Used by CLI (CliRunner) tests.
_CONFIG_TEMPLATE = """\
timezone   = "America/Vancouver"
db_path    = "{db}"
ingest_dir = "{inbox}"

[[accounts]]
key = "td-chequing"
institution = "td"
type = "chequing"
currency = "CAD"
ofx_acctid = "1111111"

[[accounts]]
key = "td-visa"
institution = "td"
type = "visa"
currency = "CAD"
ofx_acctid = "4519111122223333"

[[accounts]]
key = "ws-cash"
institution = "wealthsimple"
type = "cash"
currency = "CAD"

[advisor]
leak_threshold = "15.00"

[budgets]
groceries = "600.00"
dining = "250.00"
subscriptions = "60.00"

[category_groups]
groceries = "Food"
dining = "Food"

[[goals]]
name = "example-trip"
target = "3000.00"
start_date = "2026-07-01"
target_date = "2027-02-01"
allocation_pct = 100

[transfers]
window_days = 7
tolerance = "0.00"
seed_patterns = ["tfr-to", "tfr-fr", "eft credit", "eft debit"]

[[templates]]
name = "rent"
kind = "split_expense"
expected_amount = "2400.00"
share = "1/2"
day_of_month = 1
expense_account = "ws-cash"
expense_pattern = "landlord"
reimburse_account = "td-chequing"
reimburser_pattern = "etransfer from roommate"
amount_tolerance = "5.00"
window_days = 45
link_transfer = true
"""


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Write a config + set FINANCE_CONFIG/FINANCE_DB so the CLI runs against a temp DB."""
    db = tmp_path / "finance.db"
    inbox = tmp_path / "finance" / "inbox"
    inbox.mkdir(parents=True)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_CONFIG_TEMPLATE.format(db=db.as_posix(), inbox=inbox.as_posix()))
    monkeypatch.setenv("FINANCE_CONFIG", str(cfg_path))
    monkeypatch.setenv("FINANCE_DB", str(db))
    return {"config": cfg_path, "db": db, "inbox": inbox, "tmp": tmp_path}


@pytest.fixture
def conn():
    """In-memory DB with schema applied."""
    c = dbmod.connect(":memory:")
    dbmod.apply_schema(c)
    yield c
    c.close()


@pytest.fixture
def db_path(tmp_path):
    """Path to a fresh on-disk DB with schema applied (closed connection)."""
    p = tmp_path / "test.db"
    c = dbmod.init_db(p)
    c.close()
    return p


def insert_account(conn, key="td-chequing", institution="td", type="chequing", currency="CAD", external_id=None):
    cur = conn.execute(
        "INSERT INTO accounts(key, institution, type, currency, external_id) VALUES (?,?,?,?,?)",
        (key, institution, type, currency, external_id),
    )
    conn.commit()
    return cur.lastrowid


def insert_raw_txn(conn, account_id, *, posted_date="2026-01-15", amount_minor=-1234,
                   currency="CAD", description_raw="TEST", description_norm="test",
                   dedup_key="sha256:abc", source="csv", imported_at="2026-01-16T00:00:00Z"):
    cur = conn.execute(
        """INSERT INTO raw_txn(account_id, posted_date, amount_minor, currency,
               description_raw, description_norm, dedup_key, source, imported_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (account_id, posted_date, amount_minor, currency, description_raw,
         description_norm, dedup_key, source, imported_at),
    )
    conn.commit()
    return cur.lastrowid
