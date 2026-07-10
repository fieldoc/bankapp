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


def test_sankey_plugin_vendored(app_env):
    client = _client(app_env)
    r = client.get("/vendor/chartjs-chart-sankey.min.js")
    assert r.status_code == 200
    # Guards both packaging (asset ships) and that the vendor step actually ran.
    assert len(r.content) > 10_000, f"sankey plugin too small: {len(r.content)} bytes"
    assert b"sankey" in r.content


def test_index_has_flow_sankey(app_env):
    """The Overview page must ship the cash-flow Sankey: plugin, hooks, and API path."""
    client = _client(app_env)
    html = client.get("/").text
    assert "chartjs-chart-sankey.min.js" in html   # plugin loaded
    assert "flow-month" in html                    # month picker hook
    assert "flow-chart" in html                    # canvas hook
    assert "/api/flows" in html                    # data source


def test_transactions_page_has_categorize_ui(app_env):
    """The Transactions page must ship the in-UI categorize entry point + modal wiring."""
    client = _client(app_env)
    html = client.get("/transactions.html").text
    assert "cat-btn" in html               # in-cell categorize button hook
    assert "openCategorizeModal" in html   # modal builder
    assert "/api/rules" in html            # rule (generalizable) path
    assert "/categorize" in html           # one-off path


def test_app_js_has_post_helper(app_env):
    client = _client(app_env)
    js = client.get("/app.js").text
    assert "App.post" in js


def test_no_external_origins(app_env):
    """Offline guarantee: served HTML + app.js reference no non-local origin."""
    client = _client(app_env)
    for path in PAGES + ["/app.js"]:
        r = client.get(path)
        assert r.status_code == 200, path
        found = _EXTERNAL.findall(r.text)
        assert not found, f"{path} references external origin(s): {found}"


def test_goals_page_has_crud_ui(app_env):
    """The Goals page must ship the add/edit/archive entry points + modal wiring."""
    client = _client(app_env)
    html = client.get("/goals.html").text
    assert "new-goal" in html           # add button hook
    assert "openGoalModal" in html      # modal builder
    assert "goal-edit" in html          # per-row edit hook
    assert "goal-archive" in html       # per-row archive hook
    assert "include_archived" in html   # archived disclosure
