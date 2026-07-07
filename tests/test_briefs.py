"""Tests for the append-only advisor_brief store (src/bankapp/report/briefs.py)."""

import sqlite3

import pytest

from bankapp import db as dbmod
from bankapp.report import briefs


def test_add_brief_then_latest_round_trips(conn):
    brief_id = briefs.add_brief(conn, "Coaching text here.", "2026-07-06", source="claude")
    assert isinstance(brief_id, int)

    latest = briefs.latest(conn)
    assert latest is not None
    assert latest["content_md"] == "Coaching text here."
    assert latest["digest_as_of"] == "2026-07-06"
    assert latest["source"] == "claude"
    assert latest["id"] == brief_id


def test_latest_returns_none_when_empty(conn):
    assert briefs.latest(conn) is None


def test_list_briefs_newest_first(conn):
    briefs.add_brief(conn, "first", "2026-07-01")
    briefs.add_brief(conn, "second", "2026-07-02")
    briefs.add_brief(conn, "third", "2026-07-03")

    rows = briefs.list_briefs(conn)
    assert [r["content_md"] for r in rows] == ["third", "second", "first"]


def test_list_briefs_respects_limit(conn):
    for i in range(5):
        briefs.add_brief(conn, f"brief-{i}", "2026-07-01")

    rows = briefs.list_briefs(conn, limit=2)
    assert len(rows) == 2
    assert rows[0]["content_md"] == "brief-4"
    assert rows[1]["content_md"] == "brief-3"


def test_list_briefs_empty_returns_empty_list(conn):
    assert briefs.list_briefs(conn) == []


def test_add_brief_empty_content_raises(conn):
    with pytest.raises(ValueError):
        briefs.add_brief(conn, "   ", "2026-07-06")


def test_add_brief_bad_as_of_raises(conn):
    with pytest.raises(ValueError):
        briefs.add_brief(conn, "content", "07-06-2026")


def test_add_brief_bad_source_raises(conn):
    with pytest.raises(ValueError):
        briefs.add_brief(conn, "content", "2026-07-06", source="bogus")


def test_advisor_brief_update_aborts(conn):
    brief_id = briefs.add_brief(conn, "content", "2026-07-06")
    with pytest.raises(sqlite3.IntegrityError, match="advisor_brief is append-only"):
        conn.execute("UPDATE advisor_brief SET content_md = 'x' WHERE id = ?", (brief_id,))


def test_advisor_brief_delete_aborts(conn):
    brief_id = briefs.add_brief(conn, "content", "2026-07-06")
    with pytest.raises(sqlite3.IntegrityError, match="advisor_brief is append-only"):
        conn.execute("DELETE FROM advisor_brief WHERE id = ?", (brief_id,))


def test_schema_reapply_keeps_existing_brief(conn):
    brief_id = briefs.add_brief(conn, "content", "2026-07-06")
    dbmod.apply_schema(conn)
    latest = briefs.latest(conn)
    assert latest is not None
    assert latest["id"] == brief_id
    assert latest["content_md"] == "content"
