"""Goal CRUD + validation. Real sqlite, no mocks."""

from __future__ import annotations

from datetime import date

import pytest

from bankapp import goals
from bankapp.config import GoalConfig


def _mk(conn, **kw):
    kw.setdefault("name", "trip")
    kw.setdefault("target_minor", 300000)
    kw.setdefault("currency", "CAD")
    kw.setdefault("start_date", "2026-01-01")
    kw.setdefault("target_date", "2026-12-31")
    kw.setdefault("allocation_pct", 100)
    kw.setdefault("note", None)
    return goals.create(conn, **kw)


def _raw_insert(conn, name, pct, currency="CAD", active=1):
    conn.execute(
        "INSERT INTO goals(name, target_minor, currency, start_date, target_date, "
        "allocation_pct, note, active) VALUES (?,?,?,?,?,?,?,?)",
        (name, 100000, currency, "2026-01-01", None, pct, None, active),
    )
    conn.commit()
    return conn.execute("SELECT id FROM goals WHERE name = ?", (name,)).fetchone()[0]


def _cfg(name="trip", alloc=100, target=300000, currency="CAD"):
    return GoalConfig(name=name, target_minor=target, currency=currency,
                      start_date="2026-01-01", target_date="2026-12-31",
                      allocation_pct=alloc, note=None)


# ---- reads + field validation ----------------------------------------------

def test_list_goals_empty(conn):
    assert goals.list_goals(conn) == []


def test_get_returns_none_for_unknown_id(conn):
    assert goals.get(conn, 999) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "   "),
        ("target_minor", 0),
        ("target_minor", -5),
        ("currency", "XYZ"),
        ("start_date", "01-01-2026"),
        ("target_date", "2025-12-31"),  # before start_date
        ("allocation_pct", -1),
        ("allocation_pct", 101),
    ],
)
def test_check_fields_rejects(field, value):
    kw = dict(name="trip", target_minor=300000, currency="CAD",
              start_date="2026-01-01", target_date="2026-12-31", allocation_pct=100)
    kw[field] = value
    with pytest.raises(goals.ValidationError):
        goals.check_fields(**kw)


def test_check_fields_allows_absent_target_date():
    goals.check_fields(name="trip", target_minor=1, currency="CAD",
                       start_date="2026-01-01", target_date=None, allocation_pct=0)


# ---- funding_mode / monthly_minor / priority validation ---------------------

def test_check_fields_rejects_bad_funding_mode():
    with pytest.raises(goals.ValidationError):
        goals.check_fields(name="trip", target_minor=1, currency="CAD",
                           start_date="2026-01-01", target_date=None, allocation_pct=100,
                           funding_mode="weekly")


def test_check_fields_fixed_monthly_requires_monthly_minor_positive():
    with pytest.raises(goals.ValidationError):
        goals.check_fields(name="emergency", target_minor=0, currency="CAD",
                           start_date="2026-01-01", target_date=None, allocation_pct=100,
                           funding_mode="fixed_monthly", monthly_minor=0)


def test_check_fields_fixed_monthly_rejects_missing_monthly_minor():
    with pytest.raises(goals.ValidationError):
        goals.check_fields(name="emergency", target_minor=0, currency="CAD",
                           start_date="2026-01-01", target_date=None, allocation_pct=100,
                           funding_mode="fixed_monthly", monthly_minor=None)


def test_check_fields_fixed_monthly_rejects_bool_monthly_minor():
    with pytest.raises(goals.ValidationError):
        goals.check_fields(name="emergency", target_minor=0, currency="CAD",
                           start_date="2026-01-01", target_date=None, allocation_pct=100,
                           funding_mode="fixed_monthly", monthly_minor=True)


def test_check_fields_fixed_monthly_allows_target_zero():
    # 0 = perpetual bucket, no progress %.
    goals.check_fields(name="emergency", target_minor=0, currency="CAD",
                       start_date="2026-01-01", target_date=None, allocation_pct=100,
                       funding_mode="fixed_monthly", monthly_minor=50000)


def test_check_fields_fixed_monthly_rejects_negative_target():
    with pytest.raises(goals.ValidationError):
        goals.check_fields(name="emergency", target_minor=-1, currency="CAD",
                           start_date="2026-01-01", target_date=None, allocation_pct=100,
                           funding_mode="fixed_monthly", monthly_minor=50000)


