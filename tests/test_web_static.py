"""Static-file serving smoke tests + offline (no external origin) guard."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from bankapp import config as configmod
from bankapp import db as dbmod
from bankapp.web.app import create_app

PAGES = [
    "/",
    "/transactions.html",
    "/subscriptions.html",
    "/goals.html",
    "/receivables.html",
    "/advice.html",
]

# Any http(s):// origin that is NOT 127.0.0.1 / localhost would mean the page phones home.
_EXTERNAL = re.compile(r"https?://(?!127\.0\.0\.1|localhost)", re.IGNORECASE)


def _client(app_env):
    dbmod.init_db(app_env["db"])
    return TestClient(create_app(configmod.load_config()))


def test_root_html(app_env):
    client = _client(app_env)
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "BankApp" in r.text
    # shell wiring
    assert "/app.js" in r.text
    assert "/app.css" in r.text


def test_all_pages_serve(app_env):
    client = _client(app_env)
    for path in PAGES:
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert "text/html" in r.headers["content-type"], path


def test_shared_assets_serve(app_env):
    client = _client(app_env)
    for path in ["/app.js", "/app.css"]:
        r = client.get(path)
        assert r.status_code == 200, path


def test_chartjs_vendored(app_env):
    client = _client(app_env)
    r = client.get("/vendor/chart.umd.js")
    assert r.status_code == 200
    # Guards both packaging (asset ships) and that the vendor step actually ran.
    assert len(r.content) > 100_000, f"chart.umd.js too small: {len(r.content)} bytes"


def test_no_external_origins(app_env):
    """Offline guarantee: served HTML + app.js reference no non-local origin."""
    client = _client(app_env)
    for path in PAGES + ["/app.js"]:
        r = client.get(path)
        assert r.status_code == 200, path
        found = _EXTERNAL.findall(r.text)
        assert not found, f"{path} references external origin(s): {found}"
