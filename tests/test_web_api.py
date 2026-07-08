"""API tests: TestClient over a real temp sqlite DB. No mocks."""

from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from bankapp import config as configmod
from bankapp import db as dbmod
from bankapp.report import advisor
from bankapp.web.app import create_app
from tests.conftest import insert_account, insert_raw_txn


def _client(app_env):
    cfg = configmod.load_config()
    return TestClient(create_app(cfg))


def test_meta_and_status_seeded(app_env):
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    aid = insert_account(conn, key="td-chequing", currency="CAD")
    insert_raw_txn(conn, aid, posted_date="2026-01-15", amount_minor=-1234, currency="CAD")
    conn.execute(
        """INSERT INTO balance_snapshot(account_id, as_of, balance_minor, currency, source, captured_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (aid, "2026-01-15", 500000, "CAD", "manual", "2026-01-15T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    client = _client(app_env)

    r = client.get("/api/meta")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["currencies"], dict)
    assert all(isinstance(v, int) for v in body["currencies"].values())
    assert "CAD" in body["currencies"]
    assert body["accounts"]
    assert body["months"]

    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    for key in ("uncategorized", "pending_transfers", "receivables", "last_import"):
        assert key in body


def test_meta_and_status_empty_db(app_env):
    dbmod.init_db(app_env["db"])
    cfg = configmod.load_config()
    client = TestClient(create_app(cfg))

    r = client.get("/api/meta")
    assert r.status_code == 200
    body = r.json()
    assert "CAD" in body["currencies"]
    assert body["accounts"] == []
    assert body["categories"] == []
    assert body["months"] == []

    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["uncategorized"] == 0
    assert body["pending_transfers"] == []
    assert body["receivables"] == []
    assert body["last_import"] is None


def test_root_serves_html(app_env):
    dbmod.init_db(app_env["db"])
    client = _client(app_env)
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def _seed_rich(conn):
    """Seed 2 accounts, txns across 2 months w/ categories, balance snapshots for
    net-worth history, and a settled split-expense transfer group (netted row)."""
    aid = insert_account(conn, key="td-chequing", currency="CAD")
    aid2 = insert_account(conn, key="ws-cash", institution="wealthsimple", type="cash", currency="CAD")

    # Jan: groceries + income
    t1 = insert_raw_txn(
        conn, aid, posted_date="2026-01-05", amount_minor=-6000, currency="CAD",
        description_raw="SAFEWAY", description_norm="safeway", dedup_key="sha256:t1",
    )
    t2 = insert_raw_txn(
        conn, aid, posted_date="2026-01-20", amount_minor=250000, currency="CAD",
        description_raw="PAYROLL", description_norm="payroll", dedup_key="sha256:t2",
    )
    # Feb: dining
    t3 = insert_raw_txn(
        conn, aid, posted_date="2026-02-10", amount_minor=-4500, currency="CAD",
        description_raw="RESTAURANT", description_norm="restaurant", dedup_key="sha256:t3",
    )
    now = "2026-02-01T00:00:00Z"
    conn.execute(
        "INSERT INTO txn_interp(raw_txn_id, category, role_hint, updated_at) VALUES (?,?,?,?)",
        (t1, "groceries", None, now),
    )
    conn.execute(
        "INSERT INTO txn_interp(raw_txn_id, category, role_hint, updated_at) VALUES (?,?,?,?)",
        (t3, "dining", None, now),
    )

    for as_of, bal in (("2026-01-31", 500000), ("2026-02-28", 545500)):
        conn.execute(
            """INSERT INTO balance_snapshot(account_id, as_of, balance_minor, currency, source, captured_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (aid, as_of, bal, "CAD", "manual", now),
        )

    # A settled split-expense group: rent expense on ws-cash, reimbursement on td-chequing.
    tmpl_cur = conn.execute(
        """INSERT INTO recurring_templates(
               name, kind, expected_amount_minor, currency, cadence,
               share_numer, share_denom, expense_account, expense_pattern,
               reimburse_account, reimburser_pattern, amount_tolerance_minor,
               window_days, link_transfer, day_of_month, active
           ) VALUES ('rent','split_expense',240000,'CAD','monthly',1,2,'ws-cash','landlord',
               'td-chequing','etransfer from roommate',500,45,1,1,1)"""
    )
    template_id = tmpl_cur.lastrowid
    grp_cur = conn.execute(
        """INSERT INTO groups(type, status, template_id, period_key, created_at, updated_at)
           VALUES ('split_expense','settled', ?, '2026-01', ?, ?)""",
        (template_id, now, now),
    )
    group_id = grp_cur.lastrowid
    t_expense = insert_raw_txn(
        conn, aid2, posted_date="2026-01-01", amount_minor=-240000, currency="CAD",
        description_raw="LANDLORD RENT", description_norm="landlord rent", dedup_key="sha256:rent1",
    )
    t_reimb = insert_raw_txn(
        conn, aid, posted_date="2026-01-03", amount_minor=120000, currency="CAD",
        description_raw="ETRANSFER FROM ROOMMATE", description_norm="etransfer from roommate",
        dedup_key="sha256:rent2",
    )
    conn.execute(
        "INSERT INTO group_members(group_id, raw_txn_id, role, share_amount_minor) VALUES (?,?,?,?)",
        (group_id, t_expense, "expense", 120000),
    )
    conn.execute(
        "INSERT INTO group_members(group_id, raw_txn_id, role, share_amount_minor) VALUES (?,?,?,?)",
        (group_id, t_reimb, "reimbursement", None),
    )
    conn.commit()
    return {"account_id": aid, "account2_id": aid2, "txn_ids": [t1, t2, t3, t_expense, t_reimb]}


def test_digest_endpoint_matches_advisor_digest(app_env):
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    _seed_rich(conn)
    conn.close()

    cfg = configmod.load_config()
    conn2 = dbmod.connect(app_env["db"])
    expected = advisor.digest(conn2, cfg, today=None)
    conn2.close()

    client = TestClient(create_app(cfg))
    r = client.get("/api/digest")
    assert r.status_code == 200
    # Compare ignoring as_of/month drift only if today() ticks between calls (extremely
    # unlikely in-test); otherwise this is an exact equivalence check.
    assert r.json() == expected


def test_networth_endpoints_seeded(app_env):
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    _seed_rich(conn)
    conn.close()
    client = _client(app_env)

    r = client.get("/api/networth")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert body
    assert body[0]["currency"] == "CAD"
    assert isinstance(body[0]["net_worth_minor"], int)

    r = client.get("/api/networth/history")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) >= 2
    assert all(isinstance(row["net_worth_minor"], int) for row in body)


