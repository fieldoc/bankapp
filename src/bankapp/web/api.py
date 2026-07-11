"""JSON API routes. Reads return plain dicts; the write routes at the bottom
(categorization, goals) validate their bodies with Pydantic."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date
from importlib import metadata
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bankapp import goals as goalsmod
from bankapp import money
from bankapp import receivables as receivablesmod
from bankapp.classify import engine as classify
from bankapp.report import advisor, analytics, anomalies, briefs, projection
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
    # `known_currencies` is the allowlist for goal creation; `currencies` (from
    # filter_options) is the data-derived exponent map. They are not the same thing.
    return {
        "app_version": _app_version(),
        "known_currencies": list(money.known_currencies()),
        **filter_options(conn),
    }


@router.get("/api/status")
def get_status(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    st = analytics.status(conn, request.app.state.cfg.transfers.window_days)
    return dataclasses.asdict(st)


@router.get("/api/digest")
def get_digest(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return advisor.digest(conn, request.app.state.cfg)


@router.get("/api/changes")
def get_changes(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return advisor.digest(conn, request.app.state.cfg)["changes_since_brief"]


@router.get("/api/networth")
def get_networth(conn: sqlite3.Connection = Depends(get_conn)) -> list:
    return [dataclasses.asdict(r) for r in advisor.net_worth(conn)]


@router.get("/api/networth/history")
def get_networth_history(conn: sqlite3.Connection = Depends(get_conn)) -> list:
    return advisor.net_worth_history(conn)


@router.get("/api/reconciliation")
def get_reconciliation(conn: sqlite3.Connection = Depends(get_conn)) -> list:
    return [dataclasses.asdict(r) for r in advisor.reconcile(conn)]


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


@router.get("/api/flows")
def get_flows(
    request: Request,
    month: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """One month's cash-flow Sankey (dominant currency only). null if no activity."""
    month = month or date.today().strftime("%Y-%m")
    mf = analytics.month_flows(conn, month, request.app.state.cfg.category_groups)
    return dataclasses.asdict(mf) if mf else None


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


@router.get("/api/projection")
def get_projection(conn: sqlite3.Connection = Depends(get_conn)) -> list:
    return [dataclasses.asdict(r) for r in projection.month_projection(conn)]


@router.get("/api/anomalies")
def get_anomalies(conn: sqlite3.Connection = Depends(get_conn)) -> list:
    return [dataclasses.asdict(a) for a in anomalies.anomalies_from_db(conn)]


@router.get("/api/goals")
def get_goals(
    include_archived: bool = False, conn: sqlite3.Connection = Depends(get_conn)
) -> list:
    return [
        dataclasses.asdict(r)
        for r in advisor.goals_status(conn, include_archived=include_archived)
    ]


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


class SettleIn(BaseModel):
    group_id: int
    amount: Optional[str] = None  # major units, e.g. "60.00"; parsed by money.to_minor
    note: Optional[str] = None