def test_check_fields_target_date_mode_rejects_monthly_minor_set():
    with pytest.raises(goals.ValidationError):
        goals.check_fields(name="trip", target_minor=300000, currency="CAD",
                           start_date="2026-01-01", target_date="2026-12-31",
                           allocation_pct=100, funding_mode="target_date",
                           monthly_minor=50000)


def test_check_fields_target_date_mode_still_rejects_target_zero():
    with pytest.raises(goals.ValidationError):
        goals.check_fields(name="trip", target_minor=0, currency="CAD",
                           start_date="2026-01-01", target_date=None, allocation_pct=100,
                           funding_mode="target_date")


@pytest.mark.parametrize("priority", [-1, 1000])
def test_check_fields_rejects_priority_out_of_range(priority):
    with pytest.raises(goals.ValidationError):
        goals.check_fields(name="trip", target_minor=1, currency="CAD",
                           start_date="2026-01-01", target_date=None, allocation_pct=100,
                           priority=priority)


def test_check_fields_rejects_bool_priority():
    with pytest.raises(goals.ValidationError):
        goals.check_fields(name="trip", target_minor=1, currency="CAD",
                           start_date="2026-01-01", target_date=None, allocation_pct=100,
                           priority=True)


@pytest.mark.parametrize("priority", [0, 999])
def test_check_fields_allows_priority_boundaries(priority):
    goals.check_fields(name="trip", target_minor=1, currency="CAD",
                       start_date="2026-01-01", target_date=None, allocation_pct=100,
                       priority=priority)


# ---- allocation headroom + name uniqueness ---------------------------------

def test_headroom_is_100_on_empty_db(conn):
    assert goals.allocation_headroom(conn, "CAD") == 100


def test_headroom_counts_only_same_currency(conn):
    _raw_insert(conn, "cad-goal", 80, currency="CAD")
    assert goals.allocation_headroom(conn, "CAD") == 20
    assert goals.allocation_headroom(conn, "USD") == 100


def test_headroom_ignores_archived(conn):
    _raw_insert(conn, "old", 100, active=0)
    assert goals.allocation_headroom(conn, "CAD") == 100


def test_headroom_excludes_self(conn):
    gid = _raw_insert(conn, "trip", 100)
    assert goals.allocation_headroom(conn, "CAD") == 0
    assert goals.allocation_headroom(conn, "CAD", exclude_id=gid) == 100


def test_check_allocation_message_names_the_headroom(conn):
    _raw_insert(conn, "trip", 85)
    with pytest.raises(goals.AllocationError) as exc:
        goals.check_allocation(conn, "CAD", 20)
    assert "CAD is 85% allocated" in str(exc.value)
    assert "at most 15%" in str(exc.value)


def test_check_name_free(conn):
    gid = _raw_insert(conn, "trip", 10)
    with pytest.raises(goals.DuplicateName):
        goals.check_name_free(conn, "trip")
    goals.check_name_free(conn, "trip", exclude_id=gid)  # renaming to itself is fine
    goals.check_name_free(conn, "other")


def test_check_name_free_sees_archived_names(conn):
    # the column is UNIQUE, so an archived name is still taken
    _raw_insert(conn, "trip", 10, active=0)
    with pytest.raises(goals.DuplicateName):
        goals.check_name_free(conn, "trip")


# ---- writes -----------------------------------------------------------------

def test_create_then_get_round_trip(conn):
    gid = _mk(conn, note="hello")
    g = goals.get(conn, gid)
    assert (g.name, g.target_minor, g.currency, g.allocation_pct) == ("trip", 300000, "CAD", 100)
    assert g.active is True
    assert g.note == "hello"


def test_create_defaults_funding_mode_and_priority(conn):
    gid = _mk(conn)
    g = goals.get(conn, gid)
    assert g.funding_mode == "target_date"
    assert g.monthly_minor is None
    assert g.priority == 100


def test_create_fixed_monthly_goal_round_trip(conn):
    gid = goals.create(conn, name="emergency-fund", target_minor=0, currency="CAD",
                       start_date="2026-07-01", funding_mode="fixed_monthly",
                       monthly_minor=50000, priority=10)
    g = goals.get(conn, gid)
    assert g.funding_mode == "fixed_monthly"
    assert g.monthly_minor == 50000
    assert g.priority == 10
    assert g.target_minor == 0


