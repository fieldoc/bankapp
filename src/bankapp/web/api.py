"""Read-only JSON API routes. No Pydantic models this slice -- plain dicts."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date
from importlib import metadata
from typing import Optional

from fastapi import APIRouter, Depends, Request

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