@router.post("/api/receivables/settle")
def post_receivables_settle(body: SettleIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Record a manual (non-bank) settlement for a receivable, e.g. a roommate paying
    their share back in cash. Keyed on template+period under the hood, so it survives
    the next `match all --rebuild` even though group ids are not stable across it."""
    amount_minor = money.to_minor(body.amount, "CAD") if body.amount is not None else None
    try:
        return receivablesmod.settle_group(
            conn, body.group_id, amount_minor=amount_minor, note=body.note
        )
    except receivablesmod.ReceivableNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


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


class BulkCategorizeIn(BaseModel):
    ids: list[int]
    category: str
    role: Optional[str] = None
    counterparty: Optional[str] = None


class GoalIn(BaseModel):
    name: str
    target: str  # major units, e.g. "3000.00"; parsed by money.to_minor
    currency: str = "CAD"
    start_date: str
    target_date: Optional[str] = None
    allocation_pct: int = 100
    note: Optional[str] = None


@router.post("/api/rules")
def post_rule(body: RuleIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Add a categorization rule (generalizable) and apply it to ALL history. Rules
    added from the UI are tagged source='manual'. Runs the recompute-all path so a
    new, more specific rule can steal rows an older rule already claimed; manual
    one-off overrides are never touched.

    `categorized` = total transactions now categorized by the whole rule set after
    the recompute (not just the new rule's matches)."""
    try:
        added = classify.add_rule(
            conn, body.kind, body.pattern, category=body.category, role_hint=body.role,
            counterparty=body.counterparty, priority=body.priority, source="manual",
        )
    except classify.InvalidPatternError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    categorized = classify.categorize(conn, recompute_all=True)
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


@router.post("/api/transactions/bulk-categorize")
def post_bulk_categorize(
    body: BulkCategorizeIn, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    """Set the same one-off manual category on multiple transactions in a single
    request (no rule created). One POST carrying the id list, not one per row."""
    if not body.ids:
        raise HTTPException(status_code=400, detail="ids must not be empty")
    category = body.category.strip()
    if not category:
        raise HTTPException(status_code=400, detail="category must not be empty")

    placeholders = ",".join("?" for _ in body.ids)
    rows = conn.execute(
        f"SELECT id FROM raw_txn WHERE id IN ({placeholders})", tuple(body.ids)
    ).fetchall()
    found = {r[0] for r in rows}
    missing = [i for i in body.ids if i not in found]
    if missing:
        raise HTTPException(
            status_code=400, detail=f"no transaction(s) with id {missing}"
        )

    for raw_txn_id in body.ids:
        classify.set_manual_category(
            conn, raw_txn_id, category, role_hint=body.role, counterparty=body.counterparty
        )
    return {"categorized": len(body.ids)}


# ---- goals ------------------------------------------------------------------
# The DB owns a goal's values; config.toml only seeds names that don't exist yet.
# Removal archives (active = 0) rather than deletes, so a stale [[goals]] block
# cannot resurrect a goal the user got rid of.


def _target_minor(body: GoalIn) -> int:
    """Parse the major-unit target. Currency is gated first: money.exponent_for
    silently defaults an unknown code to 2 places, which would let a typo through
    as a goal that matches no transactions and reads 0% funded forever."""
    if body.currency not in money.known_currencies():
        known = ", ".join(money.known_currencies())
        raise HTTPException(
            status_code=400,
            detail=f"unknown currency {body.currency!r}; known currencies are {known}",
        )
    try:
        return money.to_minor(body.target, body.currency)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _write(fn):
    """Run a goals write, mapping its error tree onto HTTP status codes. The detail
    string is user-facing: App.post lifts it straight into the page's error banner."""
    try:
        return fn()
    except goalsmod.DuplicateName as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except goalsmod.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except goalsmod.GoalError as exc:  # ValidationError, AllocationError
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/goals")
def post_goal(body: GoalIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Create an active goal."""
    minor = _target_minor(body)
    gid = _write(lambda: goalsmod.create(
        conn, name=body.name, target_minor=minor, currency=body.currency,
        start_date=body.start_date, target_date=body.target_date,
        allocation_pct=body.allocation_pct, note=body.note,
    ))
    return {"id": gid}


@router.put("/api/goals/{goal_id}")
def put_goal(goal_id: int, body: GoalIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Full replace, including rename. Keyed on id so the name can change."""
    minor = _target_minor(body)
    _write(lambda: goalsmod.update(
        conn, goal_id, name=body.name, target_minor=minor, currency=body.currency,
        start_date=body.start_date, target_date=body.target_date,
        allocation_pct=body.allocation_pct, note=body.note,
    ))
    return {"ok": True}


@router.post("/api/goals/{goal_id}/archive")
def post_goal_archive(goal_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Hide the goal; never deletes. Idempotent."""
    _write(lambda: goalsmod.archive(conn, goal_id))
    return {"ok": True}


@router.post("/api/goals/{goal_id}/unarchive")
def post_goal_unarchive(goal_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Restore the goal. Re-spends its allocation, so this can 400."""
    _write(lambda: goalsmod.unarchive(conn, goal_id))
    return {"ok": True}
