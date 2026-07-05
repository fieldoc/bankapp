"""Rules-first categorization engine + rule persistence.

A persisted rule IS the learn-once cache (manual or Claude verdict). Matching is
deterministic: rules are tried in (priority ASC, id ASC) order, first match wins.
Patterns are stored lowercase and matched against description_norm (also lowercase).
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
    if not pattern:
        raise InvalidPatternError("pattern must be non-empty")
    if match_kind == "regex":
        try:
            re.compile(pattern)
        except re.error as exc:
            raise InvalidPatternError(f"invalid regex {pattern!r}: {exc}") from exc


class RuleEngine:
    """Compiled, ordered rule set. `match` returns the first matching rule or None."""

    def __init__(self, rules: list[Rule]):
        self._rules = sorted(rules, key=lambda r: (r.priority, r.id))
        self._compiled = {
            r.id: re.compile(r.pattern) for r in self._rules if r.match_kind == "regex"
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

    Validates the pattern (invalid regex rejected here) and stores it lowercase.
    """
    validate_pattern(match_kind, pattern)
    pattern = pattern.lower()
    before = conn.total_changes
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO rules
                 (match_kind, pattern, category, role_hint, counterparty, priority, source, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (match_kind, pattern, category, role_hint, counterparty, priority, source, _utc_now_iso()),
        )
    return conn.total_changes > before


def upsert_seed_rules(conn: sqlite3.Connection, seed_patterns) -> int:
    """Upsert transfer seed rules from config [transfers].seed_patterns."""
    added = 0
    for p in seed_patterns:
        if add_rule(conn, "substring", p, role_hint="transfer", priority=SEED_PRIORITY, source="seed"):
            added += 1
    return added


def categorize(conn: sqlite3.Connection, recompute_all: bool = False) -> int:
    """Apply rules to raw_txn, writing verdicts to txn_interp. Idempotent.

    Default: only txns with no txn_interp row yet. `recompute_all`: re-evaluate every
    txn against the current rules (safe — interpretation only; raw_txn is never touched)
    and drop rows that no longer match. Returns the number of txns categorized.
    """
    engine = RuleEngine(load_rules(conn))
    if recompute_all:
        rows = conn.execute("SELECT id, description_norm FROM raw_txn").fetchall()
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
