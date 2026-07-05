"""Shared test fixtures."""

import sqlite3

import pytest

from bankapp import db as dbmod


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
