import pytest

from bankapp.classify import engine
from bankapp.classify.engine import Rule
from tests.conftest import insert_account, insert_raw_txn


def _r(id, kind, pat, cat=None, role=None, prio=100):
    return Rule(id, kind, pat, cat, role, None, prio)


def test_substring_match():
    eng = engine.RuleEngine([_r(1, "substring", "netflix", "subscriptions")])
    assert eng.match("netflix.com 866-579").category == "subscriptions"
    assert eng.match("spotify premium") is None


def test_regex_match_compiled_once():
    eng = engine.RuleEngine([_r(1, "regex", r"tim hortons #\d+", "dining")])
    assert eng.match("tim hortons #4821").category == "dining"
    assert eng.match("tim hortons downtown") is None


def test_first_match_by_priority_then_id():
    rules = [
        _r(2, "substring", "amzn", "shopping", prio=100),
        _r(1, "substring", "amzn", "subscriptions", prio=50),  # lower priority wins
    ]
    assert engine.RuleEngine(rules).match("amzn mktp ca").category == "subscriptions"


def test_priority_tie_breaks_on_id():
    rules = [
        _r(5, "substring", "foo", "b", prio=100),
        _r(3, "substring", "foo", "a", prio=100),  # same priority + length, lower id wins
    ]
    assert engine.RuleEngine(rules).match("foobar").category == "a"


def test_equal_priority_longer_pattern_wins():
    """At equal priority a more specific (longer) pattern wins, even when the
    generic pattern has the LOWER id (was inserted first)."""
    rules = [
        _r(1, "substring", "uber", "transport", prio=100),    # generic, older
        _r(9, "substring", "uber eats", "dining", prio=100),  # specific, newer
    ]
    eng = engine.RuleEngine(rules)
    assert eng.match("uber eats toronto").category == "dining"
    assert eng.match("uber trip 123").category == "transport"


def test_priority_still_dominates_pattern_length():
    """Priority beats specificity: a short lower-priority-number pattern wins over
    a longer higher-priority-number one."""
    rules = [
        _r(1, "substring", "uber eats toronto on", "dining", prio=100),
        _r(2, "substring", "uber", "transport", prio=50),  # lower number wins
    ]
    assert engine.RuleEngine(rules).match("uber eats toronto on").category == "transport"


def test_validate_rejects_bad_regex():
    with pytest.raises(engine.InvalidPatternError):
        engine.validate_pattern("regex", "([unclosed")


def test_validate_rejects_unknown_kind():
    with pytest.raises(engine.InvalidPatternError):
        engine.validate_pattern("fuzzy", "x")


def test_add_rule_stores_lowercase_and_dedups(conn):
    assert engine.add_rule(conn, "substring", "NETFLIX", "subscriptions") is True
    stored = conn.execute("SELECT pattern FROM rules").fetchone()[0]
    assert stored == "netflix"
    # duplicate (same kind+pattern after lowercasing) -> friendly no-op
    assert engine.add_rule(conn, "substring", "netflix", "subscriptions") is False
    assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 1


def test_add_rule_invalid_regex_raises(conn):
    with pytest.raises(engine.InvalidPatternError):
        engine.add_rule(conn, "regex", "([bad")


# ---- add_rule normalization ------------------------------------------------

def test_add_rule_regex_not_lowercased(conn):
    r"""Lowercasing a regex corrupts escapes (\D -> \d flips meaning, [A-Z] -> [a-z]).
    Regex patterns must be stored exactly as authored."""
    assert engine.add_rule(conn, "regex", r"CHV\D+\d{4}", "fees") is True
    stored = conn.execute("SELECT pattern FROM rules").fetchone()[0]
    assert stored == r"CHV\D+\d{4}"


def test_regex_rules_match_case_insensitively(conn):
    """description_norm is always lowercase; an upper-case-authored regex must still
    match it (compiled with re.IGNORECASE)."""
    eng = engine.RuleEngine([_r(1, "regex", r"CHV\d+", "fees")])
    assert eng.match("chv12345 payment").category == "fees"


def test_existing_lowercase_regex_behaves_identically(conn):
    """The stored live regex chv\\d+ (already lowercase) must keep matching."""
    eng = engine.RuleEngine([_r(1, "regex", r"chv\d+", "fees")])
    assert eng.match("chv12345 payment").category == "fees"
    assert eng.match("chv payment") is None


