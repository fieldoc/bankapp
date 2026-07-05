from pathlib import Path

import pytest

from bankapp.ingest import core, csv_td
from tests.conftest import insert_account

FIX = Path(__file__).resolve().parent / "fixtures"


def test_parse_correctness():
    txns = csv_td.parse_td_csv(FIX / "td_chequing_A.csv", "td-chequing")
    assert len(txns) == 4
    # withdrawal -> negative, deposit -> positive
    tims = [t for t in txns if "tim hortons" in t.description_norm]
    assert all(t.amount_minor == -500 for t in tims)
    payroll = next(t for t in txns if "payroll" in t.description_norm)
    assert payroll.amount_minor == 250000
    assert payroll.posted_date == "2026-01-12"  # MM/DD/YYYY -> ISO
    assert all(t.source == "csv" for t in txns)
    assert all(t.dedup_key.startswith("sha256:") for t in txns)


def test_same_day_identical_pair_distinct_keys():
    txns = csv_td.parse_td_csv(FIX / "td_chequing_A.csv", "td-chequing")
    tims = [t for t in txns if "tim hortons" in t.description_norm]
    assert len(tims) == 2
    assert tims[0].dedup_key != tims[1].dedup_key  # occurrence 0 vs 1


def test_overlapping_window_only_non_overlap_inserts(conn):
    insert_account(conn, key="td-chequing")
    a = csv_td.parse_td_csv(FIX / "td_chequing_A.csv", "td-chequing")
    b = csv_td.parse_td_csv(FIX / "td_chequing_B.csv", "td-chequing")

    assert core.insert_batch(conn, a) == (4, 0)
    # B overlaps whole days Jan 12 & Jan 15 (2 rows) -> skipped; Jan 18 & 20 -> new.
    assert core.insert_batch(conn, b) == (2, 2)
    assert conn.execute("SELECT COUNT(*) FROM raw_txn").fetchone()[0] == 6


def test_identical_pair_exactly_two_rows_ever(conn):
    insert_account(conn, key="td-chequing")
    a = csv_td.parse_td_csv(FIX / "td_chequing_A.csv", "td-chequing")
    core.insert_batch(conn, a)
    core.insert_batch(conn, a)  # re-ingest same file
    core.insert_batch(conn, csv_td.parse_td_csv(FIX / "td_chequing_A.csv", "td-chequing"))
    n_tims = conn.execute(
        "SELECT COUNT(*) FROM raw_txn WHERE description_norm LIKE 'tim hortons%'"
    ).fetchone()[0]
    assert n_tims == 2


def test_bad_date_raises(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("2026-01-10,FOO,5.00,,1.00\n")  # wrong date format
    with pytest.raises(csv_td.MalformedCSVError):
        csv_td.parse_td_csv(bad, "td-chequing")


def test_too_few_columns_raises(tmp_path):
    bad = tmp_path / "short.csv"
    bad.write_text("01/10/2026,FOO\n")
    with pytest.raises(csv_td.MalformedCSVError):
        csv_td.parse_td_csv(bad, "td-chequing")
