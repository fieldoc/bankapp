import pytest

from bankapp.ingest import core
from bankapp import normalize
from tests.conftest import insert_account


def _txn(account_key="td-chequing", posted_date="2026-01-15", amount_minor=-1234,
         currency="CAD", desc="Shoppers Drug Mart", dedup_key=None, source="csv", occurrence=0):
    dn = normalize.norm_desc(desc)
    if dedup_key is None:
        dedup_key = normalize.content_dedup_key(account_key, posted_date, amount_minor, currency, dn, occurrence)
    return core.make_txn(account_key, posted_date, amount_minor, currency, desc, dedup_key, source)


def test_frozen_dataclass():
    t = _txn()
    with pytest.raises(Exception):
        t.amount_minor = 0  # frozen


def test_make_txn_computes_norm():
    t = core.make_txn("td-chequing", "2026-01-15", -100, "CAD", "  Foo   BAR ", "sha256:x", "csv")
    assert t.description_norm == "foo bar"


def test_insert_batch_three_new(conn):
    insert_account(conn, key="td-chequing")
    txns = [_txn(amount_minor=-100, desc="A"), _txn(amount_minor=-200, desc="B"), _txn(amount_minor=-300, desc="C")]
    assert core.insert_batch(conn, txns) == (3, 0)
    assert conn.execute("SELECT COUNT(*) FROM raw_txn").fetchone()[0] == 3


def test_insert_batch_rerun_all_skipped(conn):
    insert_account(conn, key="td-chequing")
    txns = [_txn(amount_minor=-100, desc="A"), _txn(amount_minor=-200, desc="B"), _txn(amount_minor=-300, desc="C")]
    core.insert_batch(conn, txns)
    assert core.insert_batch(conn, txns) == (0, 3)
    assert conn.execute("SELECT COUNT(*) FROM raw_txn").fetchone()[0] == 3


def test_same_day_identical_pair_both_land(conn):
    insert_account(conn, key="td-chequing")
    # identical except occurrence -> two distinct dedup keys -> both rows land.
    a = _txn(desc="TIM HORTONS", amount_minor=-500, occurrence=0)
    b = _txn(desc="TIM HORTONS", amount_minor=-500, occurrence=1)
    assert core.insert_batch(conn, [a, b]) == (2, 0)
    assert conn.execute("SELECT COUNT(*) FROM raw_txn").fetchone()[0] == 2


def test_fitid_dedup_survives_description_drift(conn):
    insert_account(conn, key="td-chequing")
    v1 = core.make_txn("td-chequing", "2026-01-15", -100, "CAD", "AMZN Mktp CA", "fitid:ABC123", "ofx")
    v2 = core.make_txn("td-chequing", "2026-01-15", -100, "CAD", "AMAZON.CA PURCHASE", "fitid:ABC123", "ofx")
    core.insert_batch(conn, [v1])
    assert core.insert_batch(conn, [v2]) == (0, 1)  # same FITID -> skipped despite different description
    assert conn.execute("SELECT COUNT(*) FROM raw_txn").fetchone()[0] == 1


def test_unknown_account_clear_error(conn):
    with pytest.raises(core.UnknownAccountError, match="ghost-account"):
        core.insert_batch(conn, [_txn(account_key="ghost-account")])
    assert conn.execute("SELECT COUNT(*) FROM raw_txn").fetchone()[0] == 0


def test_empty_batch(conn):
    assert core.insert_batch(conn, []) == (0, 0)


def test_record_import_short_circuit(conn):
    assert core.record_import(conn, "jan.csv", "hash-1", 3, 0) is True
    # same file hash again -> short-circuit, no second row
    assert core.record_import(conn, "jan.csv", "hash-1", 3, 0) is False
    assert conn.execute("SELECT COUNT(*) FROM import_log").fetchone()[0] == 1
    assert core.already_imported(conn, "hash-1") is True
    assert core.already_imported(conn, "nope") is False