def test_update_threads_funding_fields(conn):
    gid = _mk(conn)
    goals.update(conn, gid, name="trip", target_minor=300000, currency="CAD",
                start_date="2026-01-01", target_date="2026-12-31",
                allocation_pct=100, note=None, funding_mode="fixed_monthly",
                monthly_minor=25000, priority=5)
    g = goals.get(conn, gid)
    assert g.funding_mode == "fixed_monthly"
    assert g.monthly_minor == 25000
    assert g.priority == 5


def test_create_strips_name(conn):
    gid = _mk(conn, name="  trip  ")
    assert goals.get(conn, gid).name == "trip"


def test_create_rejects_duplicate_name(conn):
    _mk(conn, allocation_pct=50)
    with pytest.raises(goals.DuplicateName):
        _mk(conn, allocation_pct=50)


def test_create_rejects_allocation_breach(conn):
    _mk(conn, name="a", allocation_pct=60)
    with pytest.raises(goals.AllocationError):
        _mk(conn, name="b", allocation_pct=60)


def test_cad_and_usd_may_each_take_100_pct(conn):
    """Decision 3: allocation is a share of the goal's own currency pool."""
    _mk(conn, name="cad-trip", currency="CAD", allocation_pct=100)
    _mk(conn, name="usd-trip", currency="USD", allocation_pct=100)
    assert len(goals.list_goals(conn)) == 2


def test_update_can_lower_its_own_allocation(conn):
    """Headroom must exclude the goal under edit, or 100 -> 90 self-collides."""
    gid = _mk(conn, allocation_pct=100)
    goals.update(conn, gid, name="trip", target_minor=300000, currency="CAD",
                 start_date="2026-01-01", target_date="2026-12-31",
                 allocation_pct=90, note=None)
    assert goals.get(conn, gid).allocation_pct == 90


def test_update_can_rename(conn):
    gid = _mk(conn)
    goals.update(conn, gid, name="safari", target_minor=300000, currency="CAD",
                 start_date="2026-01-01", target_date="2026-12-31",
                 allocation_pct=100, note=None)
    assert goals.get(conn, gid).name == "safari"


def test_update_unknown_id_raises_not_found(conn):
    with pytest.raises(goals.NotFound):
        goals.update(conn, 999, name="x", target_minor=1, currency="CAD",
                     start_date="2026-01-01", target_date=None,
                     allocation_pct=1, note=None)


def test_archive_hides_and_frees_allocation(conn):
    gid = _mk(conn, allocation_pct=100)
    goals.archive(conn, gid)
    assert goals.list_goals(conn) == []
    assert goals.get(conn, gid).active is False
    assert goals.allocation_headroom(conn, "CAD") == 100


def test_archive_is_idempotent(conn):
    gid = _mk(conn)
    goals.archive(conn, gid)
    goals.archive(conn, gid)
    assert goals.get(conn, gid).active is False


def test_unarchive_restores(conn):
    gid = _mk(conn)
    goals.archive(conn, gid)
    goals.unarchive(conn, gid)
    assert goals.get(conn, gid).active is True


def test_unarchive_rejects_allocation_breach(conn):
    """Unarchiving re-spends allocation, so it must be re-checked."""
    gid = _mk(conn, name="old", allocation_pct=100)
    goals.archive(conn, gid)
    _mk(conn, name="new", allocation_pct=100)
    with pytest.raises(goals.AllocationError):
        goals.unarchive(conn, gid)


def test_archive_unknown_id_raises_not_found(conn):
    with pytest.raises(goals.NotFound):
        goals.archive(conn, 999)


def test_failed_create_leaves_no_row(conn):
    _mk(conn, name="a", allocation_pct=60)
    with pytest.raises(goals.AllocationError):
        _mk(conn, name="b", allocation_pct=60)
    assert [g.name for g in goals.list_goals(conn)] == ["a"]


# ---- config seeding ---------------------------------------------------------

def test_seed_inserts_and_reports_count(conn):
    # rowcount is unreliable for ON CONFLICT DO NOTHING; pin the real number.
    assert goals.seed_from_config(conn, [_cfg("a", 50), _cfg("b", 50)]) == 2
    assert len(goals.list_goals(conn)) == 2


