"""Read-only query helpers backing the web API. Never writes to the DB."""

from __future__ import annotations

import sqlite3

from bankapp import money


def filter_options(conn: sqlite3.Connection) -> dict:
    """Filter-menu options for the dashboard: accounts, categories, months, currencies."""
    accounts = [
        {"key": r["key"], "institution": r["institution"], "type": r["type"], "currency": r["currency"]}
        for r in conn.execute(
            "SELECT key, institution, type, currency FROM accounts ORDER BY key"
        )
    ]
    categories = [
        r["category"]
        for r in conn.execute(
            "SELECT DISTINCT category FROM txn_interp WHERE category IS NOT NULL ORDER BY category"
        )
    ]
    # Surface the synthetic '(uncategorized)' bucket the transactions filter accepts,
    # but only when such rows actually exist (raw_txn with no/NULL interp category).
    has_uncat = conn.execute(
        "SELECT 1 FROM raw_txn r LEFT JOIN txn_interp i ON i.raw_txn_id = r.id "
        "WHERE i.category IS NULL LIMIT 1"
    ).fetchone()
    if has_uncat:
        categories.append("(uncategorized)")
    months = [
        r["m"]
        for r in conn.execute(
            "SELECT DISTINCT substr(posted_date,1,7) AS m FROM raw_txn ORDER BY m DESC"
        )
    ]
    # Union in configured account currencies too, so a funded-later account (e.g. a
    # fresh BTC account with no txns/snapshots yet) still gets its display exponent.
    currency_rows = conn.execute(
        """SELECT DISTINCT currency FROM raw_txn
           UNION
           SELECT DISTINCT currency FROM balance_snapshot
           UNION
           SELECT DISTINCT currency FROM accounts"""
    ).fetchall()
    currencies = {"CAD": money.exponent_for("CAD")}
    for r in currency_rows:
        currencies[r["currency"]] = money.exponent_for(r["currency"])

    return {
        "accounts": accounts,
        "categories": categories,
        "months": months,
        "currencies": currencies,
    }


def transactions_page(
    conn: sqlite3.Connection,
    *,
    month: str | None = None,
    account: str | None = None,
    category: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Paginated, filtered view over v_effective joined to accounts, plus per-currency
    subtotals computed over the same filtered set (not just the current page)."""
    page = max(1, page)
    page_size = max(1, min(page_size, 200))

    clauses = []
    params: dict = {}
    if month is not None:
        clauses.append("substr(e.posted_date,1,7) = :month")
        params["month"] = month
    if account is not None:
        clauses.append("a.key = :account")
        params["account"] = account
    if category is not None:
        clauses.append("COALESCE(e.category,'(uncategorized)') = :category")
        params["category"] = category
    if q is not None:
        clauses.append("e.description_norm LIKE '%' || :q || '%'")
        params["q"] = q.lower()

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    items_sql = f"""
        SELECT e.id, a.key AS account_key, e.posted_date, e.currency, e.amount_minor,
               e.effective_minor, e.description_norm,
               COALESCE(e.category,'(uncategorized)') AS category,
               e.group_role, e.group_type
        FROM v_effective e JOIN accounts a ON a.id = e.account_id
        {where_sql}
        ORDER BY e.posted_date DESC, e.id DESC
        LIMIT :limit OFFSET :offset
    """
    page_params = dict(params)
    page_params["limit"] = page_size
    page_params["offset"] = (page - 1) * page_size
    items = [dict(r) for r in conn.execute(items_sql, page_params)]

    count_sql = f"""
        SELECT COUNT(*) AS n
        FROM v_effective e JOIN accounts a ON a.id = e.account_id
        {where_sql}
    """
    total = conn.execute(count_sql, params).fetchone()["n"]

    subtotals_sql = f"""
        SELECT e.currency AS currency,
               SUM(CASE WHEN e.effective_minor > 0 THEN e.effective_minor ELSE 0 END) AS income_minor,
               SUM(CASE WHEN e.effective_minor < 0 THEN -e.effective_minor ELSE 0 END) AS spend_minor
        FROM v_effective e JOIN accounts a ON a.id = e.account_id
        {where_sql}
        GROUP BY e.currency
    """
    subtotals = [dict(r) for r in conn.execute(subtotals_sql, params)]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "subtotals": subtotals,
    }


def receivables_all(conn: sqlite3.Connection) -> list[dict]:
    """Full receivables history (including settled), newest period first."""
    rows = conn.execute(
        """SELECT group_id, template, period_key, status, expense_minor, expected_minor,
                  received_minor, outstanding_minor, age_days
           FROM v_receivables
           ORDER BY period_key DESC"""
    )
    return [dict(r) for r in rows]
