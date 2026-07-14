import random

from bankapp.match.transfers import Leg, Pair, pair_legs


def L(id, acct, date, amt, currency="CAD", desc=""):
    return Leg(id, acct, date, amt, currency, desc)


def test_exact_match():
    legs = [L(1, 10, "2026-01-15", -50000), L(2, 20, "2026-01-15", 50000)]
    assert pair_legs(legs, window_days=7, tolerance_minor=0) == [Pair(1, 2)]


def test_either_date_order_td_lag():
    # out posts AFTER the in (TD batch lag)
    legs = [L(1, 10, "2026-01-18", -50000), L(2, 20, "2026-01-15", 50000)]
    assert pair_legs(legs, window_days=7, tolerance_minor=0) == [Pair(1, 2)]


def test_window_boundary_inclusive():
    legs = [L(1, 10, "2026-01-22", -50000), L(2, 20, "2026-01-15", 50000)]
    assert pair_legs(legs, window_days=7, tolerance_minor=0) == [Pair(1, 2)]  # exactly 7 days


def test_window_boundary_exclusive():
    legs = [L(1, 10, "2026-01-23", -50000), L(2, 20, "2026-01-15", 50000)]
    assert pair_legs(legs, window_days=7, tolerance_minor=0) == []  # 8 days > window


def test_tolerance_boundary_inclusive():
    # $5 fee tolerance: out -500.00, in +499.50 -> diff 50 minor <= 50
    legs = [L(1, 10, "2026-01-15", -50000), L(2, 20, "2026-01-15", 49950)]
    assert pair_legs(legs, window_days=7, tolerance_minor=50) == [Pair(1, 2)]
    assert pair_legs(legs, window_days=7, tolerance_minor=49) == []


def test_same_account_rejected():
    legs = [L(1, 10, "2026-01-15", -50000), L(2, 10, "2026-01-15", 50000)]
    assert pair_legs(legs, window_days=7, tolerance_minor=0) == []


def test_tie_break_prefers_closest_date_then_amount_then_id():
    # one out, two candidate ins: closest date should win
    out = L(1, 10, "2026-01-15", -50000)
    near = L(2, 20, "2026-01-16", 50000)   # 1 day
    far = L(3, 20, "2026-01-19", 50000)    # 4 days
    assert pair_legs([out, far, near], window_days=7, tolerance_minor=0) == [Pair(1, 2)]


def test_different_currency_never_pairs():
    # $500 USD outflow vs a coincidental $500 CAD inflow, same day: must NOT pair.
    legs = [
        L(1, 10, "2026-01-15", -50000, currency="USD", desc="wire out"),
        L(2, 20, "2026-01-15", 50000, currency="CAD", desc="e-transfer in"),
    ]
    assert pair_legs(legs, window_days=7, tolerance_minor=0) == []


def test_description_overlap_breaks_same_amount_same_day_tie():
    # Two ins tie on date+amount with one out; the in sharing the sub-account code
    # (h62239605cad) must win over the coincidental same-amount leg. Reproduces the
    # group-7 mispairing from the 2026-07-13 review.
    out = L(1, 10, "2026-01-15", -50000,
            desc="money transfer to wealthsimple non-registered h62239605cad")
    right = L(2, 20, "2026-01-15", 50000,
              desc="money transfer from wealthsimple non-registered h62239605cad")
    wrong = L(3, 30, "2026-01-15", 50000,
              desc="e-transfer from someone else abcd1234")
    assert pair_legs([out, wrong, right], window_days=7, tolerance_minor=0) == [Pair(1, 2)]


def test_greedy_one_to_one():
    legs = [
        L(1, 10, "2026-01-15", -50000), L(2, 20, "2026-01-15", 50000),
        L(3, 10, "2026-01-16", -50000), L(4, 20, "2026-01-16", 50000),
    ]
    pairs = pair_legs(legs, window_days=7, tolerance_minor=0)
    matched_ids = {p.out_id for p in pairs} | {p.in_id for p in pairs}
    assert len(pairs) == 2
    assert matched_ids == {1, 2, 3, 4}  # each leg used exactly once


def test_deterministic_under_shuffle():
    legs = [
        L(1, 10, "2026-01-15", -50000), L(2, 20, "2026-01-15", 50000),
        L(3, 10, "2026-01-16", -50000), L(4, 20, "2026-01-17", 50000),
        L(5, 30, "2026-01-15", 50000),
    ]
    baseline = pair_legs(legs, window_days=7, tolerance_minor=0)
    for seed in range(10):
        shuffled = legs[:]
        random.Random(seed).shuffle(shuffled)
        assert pair_legs(shuffled, window_days=7, tolerance_minor=0) == baseline
