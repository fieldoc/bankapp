"""Anomaly detection: unusual charges, stopped subscriptions, duplicate charges.

Pure detectors are tested over plain tuples/dataclasses (no DB). The DB composer
(anomalies_from_db) is tested over a real in-memory SQLite DB per no-mock-substitution.
"""

from datetime import date

from typer.testing import CliRunner

from bankapp.cli import app
from bankapp.report import advisor
from bankapp.report import anomalies
from tests.conftest import insert_account, insert_raw_txn

runner = CliRunner()


# ---- detect_unusual_charges (A8a) --------------------------------------------

def test_unusual_charge_fires_on_spike_within_lookback():
    today = date(2026, 3, 20)
    txns = [
        ("2025-11-01", -4000, "amazon", "CAD"),
        ("2025-12-01", -4100, "amazon", "CAD"),
        ("2026-01-01", -3900, "amazon", "CAD"),
        ("2026-02-01", -4050, "amazon", "CAD"),
        ("2026-03-15", -15000, "amazon", "CAD"),  # spike, 5 days before `today`
    ]
    out = anomalies.detect_unusual_charges(txns, today)
    assert len(out) == 1
    a = out[0]
    assert a.kind == "unusual_charge"
    assert a.merchant == "amazon"
    assert a.currency == "CAD"
    assert a.amount_minor == -15000
    assert a.date == "2026-03-15"
    assert "vs usual" in a.detail


def test_unusual_charge_does_not_fire_outside_lookback():
    # Same spike shape, but the spike (the latest charge in the group) happened
    # long before `today` -- stale, not "just happened", so it must not fire.
    today = date(2026, 6, 20)
    txns = [
        ("2025-09-01", -4000, "amazon", "CAD"),
        ("2025-10-01", -4100, "amazon", "CAD"),
        ("2025-11-01", -3900, "amazon", "CAD"),
        ("2025-12-01", -4050, "amazon", "CAD"),
        ("2026-01-05", -15000, "amazon", "CAD"),  # >60d before `today`
    ]
    out = anomalies.detect_unusual_charges(txns, today)
    assert out == []


def test_stable_merchant_history_produces_no_unusual_charge():
    # A9: near-identical charges every month -- no false positive.
    today = date(2026, 3, 20)
    txns = [
        ("2025-11-01", -4000, "amazon", "CAD"),
        ("2025-12-01", -4100, "amazon", "CAD"),
        ("2026-01-01", -3900, "amazon", "CAD"),
        ("2026-02-01", -4050, "amazon", "CAD"),
        ("2026-03-01", -4025, "amazon", "CAD"),
    ]
    assert anomalies.detect_unusual_charges(txns, today) == []


def test_unusual_charge_requires_min_history():
    # Too few prior charges to establish a norm -- must not fire even on a spike.
    today = date(2026, 3, 20)
    txns = [
        ("2026-01-01", -4000, "amazon", "CAD"),
        ("2026-02-01", -4100, "amazon", "CAD"),
        ("2026-03-15", -15000, "amazon", "CAD"),
    ]
    assert anomalies.detect_unusual_charges(txns, today) == []


def test_unusual_charge_ignores_inflows():
    today = date(2026, 3, 20)
    txns = [
        ("2025-11-01", -4000, "amazon", "CAD"),
        ("2025-12-01", -4100, "amazon", "CAD"),
        ("2026-01-01", -3900, "amazon", "CAD"),
        ("2026-02-01", -4050, "amazon", "CAD"),
        ("2026-03-15", 15000, "amazon", "CAD"),  # inflow (refund) -- not a charge
    ]
    assert anomalies.detect_unusual_charges(txns, today) == []


# ---- detect_stopped_subscriptions (A8b) --------------------------------------

def test_stopped_subscription_fires_when_quiet_past_cadence():
    today = date(2026, 3, 20)
    subs = [
        advisor.Subscription(
            merchant="netflix.com", currency="CAD", cadence="monthly",
            monthly_cost_minor=1599, last_charge="2026-01-05", count=6, price_creep=False,
        )
    ]
    out = anomalies.detect_stopped_subscriptions(subs, today)
    assert len(out) == 1
    a = out[0]
    assert a.kind == "stopped_subscription"
    assert a.merchant == "netflix.com"
    assert a.amount_minor == 1599
    assert a.date == "2026-01-05"
    assert "monthly" in a.detail


def test_stopped_subscription_does_not_fire_when_recently_charged():
    today = date(2026, 3, 20)
    subs = [
        advisor.Subscription(
            merchant="netflix.com", currency="CAD", cadence="monthly",
            monthly_cost_minor=1599, last_charge="2026-03-04", count=6, price_creep=False,
        )
    ]
    assert anomalies.detect_stopped_subscriptions(subs, today) == []


# ---- detect_duplicate_charges (A8c) ------------------------------------------

def test_duplicate_charge_fires_within_window():
    txns = [
        (1, "2026-03-01", -2500, "uber", "CAD"),
        (1, "2026-03-02", -2500, "uber", "CAD"),
    ]
    out = anomalies.detect_duplicate_charges(txns)
    assert len(out) == 1
    a = out[0]
    assert a.kind == "duplicate_charge"
    assert a.merchant == "uber"
    assert a.amount_minor == -2500
    assert a.date == "2026-03-02"
    assert "2026-03-01" in a.detail and "2026-03-02" in a.detail


def test_duplicate_charge_does_not_fire_outside_window():
    txns = [
        (1, "2026-03-01", -2500, "uber", "CAD"),
        (1, "2026-03-10", -2500, "uber", "CAD"),
    ]
    assert anomalies.detect_duplicate_charges(txns) == []


def test_duplicate_charge_requires_same_account():
    txns = [
        (1, "2026-03-01", -2500, "uber", "CAD"),
        (2, "2026-03-02", -2500, "uber", "CAD"),
    ]
    assert anomalies.detect_duplicate_charges(txns) == []


# ---- anomalies_from_db composer ----------------------------------------------

def test_clean_dataset_yields_no_anomalies(conn):
    # A9: realistic, boring recurring activity -- no anomalies of any kind.
    a = insert_account(conn, key="td-chequing")
    for i, dt in enumerate(["2025-10-05", "2025-11-05", "2025-12-05", "2026-01-05", "2026-02-05", "2026-03-05"]):
        insert_raw_txn(conn, a, posted_date=dt, amount_minor=-1599,
                       description_raw="NETFLIX.COM", description_norm="netflix.com", dedup_key=f"n{i}")
    conn.commit()
    out = anomalies.anomalies_from_db(conn, today=date(2026, 3, 20))
    assert out == []


def test_anomalies_from_db_detects_duplicate_charge(conn):
    a = insert_account(conn, key="td-chequing")
    insert_raw_txn(conn, a, posted_date="2026-03-01", amount_minor=-4500,
                   description_raw="BEST BUY", description_norm="best buy", dedup_key="d1")
    insert_raw_txn(conn, a, posted_date="2026-03-02", amount_minor=-4500,
                   description_raw="BEST BUY", description_norm="best buy", dedup_key="d2")
    conn.commit()
    out = anomalies.anomalies_from_db(conn, today=date(2026, 3, 20))
    assert len(out) == 1
    assert out[0].kind == "duplicate_charge"
    assert out[0].merchant == "best"


# ---- CLI wiring ---------------------------------------------------------------

def test_cli_report_anomalies_runs(app_env):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["report", "anomalies"])
    assert r.exit_code == 0, r.output