def test_add_rule_substring_collapses_internal_whitespace(conn):
    """description_norm never contains double spaces, so a double-space pattern could
    never match; internal runs collapse to a single space."""
    assert engine.add_rule(conn, "substring", "uber   eats", "dining") is True
    stored = conn.execute("SELECT pattern FROM rules").fetchone()[0]
    assert stored == "uber eats"


def test_add_rule_substring_preserves_leading_trailing_space(conn):
    """A single leading/trailing space is a deliberate word-boundary guard
    (e.g. 'pho ' must not match 'phone') and must survive storage."""
    assert engine.add_rule(conn, "substring", "pho ", "dining") is True
    stored = conn.execute("SELECT pattern FROM rules").fetchone()[0]
    assert stored == "pho "
    eng = engine.RuleEngine(engine.load_rules(conn))
    assert eng.match("pho house on main").category == "dining"
    assert eng.match("phone bill") is None


def test_add_rule_rejects_empty_and_whitespace_only_patterns(conn):
    with pytest.raises(engine.InvalidPatternError):
        engine.add_rule(conn, "substring", "")
    with pytest.raises(engine.InvalidPatternError):
        engine.add_rule(conn, "substring", "   ")


def test_upsert_seed_rules(conn):
    n = engine.upsert_seed_rules(conn, ["tfr-to", "tfr-fr"])
    assert n == 2
    rows = conn.execute("SELECT pattern, role_hint, source, priority FROM rules ORDER BY pattern").fetchall()
    assert rows[0]["role_hint"] == "transfer"
    assert rows[0]["source"] == "seed"
    assert rows[0]["priority"] == engine.SEED_PRIORITY
    # idempotent
    assert engine.upsert_seed_rules(conn, ["tfr-to", "tfr-fr"]) == 0


# ---- manual one-off overrides ----------------------------------------------

def test_set_manual_category_marks_source_manual(conn):
    aid = insert_account(conn)
    tid = insert_raw_txn(conn, aid, description_norm="obscure vendor 123", dedup_key="sha256:m1")
    engine.set_manual_category(conn, tid, "dining", role_hint="expense")
    row = conn.execute(
        "SELECT category, role_hint, rule_id, source FROM txn_interp WHERE raw_txn_id = ?", (tid,)
    ).fetchone()
    assert row["category"] == "dining"
    assert row["role_hint"] == "expense"
    assert row["rule_id"] is None
    assert row["source"] == "manual"


def test_manual_override_survives_recompute_all(conn):
    """A one-off manual category must not be deleted or overwritten by
    `categorize(recompute_all=True)`, even though no rule matches it."""
    aid = insert_account(conn)
    tid = insert_raw_txn(conn, aid, description_norm="one-time thing", dedup_key="sha256:m2")
    engine.set_manual_category(conn, tid, "gifts")
    assert engine.categorize(conn, recompute_all=True) == 0  # rule set is empty
    row = conn.execute(
        "SELECT category, source FROM txn_interp WHERE raw_txn_id = ?", (tid,)
    ).fetchone()
    assert row is not None and row["category"] == "gifts" and row["source"] == "manual"


def test_manual_override_wins_over_matching_rule(conn):
    """A manual override is preferred even when a rule also matches the description."""
    aid = insert_account(conn)
    tid = insert_raw_txn(conn, aid, description_norm="starbucks #42", dedup_key="sha256:m3")
    engine.set_manual_category(conn, tid, "coffee-manual")
    engine.add_rule(conn, "substring", "starbucks", "dining")
    engine.categorize(conn, recompute_all=True)
    row = conn.execute(
        "SELECT category, source FROM txn_interp WHERE raw_txn_id = ?", (tid,)
    ).fetchone()
    assert row["category"] == "coffee-manual"
    assert row["source"] == "manual"


def test_manual_override_leaves_review_queue(conn):
    from bankapp.classify import review

    aid = insert_account(conn)
    tid = insert_raw_txn(conn, aid, description_norm="mystery charge", dedup_key="sha256:m4")
    assert review.count(conn) == 1
    engine.set_manual_category(conn, tid, "misc")
    assert review.count(conn) == 0
