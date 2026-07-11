from typer.testing import CliRunner

from bankapp.cli import app
from bankapp.report import advisor
from tests.conftest import insert_account, insert_raw_txn

runner = CliRunner()


def test_reconcile_ok_when_ledger_matches_delta(conn):
    a = insert_account(conn, key="td-chequing")
    advisor.snapshot_balance(conn, a, "2026-01-01", 100000, "CAD", "ofx")
    advisor.snapshot_balance(conn, a, "2026-01-31", 105000, "CAD", "ofx")
    # ledger delta between 2026-01-01 (exclusive) and 2026-01-31 (inclusive) sums to 5000
    insert_raw_txn(conn, a, posted_date="2026-01-15", amount_minor=5000, dedup_key="t1")

    rows = advisor.reconcile(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r.account_key == "td-chequing"
    assert r.currency == "CAD"
    assert r.status == "ok"
    assert r.anchor_as_of == "2026-01-01"
    assert r.target_as_of == "2026-01-31"
    assert r.expected_delta_minor == 5000
    assert r.ledger_delta_minor == 5000
    assert r.drift_minor == 0


def test_reconcile_flags_drift_equal_to_missing_txn(conn):
    a = insert_account(conn, key="td-chequing")
    advisor.snapshot_balance(conn, a, "2026-01-01", 100000, "CAD", "ofx")
    advisor.snapshot_balance(conn, a, "2026-01-31", 105000, "CAD", "ofx")
    # only 3000 of the ledger accounted for -> 2000 missing
    insert_raw_txn(conn, a, posted_date="2026-01-15", amount_minor=3000, dedup_key="t1")

    rows = advisor.reconcile(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r.status == "drift"
    assert r.expected_delta_minor == 5000
    assert r.ledger_delta_minor == 3000
    assert r.drift_minor == 2000


def test_reconcile_unverified_with_single_snapshot(conn):
    a = insert_account(conn, key="td-chequing")
    advisor.snapshot_balance(conn, a, "2026-01-31", 420000, "CAD", "ofx")

    rows = advisor.reconcile(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r.status == "unverified"
    assert r.anchor_as_of == "2026-01-31"
    assert r.target_as_of == "2026-01-31"
    assert r.expected_delta_minor is None
    assert r.ledger_delta_minor is None
    assert r.drift_minor is None


def test_reconcile_boundary_anchor_excluded_target_included(conn):
    a = insert_account(conn, key="td-chequing")
    advisor.snapshot_balance(conn, a, "2026-01-01", 100000, "CAD", "ofx")
    advisor.snapshot_balance(conn, a, "2026-01-31", 100000, "CAD", "ofx")
    # a txn dated exactly on the anchor as_of must be EXCLUDED
    insert_raw_txn(conn, a, posted_date="2026-01-01", amount_minor=9999, dedup_key="anchor-day")
    # a txn dated exactly on the target as_of must be INCLUDED
    insert_raw_txn(conn, a, posted_date="2026-01-31", amount_minor=1, dedup_key="target-day")

    rows = advisor.reconcile(conn)
    r = rows[0]
    # expected_delta is 0 (both snapshots equal); ledger_delta should only include
    # the target-day txn (1), not the anchor-day txn (9999) -> drift = 0 - 1 = -1
    assert r.expected_delta_minor == 0
    assert r.ledger_delta_minor == 1
    assert r.drift_minor == -1
    assert r.status == "drift"


def test_reconcile_multi_source_same_as_of_uses_latest_captured(conn):
    a = insert_account(conn, key="td-chequing")
    # two snapshots sharing the same as_of but different sources; latest captured_at wins.
    # Insert directly with explicit captured_at to make the tie-break deterministic
    # (real-clock calls to snapshot_balance can land in the same second).
    conn.execute(
        """INSERT INTO balance_snapshot(account_id, as_of, balance_minor, currency, source, captured_at)
           VALUES (?,?,?,?,?,?)""",
        (a, "2026-01-01", 100000, "CAD", "ofx", "2026-01-02T00:00:00Z"),
    )
    conn.execute(
        """INSERT INTO balance_snapshot(account_id, as_of, balance_minor, currency, source, captured_at)
           VALUES (?,?,?,?,?,?)""",
        (a, "2026-01-01", 999999, "CAD", "manual", "2026-01-02T00:00:01Z"),
    )
    conn.commit()
    advisor.snapshot_balance(conn, a, "2026-01-31", 100000, "CAD", "ofx")

    rows = advisor.reconcile(conn)
    r = rows[0]
    # the manual snapshot was captured after the ofx one for the same as_of, so its
    # balance (999999) should be the anchor -> expected_delta = 100000 - 999999
    assert r.expected_delta_minor == 100000 - 999999


def test_cli_report_reconcile_runs(app_env):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["report", "reconcile"])
    assert r.exit_code == 0, r.output


def test_cli_status_prints_reconciliation_line(app_env):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert "Reconciliation:" in r.output
