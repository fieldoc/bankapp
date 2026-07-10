from pathlib import Path

import pytest

from bankapp import config as cfg

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def test_loads_example(monkeypatch):
    monkeypatch.delenv("FINANCE_DB", raising=False)
    c = cfg.load_config(EXAMPLE)
    assert c.timezone == "America/Vancouver"
    assert c.config_path == EXAMPLE


def test_env_override_config_path(monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCE_CONFIG", str(EXAMPLE))
    monkeypatch.delenv("FINANCE_DB", raising=False)
    c = cfg.load_config()  # no explicit path -> uses env
    assert c.config_path == EXAMPLE


def test_default_path_used_when_no_env(monkeypatch):
    monkeypatch.delenv("FINANCE_CONFIG", raising=False)
    p = cfg.resolve_config_path()
    # platform default ends in bankapp/config.toml
    assert p.name == "config.toml"
    assert p.parent.name == "bankapp"


def test_tilde_expansion(monkeypatch):
    monkeypatch.delenv("FINANCE_DB", raising=False)
    c = cfg.load_config(EXAMPLE)
    # db_path "~/finance/finance.db" must be expanded (no literal ~)
    assert "~" not in str(c.db_path)
    assert c.db_path.is_absolute()
    assert "~" not in str(c.ingest_dir)


def test_finance_db_env_override(monkeypatch, tmp_path):
    override = tmp_path / "custom.db"
    monkeypatch.setenv("FINANCE_DB", str(override))
    c = cfg.load_config(EXAMPLE)
    assert c.db_path == override


def test_accounts_parsed(monkeypatch):
    monkeypatch.delenv("FINANCE_DB", raising=False)
    c = cfg.load_config(EXAMPLE)
    keys = {a.key for a in c.accounts}
    assert {"td-chequing", "td-visa", "ws-cash"} <= keys
    chq = next(a for a in c.accounts if a.key == "td-chequing")
    assert chq.institution == "td"
    assert chq.type == "chequing"
    assert chq.currency == "CAD"


def test_share_parsed_to_tuple(monkeypatch):
    monkeypatch.delenv("FINANCE_DB", raising=False)
    c = cfg.load_config(EXAMPLE)
    rent = next(t for t in c.templates if t.name == "rent")
    assert (rent.share_numer, rent.share_denom) == (1, 2)


def test_money_strings_to_minor(monkeypatch):
    monkeypatch.delenv("FINANCE_DB", raising=False)
    c = cfg.load_config(EXAMPLE)
    assert c.budgets["groceries"] == 60000
    assert c.leak_threshold_minor == 1500
    rent = next(t for t in c.templates if t.name == "rent")
    assert rent.expected_amount_minor == 240000
    assert rent.amount_tolerance_minor == 500
    goal = next(g for g in c.goals if g.name == "example-trip")
    assert goal.target_minor == 300000


def test_category_groups_parsed(monkeypatch):
    monkeypatch.delenv("FINANCE_DB", raising=False)
    c = cfg.load_config(EXAMPLE)
    assert c.category_groups["rent"] == "Housing"
    assert c.category_groups["groceries"] == "Food"
    assert c.category_groups["loans"] == "Financial"


def test_category_groups_missing_section_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCE_DB", raising=False)
    p = tmp_path / "config.toml"
    p.write_text('timezone = "America/Vancouver"\ndb_path = "/tmp/x.db"\n')
    c = cfg.load_config(p)
    assert c.category_groups == {}


def test_transfers_section(monkeypatch):
    monkeypatch.delenv("FINANCE_DB", raising=False)
    c = cfg.load_config(EXAMPLE)
    assert c.transfers.window_days == 7
    assert c.transfers.tolerance_minor == 0
    assert "tfr-to" in c.transfers.seed_patterns


def test_missing_file_actionable_error(tmp_path):
    missing = tmp_path / "nope.toml"
    with pytest.raises(FileNotFoundError) as ei:
        cfg.load_config(missing)
    msg = str(ei.value)
    assert str(missing) in msg
    assert "config.example.toml" in msg


def test_template_reimburse_account_accepts_list():
    from bankapp.config import _parse_template

    t = _parse_template({
        "name": "rent", "kind": "split_expense", "expected_amount": "2199.00",
        "share": "1/2", "expense_account": "ws-cash", "expense_pattern": "uncle pete",
        "reimburse_account": ["td-chequing", "ws-cash"], "reimburser_pattern": "e-transfer",
    })
    assert t.reimburse_account == "td-chequing,ws-cash"


def test_template_reimburse_account_string_still_works():
    from bankapp.config import _parse_template

    t = _parse_template({
        "name": "rent", "kind": "split_expense", "expected_amount": "2199.00",
        "share": "1/2", "expense_account": "ws-cash", "expense_pattern": "uncle pete",
        "reimburse_account": "td-chequing", "reimburser_pattern": "e-transfer",
    })
    assert t.reimburse_account == "td-chequing"


_TEMPLATE_BASE = {
    "name": "rent", "kind": "split_expense", "expected_amount": "2199.00",
    "share": "1/2", "expense_account": "ws-cash", "expense_pattern": "uncle pete",
    "reimburse_account": "td-chequing", "reimburser_pattern": "e-transfer",
}


def test_template_start_period_parsed():
    from bankapp.config import _parse_template

    t = _parse_template({**_TEMPLATE_BASE, "start_period": "2026-01"})
    assert t.start_period == "2026-01"


def test_template_start_period_defaults_none():
    from bankapp.config import _parse_template

    assert _parse_template(dict(_TEMPLATE_BASE)).start_period is None


@pytest.mark.parametrize("bad", ["2026-1", "Jan 2026", "2026-13", "202601", "2026-00"])
def test_template_start_period_invalid_raises(bad):
    from bankapp.config import _parse_template

    with pytest.raises(ValueError, match="start_period"):
        _parse_template({**_TEMPLATE_BASE, "start_period": bad})


def test_template_start_period_empty_string_means_unset():
    from bankapp.config import _parse_template

    assert _parse_template({**_TEMPLATE_BASE, "start_period": ""}).start_period is None
