"""JSON API routes. Reads return plain dicts; the two write routes at the bottom
(categorization) validate their bodies with Pydantic."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date
from importlib import metadata
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bankapp.classify import engine as classify
from bankapp.report import advisor, analytics, briefs
from bankapp.web.deps import get_conn
from bankapp.web.queries import filter_options, receivables_all, transactions_page

router = APIRouter()


def _app_version() -> str:
    try:
        return metadata.version("bankapp")
    except metadata.PackageNotFoundError:
        return "0.0.0"


@router.get("/api/meta")
def get_meta(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return {"app_version": _app_version(), **filter_options(conn)}


@router.get("/api/status")
def get_status(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    st = analytics.status(conn, request.app.state.cfg.transfers.window_days)
    return dataclasses.asdict(st)


@router.get("/api/digest")
def get_digest(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return advisor.digest(conn, request.app.state.cfg)


@router.get("/api/networth")
def get_networth(conn: sqlite3.Connection = Depends(get_conn)) -> list:
    return [dataclasses.asdict(r) for r in advisor.net_worth(conn)]


@router.get("/api/networth/history")
def get_networth_history(conn: sqlite3.Connection = Depends(get_conn)) -> list:
    return advisor.net_worth_history(conn)


@router.get("/api/cashflow")
def get_cashflow(months: Optional[int] = None, conn: sqlite3.Connection = Depends(get_conn)) -> list:
    return [dataclasses.asdict(r) for r in advisor.monthly_cashflow(conn, months)]


@router.get("/api/budgets")
def get_budgets(month: Optional[str] = None, conn: sqlite3.Connection = Depends(get_conn)) -> list:
    month = month or date.today().strftime("%Y-%m")
    return [dataclasses.asdict(r) for r in advisor.budget_status(conn, month)]


@router.get("/api/spend")
def get_spend(
    month: Optional[str] = None,
    by: str = "category",
    conn: sqlite3.Connection = Depends(get_conn),
) -> list:
    month = month or date.today().strftime("%Y-%m")
    if by == "category":
        rows = analytics.spend_by_category(conn, month)
    else:
        rows = analytics.spend_total(conn, month)
    return [dataclasses.asdict(r) for r in rows]


@router.get("/api/subscriptions")
def get_subscriptions(conn: sqlite3.Connection = Depends(get_conn)) -> list:
    return [dataclasses.asdict(r) for r in advisor.subscriptions_from_db(conn)]


@router.get("/api/leaks")
def get_leaks(
    request: Request,
    threshold_minor: Optional[int] = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list:
    threshold = threshold_minor if threshold_minor is not None else request.app.state.cfg.leak_threshold_minor
    return [dataclasses.asdict(r) for r in advisor.leaks_from_db(conn, threshold)]


@router.get("/api/goals")
def get_goals(conn: sqlite3.Connection = Depends(get_conn)) -> list:
    return [dataclasses.asdict(r) for r in advisor.goals_status(conn)]


@router.get("/api/advice/latest")
def get_advice_latest(conn: sqlite3.Connection = Depends(get_conn)):
    return briefs.latest(conn)


@router.get("/api/advice")
def get_advice(limit: int = 20, conn: sqlite3.Connection = Depends(get_conn)) -> list:
    return briefs.list_briefs(conn, limit)


@router.get("/api/transactions")
def get_transactions(
    month: Optional[str] = None,
    account: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    return transactions_page(
        conn, month=month, account=account, category=category, q=q, page=page, page_size=page_size
    )


@router.get("/api/receivables")
def get_receivables(conn: sqlite3.Connection = Depends(get_conn)) -> list:
    return receivables_all(conn)


# ---- write routes (categorization) -----------------------------------------
# The only mutating endpoints. Both go through the classify engine so the DB
# invariants (rules-first, manual-override semantics) live in one place.


class RuleIn(BaseModel):
    pattern: str
    kind: str = "substring"
    category: Optional[str] = None
    role: Optional[str] = None
    counterparty: Optional[str] = None
    priority: int = 100


class OneOffIn(BaseModel):
    category: str
    role: Optional[str] = None
    counterparty: Optional[str] = None


@router.post("/api/rules")
def post_rule(body: RuleIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Add a categorization rule (generalizable) and apply it. Rules added from the
    UI are tagged source='manual'. Returns whether it was newly added and how many
    transactions the rule set now categorized."""
    try:
        added = classify.add_rule(
            conn, body.kind, body.pattern, category=body.category, role_hint=body.role,
            counterparty=body.counterparty, priority=body.priority, source="manual",
        )
    except classify.InvalidPatternError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    categorized = classify.categorize(conn)
    return {"added": added, "categorized": categorized}


@router.post("/api/transactions/{raw_txn_id}/categorize")
def post_one_off(
    raw_txn_id: int, body: OneOffIn, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    """Set a one-off manual category for a single transaction (no rule created)."""
    exists = conn.execute("SELECT 1 FROM raw_txn WHERE id = ?", (raw_txn_id,)).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail=f"no transaction with id {raw_txn_id}")
    classify.set_manual_category(
        conn, raw_txn_id, body.category, role_hint=body.role, counterparty=body.counterparty
    )
    return {"ok": True}
