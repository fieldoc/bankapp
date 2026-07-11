from decimal import Decimal

from typer.testing import CliRunner

from bankapp import fx
from bankapp.cli import app
from bankapp.report import advisor
from tests.conftest import insert_account

runner = CliRunner()


def test_set_then_latest_rate(conn):
    fx.set_rate(conn, "usd", "cad", "1.37", as_of="2026-07-01")
    assert fx.latest_rate(conn, "USD", "CAD") == Decimal("1.37")


def test_resetting_same_pair_updates_not_duplicates(conn):
    # A4: re-running fx set for the same pair (and day) UPDATES it -- latest wins, no
    # duplicate row for the same day.
    fx.set_rate(conn, "USD", "CAD", "1.35", as_of="2026-07-01")
    fx.set_rate(conn, "USD", "CAD", "1.40", as_of="2026-07-01")
    assert fx.latest_rate(conn, "USD", "CAD") == Decimal("1.40")
    n = conn.execute(
        "SELECT COUNT(*) FROM fx_rate WHERE base='USD' AND quote='CAD'"
    ).fetchone()[0]
    assert n == 1


def test_latest_rate_picks_most_recent_as_of(conn):
    fx.set_rate(conn, "USD", "CAD", "1.30", as_of="2026-06-01")
    fx.set_rate(conn, "USD", "CAD", "1.38", as_of="2026-07-01")
    assert fx.latest_rate(conn, "USD", "CAD") == Decimal("1.38")


def test_identity_rate_needs_no_row(conn):
    assert fx.latest_rate(conn, "CAD", "CAD") == Decimal(1)


def test_latest_rate_missing_pair_is_none(conn):
    assert fx.latest_rate(conn, "EUR", "CAD") is None


def test_list_rates(conn):
    fx.set_rate(conn, "USD", "CAD", "1.30", as_of="2026-06-01")
    fx.set_rate(conn, "USD", "CAD", "1.38", as_of="2026-07-01")
    fx.set_rate(conn, "BTC", "CAD", "90000", as_of="2026-07-01")
    rates = fx.list_rates(conn)
    assert rates == [
        {"base": "BTC", "quote": "CAD", "rate": "90000", "as_of": "2026-07-01"},
        {"base": "USD", "quote": "CAD", "rate": "1.38", "as_of": "2026-07-01"},
    ]


def test_convert_minor_usd_to_cad(conn):
    fx.set_rate(conn, "USD", "CAD", "1.37", as_of="2026-07-01")
    # 100.00 USD -> 137.00 CAD
    assert fx.convert_minor(conn, 10000, "USD", "CAD") == 13700


def test_convert_minor_same_currency_unchanged(conn):
    assert fx.convert_minor(conn, 12345, "CAD", "CAD") == 12345


def test_convert_minor_no_rate_is_none(conn):
    assert fx.convert_minor(conn, 10000, "EUR", "CAD") is None


def test_consolidated_net_worth_all_rates_present(conn):
    cad_acct = insert_account(conn, key="td-chequing", currency="CAD")
    usd_acct = insert_account(conn, key="td-usd", currency="USD")
    advisor.snapshot_balance(conn, cad_acct, "2026-07-01", 100000, "CAD", "ofx")
    advisor.snapshot_balance(conn, usd_acct, "2026-07-01", 20000, "USD", "ofx")
    fx.set_rate(conn, "USD", "CAD", "1.37", as_of="2026-07-01")

    result = advisor.consolidated_net_worth(conn, "CAD")
    assert result["target"] == "CAD"
    assert result["unconverted"] == []
    # 1000.00 CAD + (200.00 USD * 1.37 = 274.00 CAD) = 1274.00 CAD
    assert result["total_minor"] == 100000 + 27400

    by_cur = {c["currency"]: c for c in result["components"]}
    assert by_cur["CAD"]["converted_minor"] == 100000
    assert by_cur["CAD"]["rate"] is not None
    assert by_cur["USD"]["converted_minor"] == 27400
    assert by_cur["USD"]["rate"] == "1.37"


def test_consolidated_net_worth_missing_rate_is_excluded_and_reported(conn):
    # A5: a held currency with NO rate is unconverted/excluded, never silently
    # converted at 0 or dropped without mention.
    cad_acct = insert_account(conn, key="td-chequing", currency="CAD")
    eur_acct = insert_account(conn, key="td-eur", currency="EUR")
    advisor.snapshot_balance(conn, cad_acct, "2026-07-01", 100000, "CAD", "ofx")
    advisor.snapshot_balance(conn, eur_acct, "2026-07-01", 5000, "EUR", "ofx")
    # no rate set for EUR/CAD

    result = advisor.consolidated_net_worth(conn, "CAD")
    assert result["total_minor"] == 100000  # EUR excluded from the total
    assert result["unconverted"] == ["EUR"]

    by_cur = {c["currency"]: c for c in result["components"]}
    assert by_cur["EUR"]["converted_minor"] is None
    assert by_cur["EUR"]["rate"] is None
    assert by_cur["EUR"]["net_worth_minor"] == 5000


def test_fx_set_then_list_cli_roundtrip(app_env):
    runner.invoke(app, ["init"])
    r_set = runner.invoke(app, ["fx", "set", "--pair", "USD/CAD", "--rate", "1.37"])
    assert r_set.exit_code == 0, r_set.output
    r_list = runner.invoke(app, ["fx", "list"])
    assert r_list.exit_code == 0, r_list.output
    assert "USD/CAD" in r_list.output
    assert "1.37" in r_list.output
