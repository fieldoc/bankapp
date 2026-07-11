"""S5: what changed since the last persisted advisor brief.

digest()["changes_since_brief"] compares the CURRENT digest against the most
recent brief that carries a pure digest_json snapshot. With zero such briefs it
reports has_prior=False (A6), never an error. A newly-detected subscription, a
budget that newly crossed over, and a goal that newly fell off pace since that
snapshot each surface as a distinct change line (A7).
"""

import json
from dataclasses import dataclass
from datetime import date

from typer.testing import CliRunner

from bankapp import db as dbmod
from bankapp import goals as goalsmod
from bankapp.cli import app
from bankapp.config import GoalConfig
from bankapp.report import advisor, briefs
from tests.conftest import insert_account, insert_raw_txn

runner = CliRunner()


@dataclass(frozen=True)
class _Cfg:
    """Minimal stand-in for bankapp.config.Config: digest() only reads
    leak_threshold_minor off the cfg it's given."""
    leak_threshold_minor: int = 1500


def _txn(conn, acct, date_s, amt, desc, dedup, category=None):
    tid = insert_raw_txn(conn, acct, posted_date=date_s, amount_minor=amt,
                         description_raw=desc, description_norm=desc.lower(), dedup_key=dedup)
    if category:
        conn.execute("INSERT INTO txn_interp(raw_txn_id, category, updated_at) VALUES (?,?,'t')", (tid, category))
    conn.commit()
    return tid


def _pure(d: dict) -> dict:
    """What add_brief's digest_json should hold: the digest WITHOUT the volatile
    changes_since_brief key (avoids nesting snapshots forever)."""
    return {k: v for k, v in d.items() if k != "changes_since_brief"}


# ---- A6: no prior snapshot ---------------------------------------------------

def test_no_briefs_at_all_reports_no_prior(conn):
    d = advisor.digest(conn, _Cfg(), today=date(2026, 3, 15))
    assert d["changes_since_brief"] == {"has_prior": False, "since": None, "changes": []}


def test_brief_without_digest_json_is_ignored_as_prior(conn):
    # A brief written before this column existed (or by an older caller) stores
    # digest_json=NULL and must not be treated as a usable snapshot.
    briefs.add_brief(conn, "an old brief", "2026-01-01", source="claude")
    d = advisor.digest(conn, _Cfg(), today=date(2026, 3, 15))
    assert d["changes_since_brief"]["has_prior"] is False
    assert d["changes_since_brief"]["changes"] == []


# ---- A7: distinct change lines -----------------------------------------------

def test_new_subscription_budget_over_and_goal_off_pace_each_appear(conn):
    cfg = _Cfg()
    a = insert_account(conn, key="td-chequing")

    goal = GoalConfig(name="trip", target_minor=300000, currency="CAD",
                      start_date="2026-07-01", target_date="2027-02-01",
                      allocation_pct=100, note=None)
    goalsmod.seed_from_config(conn, [goal])

    advisor.upsert_budgets(conn, {"dining": 25000}, currency="CAD")

    # Funds the goal just after its start -> comfortably on_track early on (net-since-start
    # funding, so it must clear the July dining spend below too).
    _txn(conn, a, "2026-07-02", 50000, "payroll", "inc1")
    # July dining spend well under the 250.00 limit.
    _txn(conn, a, "2026-07-05", -10000, "restaurant", "d-jul", category="dining")

    prior = advisor.digest(conn, cfg, today=date(2026, 7, 10))
    assert prior["changes_since_brief"]["has_prior"] is False  # nothing persisted yet
    # Sanity-check the "prior" state is what the test intends before it's snapshotted.
    prior_dining = {b["category"]: b for b in prior["budgets"]}["dining"]
    assert prior_dining["over"] is False
    assert prior["subscriptions"] == []
    prior_goal = {g["name"]: g for g in prior["goals"]}["trip"]
    assert prior_goal["pace"] == "on_track"

    briefs.add_brief(conn, "prior brief content", prior["as_of"], source="claude",
                      digest_json=json.dumps(_pure(prior)))

    # New recurring charge -> a subscription that didn't exist in the snapshot.
    for i, d_s in enumerate(["2026-08-03", "2026-09-03", "2026-10-03"]):
        _txn(conn, a, d_s, -1599, "netflix.com monthly", f"n{i}")
    # November dining spend that blows past the 250.00 limit -- budget_status is scoped
    # to the digest's current month, so this must land in the same month as `today` below.
    _txn(conn, a, "2026-11-02", -30000, "fancy dinner", "d-nov", category="dining")

    # No more funding, and enough time has passed that the goal now trails pace.
    current = advisor.digest(conn, cfg, today=date(2026, 11, 5))
    changes = current["changes_since_brief"]
    assert changes["has_prior"] is True
    assert changes["since"] == prior["as_of"]

    by_kind = {c["kind"]: c for c in changes["changes"]}
    assert "new_subscription" in by_kind
    assert "netflix.com" in by_kind["new_subscription"]["detail"]

    assert "budget_over" in by_kind
    assert "dining" in by_kind["budget_over"]["detail"]

    assert "goal_off_pace" in by_kind
    assert "trip" in by_kind["goal_off_pace"]["detail"]


# ---- digest() contract growth -------------------------------------------------

def test_digest_stores_pure_snapshot_no_recursive_changes_key(conn):
    d = advisor.digest(conn, _Cfg(), today=date(2026, 3, 15))
    pure = _pure(d)
    assert "changes_since_brief" not in pure
    # round-trips as JSON with no nested changes_since_brief anywhere
    assert "changes_since_brief" not in json.dumps(pure)


# ---- CLI round-trip -----------------------------------------------------------

def test_cli_advice_add_persists_and_reads_prior_snapshot(app_env, tmp_path):
    runner.invoke(app, ["init"])
    brief_file = tmp_path / "brief.md"
    brief_file.write_text("Here is the coaching text.")

    r1 = runner.invoke(app, ["advice", "add", "--file", str(brief_file), "--as-of", "2026-07-06"])
    assert r1.exit_code == 0, r1.output

    conn = dbmod.connect(app_env["db"])
    row1 = conn.execute("SELECT digest_json FROM advisor_brief WHERE id = 1").fetchone()
    assert row1["digest_json"] is not None
    stored = json.loads(row1["digest_json"])
    assert "changes_since_brief" not in stored  # pure snapshot, no recursion
    conn.close()

    # A digest computed after brief #1 now sees it as the prior snapshot.
    dj = runner.invoke(app, ["digest", "--format", "json"])
    assert dj.exit_code == 0, dj.output
    d = json.loads(dj.output)
    assert d["changes_since_brief"]["has_prior"] is True
    assert d["changes_since_brief"]["since"] == "2026-07-06"

    r2 = runner.invoke(app, ["advice", "add", "--file", str(brief_file), "--as-of", "2026-07-07"])
    assert r2.exit_code == 0, r2.output

    conn = dbmod.connect(app_env["db"])
    row2 = conn.execute("SELECT digest_json FROM advisor_brief WHERE id = 2").fetchone()
    assert row2["digest_json"] is not None
    conn.close()
