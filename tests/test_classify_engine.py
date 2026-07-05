import pytest

from bankapp.classify import engine
from bankapp.classify.engine import Rule


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
        _r(3, "substring", "foo", "a", prio=100),  # same priority, lower id wins
    ]
    assert engine.RuleEngine(rules).match("foobar").category == "a"


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


def test_upsert_seed_rules(conn):
    n = engine.upsert_seed_rules(conn, ["tfr-to", "tfr-fr"])
    assert n == 2
    rows = conn.execute("SELECT pattern, role_hint, source, priority FROM rules ORDER BY pattern").fetchall()
    assert rows[0]["role_hint"] == "transfer"
    assert rows[0]["source"] == "seed"
    assert rows[0]["priority"] == engine.SEED_PRIORITY
    # idempotent
    assert engine.upsert_seed_rules(conn, ["tfr-to", "tfr-fr"]) == 0