def test_cashflow_endpoint(app_env):
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    _seed_rich(conn)
    conn.close()
    client = _client(app_env)

    r = client.get("/api/cashflow")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert body
    assert isinstance(body[0]["income_minor"], int)

    r = client.get("/api/cashflow?months=1")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_budgets_endpoint(app_env):
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    _seed_rich(conn)
    conn.close()
    client = _client(app_env)

    r = client.get("/api/budgets?month=2026-01")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    if body:
        assert isinstance(body[0]["actual_minor"], int)
        assert body[0]["limit_minor"] is None or isinstance(body[0]["limit_minor"], int)

    r = client.get("/api/budgets")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_spend_endpoint(app_env):
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    _seed_rich(conn)
    conn.close()
    client = _client(app_env)

    r = client.get("/api/spend?month=2026-01&by=category")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert any(row["category"] == "groceries" for row in body)

    r = client.get("/api/spend?month=2026-01&by=total")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)

    r = client.get("/api/spend?month=2026-01")
    assert r.status_code == 200


def test_subscriptions_and_leaks_endpoints(app_env):
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    _seed_rich(conn)
    conn.close()
    client = _client(app_env)

    r = client.get("/api/subscriptions")
    assert r.status_code == 200
    assert isinstance(r.json(), list)

    r = client.get("/api/leaks")
    assert r.status_code == 200
    assert isinstance(r.json(), list)

    r = client.get("/api/leaks?threshold_minor=100")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_goals_endpoint(app_env):
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    _seed_rich(conn)
    conn.close()
    client = _client(app_env)

    r = client.get("/api/goals")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    if body:
        assert isinstance(body[0]["target_minor"], int)


