from bankapp.classify import engine, review
from tests.conftest import insert_account, insert_raw_txn


def _txn(conn, acct, desc_norm, dedup_key, amount=-1000):
    return insert_raw_txn(
        conn, acct, description_raw=desc_norm, description_norm=desc_norm,
        dedup_key=dedup_key, amount_minor=amount,
    )


def test_categorize_fills_interp_and_is_idempotent(conn):
    acct = insert_account(conn)
    _txn(conn, acct, "netflix.com", "sha256:1")
    engine.add_rule(conn, "substring", "netflix", "subscriptions")

    assert engine.categorize(conn) == 1
    # second run: nothing new to do
    assert engine.categorize(conn) == 0
    cat = conn.execute("SELECT category FROM txn_interp").fetchone()[0]
    assert cat == "subscriptions"


def test_recompute_all_applies_new_rule(conn):
    acct = insert_account(conn)
    _txn(conn, acct, "loblaws groceries", "sha256:1")
    engine.categorize(conn)  # no rule yet -> uncategorized
    assert review.count(conn) == 1

    engine.add_rule(conn, "substring", "loblaws", "groceries")
    assert engine.categorize(conn, recompute_all=True) == 1
    assert review.count(conn) == 0


def test_recompute_all_drops_stale_interp(conn):
    acct = insert_account(conn)
    tid = _txn(conn, acct, "mystery merchant", "sha256:1")
    # A stale interp with no backing rule (e.g. left over after a rule was retired).
    conn.execute(
        "INSERT INTO txn_interp(raw_txn_id, category, updated_at) VALUES (?, 'shopping', 't')",
        (tid,),
    )
    conn.commit()
    assert review.count(conn) == 0
    # No current rule matches -> recompute_all drops the stale interp, txn re-queues.
    engine.categorize(conn, recompute_all=True)
    assert review.count(conn) == 1


def test_transfer_rule_removes_from_queue_without_category(conn):
    acct = insert_account(conn)
    _txn(conn, acct, "tfr-to 123", "sha256:1")
    engine.upsert_seed_rules(conn, ["tfr-to"])
    engine.categorize(conn)
    row = conn.execute("SELECT category, role_hint FROM txn_interp").fetchone()
    assert row["category"] is None
    assert row["role_hint"] == "transfer"
    assert review.count(conn) == 0  # handled as transfer, not in review queue


def test_raw_txn_untouched_by_categorize(conn):
    acct = insert_account(conn)
    _txn(conn, acct, "netflix", "sha256:1", amount=-1599)
    engine.add_rule(conn, "substring", "netflix", "subscriptions")
    engine.categorize(conn)
    assert conn.execute("SELECT amount_minor FROM raw_txn").fetchone()[0] == -1599


def test_review_export_json_and_markdown(conn):
    acct = insert_account(conn)
    _txn(conn, acct, "unknown thing", "sha256:1", amount=-2500)
    j = review.export_json(conn)
    assert "unknown thing" in j
    md = review.export_markdown(conn)
    assert "| id |" in md
    assert "unknown thing" in md
    assert "-25.00 CAD" in md
