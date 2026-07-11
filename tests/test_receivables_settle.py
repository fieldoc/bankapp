"""Manual settlement of a receivable (e.g. a roommate pays their share back in
cash -- no bank transaction to match). Real in-memory SQLite; no mocks. Reuses the
split-expense seeding from tests/test_match_splits.py (RENT template, rent_db
fixture) so groups come from the real match_splits() pipeline, not hand-rolled rows.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from bankapp import config as configmod
from bankapp import db as dbmod
from bankapp import receivables
from bankapp.cli import app as cli_app
from bankapp.match import splits
from bankapp.web.app import create_app
from tests.test_match_splits import RENT, _expense, rent_db  # noqa: F401 (fixture reuse)

runner = CliRunner()


def _group_id(conn, period_key="2026-01"):
    return conn.execute(
        "SELECT id FROM groups WHERE period_key = ?", (period_key,)
    ).fetchone()[0]


def _outstanding(conn, group_id):
    return conn.execute(
        "SELECT outstanding_minor FROM v_receivables WHERE group_id = ?", (group_id,)
    ).fetchone()[0]


# ---- A12: settle_group drops outstanding to 0 and leaves the outstanding filter ----

def test_settle_group_full_zeroes_outstanding(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"])
    splits.match_splits(conn, today=date(2026, 1, 15))
    gid = _group_id(conn)
    assert _outstanding(conn, gid) == 120000  # 240000 total - my 120000 share, no reimbursement

    result = receivables.settle_group(conn, gid)

    assert result["settled_minor"] == 120000
    assert result["outstanding_minor"] == 0
    assert _outstanding(conn, gid) == 0

    # the digest/status "outstanding > 0" filter no longer returns this group
    row = conn.execute(
        "SELECT 1 FROM v_receivables WHERE group_id = ? AND outstanding_minor > 0", (gid,)
    ).fetchone()
    assert row is None


# ---- A13: settlement survives `match_splits` rebuild (new group id each run) ----

def test_settlement_survives_rebuild(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"])
    splits.match_splits(conn, today=date(2026, 1, 15))
    gid = _group_id(conn)
    receivables.settle_group(conn, gid)
    assert _outstanding(conn, gid) == 0

    # match_splits() ALWAYS deletes and rebuilds a template's split groups on every
    # run (see match/splits.py:match_splits docstring) -- there is no "did it
    # actually rebuild" flag to check here, so this rerun stands in for `finance
    # match all --rebuild`. The settlement is keyed on (template_id, period_key),
    # never on groups.id, so it must re-associate with whatever group now covers
    # this period and outstanding must still read 0.
    splits.match_splits(conn, today=date(2026, 1, 15))
    new_gid = _group_id(conn)
    assert _outstanding(conn, new_gid) == 0
    row = conn.execute(
        "SELECT 1 FROM v_receivables WHERE group_id = ? AND outstanding_minor > 0", (new_gid,)
    ).fetchone()
    assert row is None


# ---- partial settlement ----

def test_settle_group_partial_reduces_outstanding_by_exact_amount(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"])
    splits.match_splits(conn, today=date(2026, 1, 15))
    gid = _group_id(conn)

    result = receivables.settle_group(conn, gid, amount_minor=50000, note="cash")

    assert result["settled_minor"] == 50000
    assert result["outstanding_minor"] == 120000 - 50000
    assert _outstanding(conn, gid) == 120000 - 50000


# ---- ReceivableNotFound ----

def test_settle_group_bogus_id_raises(rent_db):
    conn, ids = rent_db
    with pytest.raises(receivables.ReceivableNotFound):
        receivables.settle_group(conn, 999999)


def test_settle_by_template_unknown_template_raises(rent_db):
    conn, ids = rent_db
    with pytest.raises(receivables.ReceivableNotFound):
        receivables.settle_by_template(conn, "nope", "2026-01")


def test_settle_by_template_unknown_period_raises(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"])
    splits.match_splits(conn, today=date(2026, 1, 15))
    with pytest.raises(receivables.ReceivableNotFound):
        receivables.settle_by_template(conn, "rent", "2026-12")


def test_settle_by_template_full(rent_db):
    conn, ids = rent_db
    _expense(conn, ids["ws-cash"])
    splits.match_splits(conn, today=date(2026, 1, 15))

    result = receivables.settle_by_template(conn, "rent", "2026-01")

    assert result["settled_minor"] == 120000
    assert result["outstanding_minor"] == 0


# ---- API ----

def test_api_settle_receivable(app_env):
    dbmod.init_db(app_env["db"])
    runner.invoke(cli_app, ["init"])  # seeds accounts + the 'rent' template from config
    conn = dbmod.connect(app_env["db"])
    ws_id = conn.execute("SELECT id FROM accounts WHERE key = 'ws-cash'").fetchone()[0]
    _expense(conn, ws_id)
    splits.match_splits(conn, today=date(2026, 1, 15))
    gid = _group_id(conn)
    conn.close()

    cfg = configmod.load_config()
    client = TestClient(create_app(cfg))

    r = client.post("/api/receivables/settle", json={"group_id": gid})
    assert r.status_code == 200
    body = r.json()
    assert body["settled_minor"] == 120000
    assert body["outstanding_minor"] == 0

    r = client.get("/api/receivables")
    assert r.status_code == 200
    row = next(x for x in r.json() if x["group_id"] == gid)
    assert row["outstanding_minor"] == 0
    assert row["settled_minor"] == 120000

    r404 = client.post("/api/receivables/settle", json={"group_id": 999999})
    assert r404.status_code == 404


# ---- CLI ----

def test_cli_receivables_settle(app_env):
    dbmod.init_db(app_env["db"])
    runner.invoke(cli_app, ["init"])
    conn = dbmod.connect(app_env["db"])
    ws_id = conn.execute("SELECT id FROM accounts WHERE key = 'ws-cash'").fetchone()[0]
    _expense(conn, ws_id)
    splits.match_splits(conn, today=date(2026, 1, 15))
    conn.close()

    r = runner.invoke(cli_app, ["receivables", "settle", "--template", "rent", "--period", "2026-01"])
    assert r.exit_code == 0, r.output
    assert "120000" not in r.output or "1,200.00" in r.output or "settled" in r.output.lower()

    conn = dbmod.connect(app_env["db"])
    gid = _group_id(conn)
    assert _outstanding(conn, gid) == 0
    conn.close()

    r2 = runner.invoke(
        cli_app, ["receivables", "settle", "--template", "nope", "--period", "2026-01"]
    )
    assert r2.exit_code == 1
