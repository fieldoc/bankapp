"""CSRF: SameOriginMiddleware blocks cross-origin writes but lets non-browser
clients (the categorize skill / CLI / this test client) and all reads through.

TestClient sends no Origin/Sec-Fetch-Site by default, which is exactly the shape of
a non-browser client — so we simulate a browser by setting those headers explicitly.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bankapp import config as configmod
from bankapp import db as dbmod
from bankapp.web.app import create_app


def _client(app_env):
    dbmod.init_db(app_env["db"])
    return TestClient(create_app(configmod.load_config()))


def _goal_body(**over):
    body = {
        "name": "trip",
        "target": "1000.00",
        "currency": "CAD",
        "start_date": "2026-01-01",
        "allocation_pct": 50,
    }
    body.update(over)
    return body


def test_cross_site_sec_fetch_blocked(app_env):
    client = _client(app_env)
    r = client.post(
        "/api/goals",
        json=_goal_body(),
        headers={"Sec-Fetch-Site": "cross-site", "Host": "127.0.0.1:8377"},
    )
    assert r.status_code == 403


def test_cross_origin_header_blocked(app_env):
    client = _client(app_env)
    r = client.post(
        "/api/goals",
        json=_goal_body(),
        headers={"Origin": "http://evil.example", "Host": "127.0.0.1:8377"},
    )
    assert r.status_code == 403


def test_rebound_host_blocked(app_env):
    """DNS-rebinding: same-origin to the browser, but Host carries the attacker domain."""
    client = _client(app_env)
    r = client.post(
        "/api/goals",
        json=_goal_body(),
        headers={"Sec-Fetch-Site": "same-origin", "Host": "attacker.example"},
    )
    assert r.status_code == 403


def test_non_browser_client_allowed(app_env):
    """No Origin / Sec-Fetch-Site (the skill, curl, tests) => allowed."""
    client = _client(app_env)
    r = client.post("/api/goals", json=_goal_body())
    assert r.status_code == 200


def test_same_origin_browser_allowed(app_env):
    client = _client(app_env)
    r = client.post(
        "/api/goals",
        json=_goal_body(),
        headers={"Sec-Fetch-Site": "same-origin", "Host": "127.0.0.1:8377"},
    )
    assert r.status_code == 200


def test_reads_never_blocked(app_env):
    client = _client(app_env)
    r = client.get(
        "/api/goals",
        headers={"Sec-Fetch-Site": "cross-site", "Host": "attacker.example"},
    )
    assert r.status_code == 200
