"""Rules-first categorization engine + rule persistence.

A persisted rule IS the learn-once cache (manual or Claude verdict). Matching is
deterministic: rules are tried in (priority ASC, pattern length DESC, id ASC) order —
at equal priority, a longer (more specific) pattern wins over a shorter one, and exact
length ties fall back to insertion order (lower id) — first match wins.

Substring patterns are stored lowercase, with internal whitespace runs collapsed to a
single space but a single leading/trailing space preserved (some patterns rely on a
trailing space as a word-boundary guard, e.g. "pho "). Regex patterns are stored
exactly as authored — never lowercased, since that would corrupt escapes like \\D,
\\W, or [A-Z] — and are compiled with re.IGNORECASE so they still match
case-insensitively against description_norm (which is always lowercase).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Optional

from bankapp.ingest.core import _utc_now_iso

SEED_PRIORITY = 50  # seed transfer rules should beat generic manual rules


class InvalidPatternError(ValueError):
    """A rule pattern is not a valid regex, or match_kind is unknown."""


@dataclass(frozen=True)
class Rule:
    id: int
    match_kind: str
    pattern: str
    category: Optional[str]
    role_hint: Optional[str]
    counterparty: Optional[str]
    priority: int


def validate_pattern(match_kind: str, pattern: str) -> None:
    if match_kind not in ("substring", "regex"):
        raise InvalidPatternError(f"match_kind must be 'substring' or 'regex', got {match_kind!r}")
    if not pattern or not pattern.strip():
        raise InvalidPatternError("pattern must not be empty or whitespace-only")
    if match_kind == "regex":
        try:
            re.compile(pattern)
        except re.error as exc:
            raise InvalidPatternError(f"invalid regex {pattern!r}: {exc}") from exc


def match_order_key(rule: Rule) -> tuple:
    """Deterministic match order: priority ASC, then longer (more specific) pattern
    first, then id ASC for exact-length ties."""
    return (rule.priority, -len(rule.pattern), rule.id)


def normalize_substring_pattern(pattern: str) -> str:
    """Store-time normalization for substring patterns: lowercase and collapse internal
    whitespace runs to a single space (description_norm never contains doubles, so a
    double-space pattern could never match), preserving a single leading/trailing
    space — some patterns use a trailing space as a word-boundary guard
    (e.g. "pho " must not match "phone")."""
    lead = " " if pattern[:1].isspace() else ""
    trail = " " if pattern[-1:].isspace() else ""
    return lead + " ".join(pattern.split()).lower() + trail


class RuleEngine:
    """Compiled, ordered rule set. `match` returns the first matching rule or None."""

    def __init__(self, rules: list[Rule]):
        self._rules = sorted(rules, key=match_order_key)
        # description_norm is always lowercase; IGNORECASE keeps regex patterns
        # (stored exactly as authored, possibly with upper-case classes) matching it.
        self._compiled = {
            r.id: re.compile(r.pattern, re.IGNORECASE)
            for r in self._rules
            if r.match_kind == "regex"
        }

    def match(self, description_norm: str) -> Optional[Rule]:
        for r in self._rules:
            if r.match_kind == "substring":
                if r.pattern in description_norm:
                    return r
            else:  # regex
                if self._compiled[r.id].search(description_norm):
                    return r
        return None


def load_rules(conn: sqlite3.Connection) -> list[Rule]:
    rows = conn.execute(
        "SELECT id, match_kind, pattern, category, role_hint, counterparty, priority FROM rules"
    ).fetchall()
    return [
        Rule(r["id"], r["match_kind"], r["pattern"], r["category"], r["role_hint"],
             r["counterparty"], r["priority"])
        for r in rows
    ]


def add_rule(
    conn: sqlite3.Connection,
    match_kind: str,
    pattern: str,
    category: Optional[str] = None,
    role_hint: Optional[str] = None,
    counterparty: Optional[str] = None,
    priority: int = 100,
    source: str = "manual",
) -> bool:
    """Add a rule. Returns True if inserted, False if it already existed (friendly no-op).

    Validates the pattern (invalid regex / empty rejected here). Substring patterns are
    normalized via normalize_substring_pattern; regex patterns are stored exactly as
    authored (lowercasing would corrupt escapes like \\D or [A-Z]) and matched
    case-insensitively by the engine.
    """
    validate_pattern(match_kind, pattern)
    if match_kind == "substring":
        pattern = normalize_substring_pattern(pattern)
    before = conn.total_changes
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO rules
                 (match_kind, pattern, category, role_hint, counterparty, priority, source, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (match_kind, pattern, category, role_hint, counterparty, priority, source, _utc_now_iso()),
        )
    return conn.total_changes > before


def remove_rule(conn: sqlite3.Connection, match_kind: str, pattern: str) -> bool:
    """Delete a rule by its (match_kind, pattern) identity. Returns True if a row went.

    Substring patterns are looked up through the same store-time normalization as
    add_rule, so callers can pass the pattern as they typed it. Deleting a rule does
    not touch txn_interp — run categorize(recompute_all=True) afterwards to drop the
    labels that no longer match (manual overrides survive that, by design).
    """
    if match_kind == "substring":
        pattern = normalize_substring_pattern(pattern)
    row = conn.execute(
        "SELECT id FROM rules WHERE match_kind = ? AND pattern = ?", (match_kind, pattern)
    ).fetchone()
    if row is None:
        return False
    with conn:  # txn_interp.rule_id references rules(id): detach before deleting
        conn.execute("UPDATE txn_interp SET rule_id = NULL WHERE rule_id = ?", (row["id"],))
        conn.execute("DELETE FROM rules WHERE id = ?", (row["id"],))
    return True


def set_rule_counterparty(
    conn: sqlite3.Connection, match_kind: str, pattern: str, counterparty: Optional[str]
) -> bool:
    """Set (or clear, with None) a rule's counterparty. Returns True if the rule exists.

    counterparty is the canonical merchant identity used to merge vendor renames in
    the subscriptions/leaks reports. Flows into txn_interp on the next categorize
    (recompute_all=True re-stamps existing matches).
    """
    if match_kind == "substring":
        pattern = normalize_substring_pattern(pattern)
    with conn:
        cur = conn.execute(
            "UPDATE rules SET counterparty = ? WHERE match_kind = ? AND pattern = ?",
            (counterparty, match_kind, pattern),
        )
    return cur.rowcount > 0


def upsert_seed_rules(conn: sqlite3.Connection, seed_patterns) -> int:
    """Upsert transfer seed rules from config [transfers].seed_patterns."""
    added = 0
    for p in seed_patterns:
        if add_rule(conn, "substring", p, role_hint="transfer", priority=SEED_PRIORITY, source="seed"):
            added += 1
    return added


def set_manual_category(
    conn: sqlite3.Connection,
    raw_txn_id: int,
    category: str,
    role_hint: Optional[str] = None,
    counterparty: Optional[str] = None,
) -> None:
    """Set a one-off manual category override for a single txn (no rule created).

    Marked source='manual' so it wins over rules and survives `categorize --all`
    (see categorize()). Upserts the txn_interp row and clears any prior rule_id.
    """
    with conn:
        conn.execute(
            """INSERT INTO txn_interp(raw_txn_id, category, role_hint, counterparty,
                                      rule_id, source, updated_at)
               VALUES (?,?,?,?,NULL,'manual',?)
               ON CONFLICT(raw_txn_id) DO UPDATE SET
                 category=excluded.category, role_hint=excluded.role_hint,
                 counterparty=excluded.counterparty, rule_id=NULL,
                 source='manual', updated_at=excluded.updated_at""",
            (raw_txn_id, category, role_hint, counterparty, _utc_now_iso()),
        )


def categorize(conn: sqlite3.Connection, recompute_all: bool = False) -> int:
    """Apply rules to raw_txn, writing verdicts to txn_interp. Idempotent.

    Default: only txns with no txn_interp row yet. `recompute_all`: re-evaluate every
    txn against the current rules (safe — interpretation only; raw_txn is never touched)
    and drop rows that no longer match. Returns the number of txns categorized.

    Manual overrides (source='manual', set via set_manual_category) are never touched:
    they win over rules and survive --all.
    """
    engine = RuleEngine(load_rules(conn))
    if recompute_all:
        rows = conn.execute(
            """SELECT id, description_norm FROM raw_txn
               WHERE id NOT IN (SELECT raw_txn_id FROM txn_interp WHERE source='manual')"""
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT r.id, r.description_norm
               FROM raw_txn r LEFT JOIN txn_interp i ON i.raw_txn_id = r.id
               WHERE i.raw_txn_id IS NULL"""
        ).fetchall()

    now = _utc_now_iso()
    categorized = 0
    with conn:
        for row in rows:
            m = engine.match(row["description_norm"])
            if m is not None:
                conn.execute(
                    """INSERT INTO txn_interp(raw_txn_id, category, role_hint, counterparty, rule_id, updated_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(raw_txn_id) DO UPDATE SET
                         category=excluded.category, role_hint=excluded.role_hint,
                         counterparty=excluded.counterparty, rule_id=excluded.rule_id,
                         updated_at=excluded.updated_at""",
                    (row["id"], m.category, m.role_hint, m.counterparty, m.id, now),
                )
                categorized += 1
            elif recompute_all:
                conn.execute("DELETE FROM txn_interp WHERE raw_txn_id = ?", (row["id"],))
    return categorized
