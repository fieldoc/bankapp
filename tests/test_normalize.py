from types import SimpleNamespace

from bankapp import normalize as nz


def test_norm_desc_collapses_and_lowercases():
    assert nz.norm_desc("  Hello   WORLD ") == "hello world"
    assert nz.norm_desc("TAB\tand\nnewline") == "tab and newline"
    assert nz.norm_desc("Already normal") == "already normal"


def test_dedup_key_deterministic():
    a = nz.content_dedup_key("td-chequing", "2026-01-15", -1234, "CAD", "shoppers drug mart", 0)
    b = nz.content_dedup_key("td-chequing", "2026-01-15", -1234, "CAD", "shoppers drug mart", 0)
    assert a == b
    assert a.startswith("sha256:")


def test_dedup_key_sensitive_to_every_field():
    base = dict(account_key="a", posted_date="2026-01-15", amount_minor=-100,
                currency="CAD", desc_norm="x", occurrence=0)
    baseline = nz.content_dedup_key(**base)
    for field, newval in [
        ("account_key", "b"),
        ("posted_date", "2026-01-16"),
        ("amount_minor", -101),
        ("currency", "USD"),
        ("desc_norm", "y"),
        ("occurrence", 1),
    ]:
        variant = dict(base)
        variant[field] = newval
        assert nz.content_dedup_key(**variant) != baseline, f"{field} did not change the hash"


def test_occurrences_identical_pair():
    keys = [("a", "d", -1, "x"), ("a", "d", -1, "x")]
    assert nz.occurrences_for(keys) == [0, 1]


def test_occurrences_rerun_same_values():
    keys = [("a", "d", -1, "x"), ("a", "d", -1, "x"), ("a", "d", -2, "x")]
    first = nz.occurrences_for(keys)
    second = nz.occurrences_for(keys)
    assert first == second == [0, 1, 0]


def test_occurrences_interleaving_does_not_disturb():
    # A, B, A -> the second A is occurrence 1 even with B in between.
    keys = [("A",), ("B",), ("A",)]
    assert nz.occurrences_for(keys) == [0, 0, 1]


def test_assign_occurrences_mutates_objects():
    txns = [
        SimpleNamespace(account_key="a", posted_date="d", amount_minor=-1, desc_norm="x"),
        SimpleNamespace(account_key="a", posted_date="d", amount_minor=-1, desc_norm="x"),
        SimpleNamespace(account_key="a", posted_date="d", amount_minor=-9, desc_norm="x"),
    ]
    nz.assign_occurrences(txns)
    assert [t.occurrence for t in txns] == [0, 1, 0]