def test_advice_endpoints(app_env):
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    _seed_rich(conn)
    conn.commit()

    r_latest = None
    client = _client(app_env)
    r = client.get("/api/advice/latest")
    assert r.status_code == 200
    assert r.json() is None

    r = client.get("/api/advice?limit=5")
    assert r.status_code == 200
    assert r.json() == []

    from bankapp.report import briefs
    briefs.add_brief(conn, "# Brief\nSome content", "2026-02-01", source="claude")
    conn.close()

    client = _client(app_env)
    r = client.get("/api/advice/latest")
    assert r.status_code == 200
    body = r.json()
    assert body is not None
    assert body["content_md"] == "# Brief\nSome content"

    r = client.get("/api/advice?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1


def test_all_slice5_and_6_endpoints_empty_db(app_env):
    dbmod.init_db(app_env["db"])
    client = _client(app_env)

    r = client.get("/api/digest")
    assert r.status_code == 200
    assert r.json()["net_worth"] == []

    r = client.get("/api/networth")
    assert r.status_code == 200
    assert r.json() == []

    r = client.get("/api/networth/history")
    assert r.status_code == 200
    assert r.json() == []

    r = client.get("/api/cashflow")
    assert r.status_code == 200
    assert r.json() == []

    r = client.get("/api/budgets")
    assert r.status_code == 200
    assert r.json() == []

    r = client.get("/api/spend")
    assert r.status_code == 200
    assert r.json() == []

    r = client.get("/api/subscriptions")
    assert r.status_code == 200
    assert r.json() == []

    r = client.get("/api/leaks")
    assert r.status_code == 200
    assert r.json() == []

    r = client.get("/api/goals")
    assert r.status_code == 200
    assert r.json() == []

    r = client.get("/api/advice/latest")
    assert r.status_code == 200
    assert r.json() is None

    r = client.get("/api/advice")
    assert r.status_code == 200
    assert r.json() == []

    r = client.get("/api/transactions")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["subtotals"] == []

    r = client.get("/api/receivables")
    assert r.status_code == 200
    assert r.json() == []


def test_transactions_endpoint_filters_and_pagination(app_env):
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    seed = _seed_rich(conn)
    conn.close()
    client = _client(app_env)

    r = client.get("/api/transactions")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == len(seed["txn_ids"])
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert len(body["items"]) == body["total"]
    item = body["items"][0]
    for key in ("id", "account_key", "posted_date", "currency", "amount_minor",
                "effective_minor", "description_norm", "category", "group_role", "group_type"):
        assert key in item
    assert isinstance(item["amount_minor"], int)
    assert isinstance(item["effective_minor"], int)

    # month filter
    r = client.get("/api/transactions?month=2026-01")
    body = r.json()
    assert body["total"] == 4  # t1, t2, t_expense, t_reimb all in Jan

    # account filter
    r = client.get("/api/transactions?account=ws-cash")
    body = r.json()
    assert body["total"] == 1

    # category filter
    r = client.get("/api/transactions?category=groceries")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["description_norm"] == "safeway"

    # q filter (case-insensitive)
    r = client.get("/api/transactions?q=SAFEWAY")
    body = r.json()
    assert body["total"] == 1

    # pagination
    r = client.get("/api/transactions?page_size=1")
    body = r.json()
    assert len(body["items"]) == 1
    assert body["total"] == len(seed["txn_ids"])
    assert body["page_size"] == 1

    # subtotals: per-currency income/spend over full set
    r = client.get("/api/transactions")
    body = r.json()
    assert body["subtotals"]
    cad = next(s for s in body["subtotals"] if s["currency"] == "CAD")
    assert isinstance(cad["income_minor"], int)
    assert isinstance(cad["spend_minor"], int)

    # netted row: the reimbursement leg should have effective_minor == 0 and group_role set
    reimb_row = next(i for i in body["items"] if i["description_norm"] == "etransfer from roommate")
    assert reimb_row["effective_minor"] == 0
    assert reimb_row["group_role"] == "reimbursement"


def test_receivables_endpoint_includes_settled(app_env):
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    _seed_rich(conn)
    conn.close()
    client = _client(app_env)

    r = client.get("/api/receivables")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["status"] == "settled"
    assert body[0]["template"] == "rent"


# ---- Part-B fix regressions ------------------------------------------------

def test_filter_options_lists_uncategorized_when_present(app_env):
    """(uncategorized) must appear in the category filter when such rows exist,
    since the transactions filter accepts it (Part-B finding #2)."""
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    aid = insert_account(conn, key="td-chequing", currency="CAD")
    # a raw_txn with NO txn_interp row -> effective category is (uncategorized)
    insert_raw_txn(conn, aid, posted_date="2026-03-02", amount_minor=-999, currency="CAD",
                   dedup_key="sha256:uncat")
    conn.commit()
    conn.close()
    client = _client(app_env)
    cats = client.get("/api/meta").json()["categories"]
    assert "(uncategorized)" in cats


def test_filter_options_includes_account_currency_without_txns(app_env):
    """A funded-later account's currency must be in the exponent map even with no
    transactions/snapshots yet (Part-B finding #3)."""
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    insert_account(conn, key="btc-wallet", institution="wealthsimple", type="crypto", currency="BTC")
    conn.commit()
    conn.close()
    client = _client(app_env)
    currencies = client.get("/api/meta").json()["currencies"]
    assert "BTC" in currencies
    assert currencies["BTC"] == 8  # money.exponent_for('BTC')


# ---- write routes: categorization -----------------------------------------

def _seed_uncategorized(app_env):
    dbmod.init_db(app_env["db"])
    conn = dbmod.connect(app_env["db"])
    aid = insert_account(conn, key="td-chequing", currency="CAD")
    ids = [
        insert_raw_txn(conn, aid, posted_date="2026-04-01", amount_minor=-500, currency="CAD",
                       description_raw="STARBUCKS #1", description_norm="starbucks #1", dedup_key="sha256:s1"),
        insert_raw_txn(conn, aid, posted_date="2026-04-02", amount_minor=-600, currency="CAD",
                       description_raw="STARBUCKS #2", description_norm="starbucks #2", dedup_key="sha256:s2"),
        insert_raw_txn(conn, aid, posted_date="2026-04-03", amount_minor=-700, currency="CAD",
                       description_raw="ONE OFF VENDOR", description_norm="one off vendor", dedup_key="sha256:o1"),
    ]
    conn.commit()
    conn.close()
    return ids


def test_post_rule_categorizes_matching_txns(app_env):
    ids = _seed_uncategorized(app_env)
    client = _client(app_env)

    r = client.post("/api/rules", json={"pattern": "starbucks", "category": "coffee"})
    assert r.status_code == 200
    body = r.json()
    assert body["added"] is True
    assert body["categorized"] == 2  # both starbucks rows, not the one-off vendor

    # the rule now shows as source='manual' (added from the UI)
    conn = dbmod.connect(app_env["db"])
    src = conn.execute("SELECT source FROM rules WHERE pattern = 'starbucks'").fetchone()[0]
    conn.close()
    assert src == "manual"

    # the matched rows are no longer under (uncategorized)
    got = client.get("/api/transactions?category=coffee").json()
    assert got["total"] == 2

    # duplicate pattern -> added=false, still idempotently categorizes
    r2 = client.post("/api/rules", json={"pattern": "starbucks", "category": "coffee"})
    assert r2.status_code == 200
    assert r2.json()["added"] is False


def test_post_rule_invalid_regex_400(app_env):
    _seed_uncategorized(app_env)
    client = _client(app_env)
    r = client.post("/api/rules", json={"kind": "regex", "pattern": "([bad", "category": "x"})
    assert r.status_code == 400


def test_post_one_off_categorizes_single_txn(app_env):
    ids = _seed_uncategorized(app_env)
    one_off_id = ids[2]
    client = _client(app_env)

    before = client.get("/api/status").json()["uncategorized"]
    assert before == 3

    r = client.post(f"/api/transactions/{one_off_id}/categorize", json={"category": "misc"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    after = client.get("/api/status").json()["uncategorized"]
    assert after == 2  # only the one-off left the queue

    # the two starbucks rows are untouched (no rule was created)
    got = client.get("/api/transactions?category=misc").json()
    assert got["total"] == 1
    assert got["items"][0]["description_norm"] == "one off vendor"


def test_post_one_off_unknown_id_404(app_env):
    _seed_uncategorized(app_env)
    client = _client(app_env)
    r = client.post("/api/transactions/999999/categorize", json={"category": "misc"})
    assert r.status_code == 404


def test_serve_busy_port_prints_friendly_message_and_exits_1(app_env, capsys):
    """A busy port must yield a friendly ASCII message + exit code 1, not a raw
    uvicorn traceback / exit 3 (Part-B finding #1)."""
    import socket
    import pytest
    import typer
    from bankapp.web import app as webapp

    dbmod.init_db(app_env["db"])
    cfg = configmod.load_config()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        with pytest.raises(typer.Exit) as exc:
            webapp.serve(cfg, port=port, open_browser=False)
        assert exc.value.exit_code == 1
        assert "Could not bind port" in capsys.readouterr().out
    finally:
        holder.close()