def test_seed_twice_is_a_no_op(conn):
    goals.seed_from_config(conn, [_cfg()])
    assert goals.seed_from_config(conn, [_cfg()]) == 0
    assert len(goals.list_goals(conn)) == 1


def test_seed_does_not_raise_duplicate_name(conn):
    """A name collision during seeding is the EXPECTED case, not an error."""
    goals.seed_from_config(conn, [_cfg()])
    goals.seed_from_config(conn, [_cfg()])  # must not raise


def test_seed_does_not_clobber_a_ui_edit(conn):
    """Decision 1: the DB owns a goal's values; config only seeds new names."""
    goals.seed_from_config(conn, [_cfg(target=300000)])
    gid = goals.list_goals(conn)[0].id
    goals.update(conn, gid, name="trip", target_minor=999, currency="CAD",
                 start_date="2026-01-01", target_date="2026-12-31",
                 allocation_pct=100, note="mine")
    goals.seed_from_config(conn, [_cfg(target=300000)])
    g = goals.get(conn, gid)
    assert g.target_minor == 999
    assert g.note == "mine"


def test_seed_does_not_resurrect_an_archived_goal(conn):
    """Decision 2: archiving survives `finance init`."""
    goals.seed_from_config(conn, [_cfg()])
    gid = goals.list_goals(conn)[0].id
    goals.archive(conn, gid)
    goals.seed_from_config(conn, [_cfg()])
    assert goals.get(conn, gid).active is False
    assert goals.list_goals(conn) == []


def test_seed_rejects_allocation_breach_and_rolls_back(conn):
    with pytest.raises(goals.AllocationError):
        goals.seed_from_config(conn, [_cfg("a", 60), _cfg("b", 60)])
    assert goals.list_goals(conn, include_archived=True) == []


def test_seed_allows_100_pct_in_each_currency(conn):
    assert goals.seed_from_config(conn, [_cfg("c", 100, currency="CAD"),
                                         _cfg("u", 100, currency="USD")]) == 2


def test_seed_validates_fields(conn):
    with pytest.raises(goals.ValidationError):
        goals.seed_from_config(conn, [_cfg(target=0)])


# ---- seed ledger: a config goal is seeded ONCE, ever ------------------------
# Config seeds by name, but the app lets you rename. Without a ledger of names
# already seeded, renaming a config goal makes the next `finance init` no longer
# recognize it and re-insert the config version -- silently duplicating the goal,
# or blowing the allocation cap when the two together exceed 100%.

def test_seed_does_not_resurrect_a_renamed_goal(conn):
    goals.seed_from_config(conn, [_cfg("example-trip", alloc=60)])
    gid = goals.list_goals(conn)[0].id
    goals.update(conn, gid, name="japan-trip", target_minor=420000, currency="CAD",
                 start_date="2026-01-01", target_date="2026-12-31",
                 allocation_pct=60, note=None)

    assert goals.seed_from_config(conn, [_cfg("example-trip", alloc=60)]) == 0
    assert [g.name for g in goals.list_goals(conn)] == ["japan-trip"]


def test_seed_of_renamed_goal_does_not_break_the_allocation_cap(conn):
    """The 120% failure that a rename used to cause in `finance init`."""
    goals.seed_from_config(conn, [_cfg("example-trip", alloc=60)])
    gid = goals.list_goals(conn)[0].id
    goals.update(conn, gid, name="japan-trip", target_minor=300000, currency="CAD",
                 start_date="2026-01-01", target_date="2026-12-31",
                 allocation_pct=60, note=None)
    goals.seed_from_config(conn, [_cfg("example-trip", alloc=60)])  # must not raise


def test_seed_ledger_records_preexisting_names(conn):
    """A goal already in the DB (seeded before the ledger existed) is adopted, not
    re-inserted, and is remembered so a later rename is safe."""
    _raw_insert(conn, "example-trip", 60)
    assert goals.seed_from_config(conn, [_cfg("example-trip", alloc=60)]) == 0
    gid = goals.list_goals(conn)[0].id
    goals.update(conn, gid, name="renamed", target_minor=100000, currency="CAD",
                 start_date="2026-01-01", target_date=None, allocation_pct=60, note=None)
    assert goals.seed_from_config(conn, [_cfg("example-trip", alloc=60)]) == 0
    assert [g.name for g in goals.list_goals(conn)] == ["renamed"]


