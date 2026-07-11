import json
from datetime import date

import pytest

from bankapp import config as configmod
from bankapp.config import GoalConfig
from bankapp import goals as goalsmod
from bankapp.report import advisor
from tests.conftest import FIXTURES, insert_account, insert_raw_txn


def _goal(name="trip", target=300000, start="2026-01-01", target_date="2026-12-31", alloc=100):
    return GoalConfig(name=name, target_minor=target, currency="CAD",
                      start_date=start, target_date=target_date, allocation_pct=alloc, note=None)


def _income(conn, acct, amt, date_s="2026-01-05", dedup="i1"):
    insert_raw_txn(conn, acct, posted_date=date_s, amount_minor=amt,
                   description_raw="income", description_norm="income", dedup_key=dedup)
    conn.commit()


# ---- T10.1 goals ----

def test_funding_math(conn):
    a = insert_account(conn, key="td-chequing")
    _income(conn, a, 100000, dedup="i1")   # +1000 net savings since start
    goalsmod.seed_from_config(conn, [_goal(alloc=100)])
    g = advisor.goals_status(conn, today=date(2026, 6, 1))[0]
    assert g.funded_minor == 100000


def test_multi_goal_allocation_split(conn):
    a = insert_account(conn, key="td-chequing")
    _income(conn, a, 100000, dedup="i1")
    goalsmod.seed_from_config(conn, [_goal(name="trip", alloc=60), _goal(name="camera", alloc=40)])
    funded = {g.name: g.funded_minor for g in advisor.goals_status(conn, today=date(2026, 6, 1))}
    assert funded["trip"] == 60000
    assert funded["camera"] == 40000


def test_allocation_over_100_rejected(conn):
    with pytest.raises(goalsmod.AllocationError):
        goalsmod.seed_from_config(conn, [_goal(name="a", alloc=60), _goal(name="b", alloc=60)])


def test_pace_behind_and_on_track(conn):
    a = insert_account(conn, key="td-chequing")
    _income(conn, a, 10000, dedup="i1")  # small savings vs a 300000 target
    goalsmod.seed_from_config(conn, [_goal(target=300000, start="2026-01-01", target_date="2026-12-31", alloc=100)])
    # near year-end, funded 10000 << expected -> behind
    assert advisor.goals_status(conn, today=date(2026, 12, 1))[0].pace == "behind"


def test_inactive_goal_excluded(conn):
    a = insert_account(conn, key="td-chequing")
    _income(conn, a, 100000, dedup="i1")
    goalsmod.seed_from_config(conn, [_goal(name="trip")])
    conn.execute("UPDATE goals SET active = 0 WHERE name = 'trip'")
    conn.commit()
    assert advisor.goals_status(conn) == []


def test_goals_status_exposes_edit_fields(conn):
    goalsmod.seed_from_config(conn, [_goal()])
    g = advisor.goals_status(conn, today=date(2026, 6, 1))[0]
    assert g.id > 0
    assert g.start_date == "2026-01-01"
    assert g.target_date == "2026-12-31"
    assert g.active is True


def test_goals_status_include_archived(conn):
    goalsmod.seed_from_config(conn, [_goal(name="trip")])
    conn.execute("UPDATE goals SET active = 0 WHERE name = 'trip'")
    conn.commit()
    assert advisor.goals_status(conn) == []
    archived = advisor.goals_status(conn, include_archived=True)
    assert [g.name for g in archived] == ["trip"]
    assert archived[0].active is False

# ---- T10.2 digest ----

@pytest.fixture
def seeded(app_env):
    from bankapp import db as dbmod

    cfg = configmod.load_config(app_env["config"])
    conn = dbmod.init_db(cfg.db_path)
    from bankapp.cli import sync_accounts

    sync_accounts(conn, cfg)
    advisor.upsert_budgets(conn, cfg.budgets)
    a = conn.execute("SELECT id FROM accounts WHERE key='td-chequing'").fetchone()[0]
    _income(conn, a, 500000, dedup="pay")
    for i, dt in enumerate(["2026-01-03", "2026-02-02", "2026-03-04"]):
        insert_raw_txn(conn, a, posted_date=dt, amount_minor=-1599,
                       description_raw="NETFLIX.COM", description_norm="netflix.com", dedup_key=f"n{i}")
    advisor.snapshot_balance(conn, a, "2026-01-31", 420000, "CAD", "ofx")
    conn.commit()
    return cfg, conn


def test_digest_json_keys_stable(seeded):
    cfg, conn = seeded
    d = advisor.digest(conn, cfg, today=date(2026, 3, 15))
    expected_keys = {
        "as_of", "month", "net_worth", "net_worth_split", "net_worth_delta_minor",
        "net_worth_consolidated", "savings", "budgets", "subscriptions", "top_leaks",
        "receivables", "goals", "projection", "anomalies", "uncategorized_count",
        "pending_transfer_legs", "data_quality",
    }
    assert set(d.keys()) == expected_keys
    # round-trips as JSON
    assert json.loads(json.dumps(d))["month"] == "2026-03"


def test_digest_markdown_renders(seeded):
    cfg, conn = seeded
    d = advisor.digest(conn, cfg, today=date(2026, 3, 15))
    md = advisor.render_digest_markdown(d)
    assert "# Finance digest" in md
    assert "Net worth" in md
    assert "netflix.com" in md  # detected subscription
