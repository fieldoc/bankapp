from bankapp import config as configmod
from bankapp.match import splits
from tests.conftest import FIXTURES


def _rent_template(**over):
    from bankapp.config import TemplateConfig

    base = dict(
        name="rent", kind="split_expense", expected_amount_minor=240000, currency="CAD",
        share_numer=1, share_denom=2, day_of_month=1, expense_account="ws-cash",
        expense_pattern="landlord", reimburse_account="td-chequing",
        reimburser_pattern="etransfer from roommate", amount_tolerance_minor=500,
        window_days=45, link_transfer=True, cadence="monthly",
    )
    base.update(over)
    return TemplateConfig(**base)


def test_upsert_creates_template(conn):
    assert splits.upsert_templates(conn, [_rent_template()]) == 1
    rows = splits.load_templates(conn)
    assert len(rows) == 1
    assert rows[0].name == "rent"
    assert (rows[0].share_numer, rows[0].share_denom) == (1, 2)


def test_upsert_id_stable_across_edits(conn):
    splits.upsert_templates(conn, [_rent_template(expected_amount_minor=240000)])
    id1 = splits.load_templates(conn)[0].id
    # edit the amount and re-upsert -> same id, new value
    splits.upsert_templates(conn, [_rent_template(expected_amount_minor=250000)])
    tmpls = splits.load_templates(conn)
    assert len(tmpls) == 1
    assert tmpls[0].id == id1
    assert tmpls[0].expected_amount_minor == 250000


def test_upsert_from_example_config(conn):
    cfg = configmod.load_config(FIXTURES.parent.parent / "config.example.toml")
    splits.upsert_templates(conn, cfg.templates)
    assert splits.load_templates(conn)[0].name == "rent"


def test_start_period_roundtrip_and_unset_clears(conn):
    splits.upsert_templates(conn, [_rent_template(start_period="2026-01")])
    assert splits.load_templates(conn)[0].start_period == "2026-01"
    # removing it from config clears the stored value on the next upsert
    splits.upsert_templates(conn, [_rent_template()])
    assert splits.load_templates(conn)[0].start_period is None