def test_seed_ledger_rolls_back_with_the_inserts(conn):
    with pytest.raises(goals.AllocationError):
        goals.seed_from_config(conn, [_cfg("a", 60), _cfg("b", 60)])
    # ledger must not remember 'a'/'b', or a corrected config could never seed them
    assert goals.seed_from_config(conn, [_cfg("a", 50), _cfg("b", 50)]) == 2


def test_seed_from_config_threads_fixed_monthly_goal(conn):
    fixed_cfg = GoalConfig(
        name="emergency-fund", target_minor=0, currency="CAD",
        start_date="2026-07-01", target_date=None, allocation_pct=100, note=None,
        funding_mode="fixed_monthly", monthly_minor=50000, priority=10,
    )
    assert goals.seed_from_config(conn, [fixed_cfg]) == 1
    g = goals.list_goals(conn)[0]
    assert g.funding_mode == "fixed_monthly"
    assert g.monthly_minor == 50000
    assert g.priority == 10
    assert g.target_minor == 0


# ---- monthly_ask --------------------------------------------------------------

def test_monthly_ask_fixed_monthly_passthrough():
    assert goals.monthly_ask(
        funding_mode="fixed_monthly", monthly_minor=50000, target_minor=999999,
        funded_minor=0, target_date=None, today=date(2026, 7, 12),
    ) == 50000


def test_monthly_ask_fixed_monthly_none_defaults_to_zero():
    assert goals.monthly_ask(
        funding_mode="fixed_monthly", monthly_minor=None, target_minor=0,
        funded_minor=0, target_date=None, today=date(2026, 7, 12),
    ) == 0


def test_monthly_ask_target_mode_no_target_date_is_zero():
    assert goals.monthly_ask(
        funding_mode="target_date", monthly_minor=None, target_minor=100000,
        funded_minor=0, target_date=None, today=date(2026, 7, 12),
    ) == 0


def test_monthly_ask_target_mode_zero_target_is_zero():
    assert goals.monthly_ask(
        funding_mode="target_date", monthly_minor=None, target_minor=0,
        funded_minor=0, target_date="2026-12-01", today=date(2026, 7, 12),
    ) == 0


def test_monthly_ask_funded_at_or_past_target_is_zero():
    assert goals.monthly_ask(
        funding_mode="target_date", monthly_minor=None, target_minor=100000,
        funded_minor=100000, target_date="2026-12-01", today=date(2026, 7, 12),
    ) == 0
    assert goals.monthly_ask(
        funding_mode="target_date", monthly_minor=None, target_minor=100000,
        funded_minor=150000, target_date="2026-12-01", today=date(2026, 7, 12),
    ) == 0


def test_monthly_ask_months_left_counts_current_month():
    # today 2026-07-12, target 2026-12-01 -> months_left = 6 (Jul..Dec inclusive).
    assert goals.monthly_ask(
        funding_mode="target_date", monthly_minor=None, target_minor=100000,
        funded_minor=40000, target_date="2026-12-01", today=date(2026, 7, 12),
    ) == 10000  # ceil(60000 / 6)


def test_monthly_ask_target_this_month_is_whole_remaining():
    assert goals.monthly_ask(
        funding_mode="target_date", monthly_minor=None, target_minor=50000,
        funded_minor=20000, target_date="2026-07-20", today=date(2026, 7, 12),
    ) == 30000  # months_left clamps to 1 -> whole remaining


def test_monthly_ask_past_target_clamps_to_whole_remaining():
    assert goals.monthly_ask(
        funding_mode="target_date", monthly_minor=None, target_minor=50000,
        funded_minor=10000, target_date="2026-01-01", today=date(2026, 7, 12),
    ) == 40000  # months_left would be negative -> clamped to 1


def test_monthly_ask_uses_ceil_division():
    # remaining=100, months_left=3 -> ceil(100/3) = 34, not 33.
    assert goals.monthly_ask(
        funding_mode="target_date", monthly_minor=None, target_minor=100100,
        funded_minor=100000, target_date="2026-09-01", today=date(2026, 7, 12),
    ) == 34
