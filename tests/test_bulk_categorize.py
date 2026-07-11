"""Tests for POST /api/transactions/bulk-categorize: TestClient over a real temp
sqlite DB. No mocks. Mirrors the fixture/seed pattern in test_web_api.py."""

from __future__ import annotations

from fastapi.testclient import TestClient

from bankapp import config as configmod
from bankapp import db as dbmod
from bankapp.classify import review
from bankapp.web.app import create_app
from tests.conftest import insert_account, insert_raw_txn


def _client(app_env):
    cfg = configmod.load_config()
    return TestClient(create_app(cfg))


def _seed_uncategorized(app_env, n=3):
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    aid = insert_account(conn, key="td-chequing", currency="CAD")
    ids = []
    for i in range(n):
        ids.append(insert_raw_txn(
            conn, aid, posted_date=f"2026-04-{i + 1:02d}", amount_minor=-500 - i,
            currency="CAD", description_raw=f"VENDOR {i}", description_norm=f"vendor {i}",
            dedup_key=f"sha256:bc{i}",
        ))
    conn.commit()
    conn.close()
    return ids


def test_bulk_categorize_applies_category_to_all_selected(app_env):
    ids = _seed_uncategorized(app_env, n=3)
    selected = ids[:2]  # leave the third uncategorized
    conn = dbmod.connect(app_env["db"])
    before = review.count(conn)
    conn.close()
    assert before == 3

    client = _client(app_env)
    r = client.post("/api/transactions/bulk-categorize", json={"ids": selected, "category": "misc"})
    assert r.status_code == 200
    assert r.json() == {"categorized": 2}

    conn = dbmod.connect(app_env["db"])
    after = review.count(conn)
    rows = conn.execute(
        "SELECT raw_txn_id, category, source FROM txn_interp WHERE raw_txn_id IN (?, ?)",
        tuple(selected),
    ).fetchall()
    conn.close()
    assert after == before - 2  # review queue dropped by exactly the selected count
    assert len(rows) == 2
    for row in rows:
        assert row["category"] == "misc"
        assert row["source"] == "manual"

    # untouched third txn is still in the queue
    got = client.get("/api/transactions?category=misc").json()
    assert got["total"] == 2


def test_bulk_categorize_empty_ids_400(app_env):
    _seed_uncategorized(app_env, n=1)
    client = _client(app_env)
    r = client.post("/api/transactions/bulk-categorize", json={"ids": [], "category": "misc"})
    assert r.status_code == 400


def test_bulk_categorize_empty_category_400(app_env):
    ids = _seed_uncategorized(app_env, n=1)
    client = _client(app_env)
    r = client.post("/api/transactions/bulk-categorize", json={"ids": ids, "category": "   "})
    assert r.status_code == 400


def test_bulk_categorize_missing_id_400(app_env):
    ids = _seed_uncategorized(app_env, n=1)
    client = _client(app_env)
    r = client.post(
        "/api/transactions/bulk-categorize",
        json={"ids": ids + [999999], "category": "misc"},
    )
    assert r.status_code == 400
    assert "999999" in r.json()["detail"]

    # nothing was written — the whole batch is rejected together
    conn = dbmod.connect(app_env["db"])
    remaining = review.count(conn)
    conn.close()
    assert remaining == 1


def test_bulk_categorize_single_request_for_n_rows(app_env):
    """A15: one POST carrying N ids categorizes all N — no per-row request needed."""
    ids = _seed_uncategorized(app_env, n=5)
    client = _client(app_env)
    r = client.post("/api/transactions/bulk-categorize", json={"ids": ids, "category": "misc"})
    assert r.status_code == 200
    assert r.json()["categorized"] == len(ids)
