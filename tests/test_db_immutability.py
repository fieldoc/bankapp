import sqlite3

import pytest

from bankapp import db as dbmod
from tests.conftest import insert_account, insert_raw_txn


def test_schema_applies_twice_cleanly(conn):
    # conftest already applied once; applying again must not raise.
    dbmod.apply_schema(conn)
    dbmod.apply_schema(conn)


def test_schema_version_is_1(conn):
    assert dbmod.schema_version(conn) == "1"


def test_meta_seeded(conn):
    rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert rows["schema_version"] == "1"
    assert rows["hash_version"] == "1"


def test_raw_txn_update_aborts(conn):
    acct = insert_account(conn)
    tid = insert_raw_txn(conn, acct)
    with pytest.raises(sqlite3.IntegrityError, match="raw_txn is immutable"):
        conn.execute("UPDATE raw_txn SET amount_minor = 0 WHERE id = ?", (tid,))


def test_raw_txn_delete_aborts(conn):
    acct = insert_account(conn)
    tid = insert_raw_txn(conn, acct)
    with pytest.raises(sqlite3.IntegrityError, match="raw_txn is immutable"):
        conn.execute("DELETE FROM raw_txn WHERE id = ?", (tid,))


def test_raw_txn_unique_account_dedup_key(conn):
    acct = insert_account(conn)
    insert_raw_txn(conn, acct, dedup_key="fitid:X")
    with pytest.raises(sqlite3.IntegrityError):
        insert_raw_txn(conn, acct, dedup_key="fitid:X", posted_date="2026-02-02")


def test_group_members_unique_raw_txn(conn):
    acct = insert_account(conn)
    tid = insert_raw_txn(conn, acct)
    conn.execute(
        "INSERT INTO groups(type, status, created_at, updated_at) VALUES ('transfer','matched','t','t')"
    )
    g1 = conn.execute("SELECT id FROM groups").fetchone()[0]
    conn.execute(
        "INSERT INTO groups(type, status, created_at, updated_at) VALUES ('transfer','matched','t','t')"
    )
    g2 = conn.execute("SELECT id FROM groups ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO group_members(group_id, raw_txn_id, role) VALUES (?,?, 'transfer_out')",
        (g1, tid),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO group_members(group_id, raw_txn_id, role) VALUES (?,?, 'transfer_in')",
            (g2, tid),
        )


def test_foreign_keys_enforced(conn):
    # account_id 999 does not exist -> FK violation (PRAGMA foreign_keys ON).
    with pytest.raises(sqlite3.IntegrityError):
        insert_raw_txn(conn, 999)


def test_views_exist(conn):
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()
    }
    assert {
        "v_effective", "v_pending_transfers", "v_net_worth",
        "v_monthly_cashflow", "v_receivables",
    } <= names


def test_connect_sets_foreign_keys_on(db_path):
    c = dbmod.connect(db_path)
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    c.close()


def test_start_period_column_backfilled_on_legacy_db():
    # Simulate a pre-start_period DB: drop the column, then apply_schema's guarded
    # ALTER must re-add it (and applying again is a no-op).
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbmod.apply_schema(conn)
    conn.execute("ALTER TABLE recurring_templates DROP COLUMN start_period")
    dbmod.apply_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(recurring_templates)")}
    assert "start_period" in cols
    dbmod.apply_schema(conn)


def test_txn_interp_source_column_backfilled_on_legacy_db():
    # A pre-source DB: drop the column and confirm apply_schema re-adds it with the
    # 'rule' default, so existing interp rows are treated as rule-derived (not manual).
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbmod.apply_schema(conn)
    acct = insert_account(conn)
    tid = insert_raw_txn(conn, acct)
    conn.execute(
        "INSERT INTO txn_interp(raw_txn_id, category, updated_at) VALUES (?, 'groceries', 't')",
        (tid,),
    )
    conn.execute("ALTER TABLE txn_interp DROP COLUMN source")
    dbmod.apply_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(txn_interp)")}
    assert "source" in cols
    assert conn.execute("SELECT source FROM txn_interp WHERE raw_txn_id = ?", (tid,)).fetchone()[0] == "rule"
    dbmod.apply_schema(conn)
