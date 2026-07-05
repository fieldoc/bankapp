"""Wealthsimple adapter: keyring-backed auth + activity mapping + graceful degradation.

Signatures/field names verified in docs/ws-api-notes.md (T3.1 probe). The session
token lives ONLY in the OS keyring (service 'bankapp', entry 'ws-session'); ws-api's
persist_session_fct hook re-saves the refreshed session JSON on every (re)auth.

Schema drift degrades, never crashes: an activity missing an expected field maps to a
SkipResult, and any WS API error in sync() is caught so a scheduled run still exits 0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional, Union
from zoneinfo import ZoneInfo

from bankapp import money
from bankapp.ingest.core import NormalizedTxn, make_txn

SERVICE = "bankapp"
WS_ENTRY = "ws-session"

# WS unifiedAccountType -> our accounts.type (best-effort; single-user tool).
_WS_TYPE_MAP = {
    "CASH": "cash",
    "SELF_DIRECTED_CRYPTO": "crypto",
    "CREDIT_CARD": "visa",
}


class NoSessionError(RuntimeError):
    """No stored WS session; the user must run `finance ws login`."""


@dataclass(frozen=True)
class SkipResult:
    reason: str
    activity_id: Optional[str] = None


@dataclass
class SyncReport:
    inserted: int = 0
    skipped: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


# ---- keyring session persistence -------------------------------------------

def _keyring():
    import keyring

    return keyring


def save_session(session_json: str) -> None:
    """persist_session_fct callback: store the session JSON in the OS keyring."""
    _keyring().set_password(SERVICE, WS_ENTRY, session_json)


def load_session_json() -> Optional[str]:
    return _keyring().get_password(SERVICE, WS_ENTRY)


def clear_session() -> None:
    try:
        _keyring().delete_password(SERVICE, WS_ENTRY)
    except Exception:
        pass


def load_session() -> Optional[Any]:
    from ws_api import WSAPISession

    raw = load_session_json()
    return WSAPISession.from_json(raw) if raw else None


def authenticate(username: str, password: str, otp: Optional[str] = None, api_factory=None):
    """Log in and persist the session to keyring. Returns the WealthsimpleAPI client.

    The password is supplied by the user via the CLI prompt and flows straight into
    ws-api; it is never stored (only the resulting session token is)."""
    if api_factory is None:
        from ws_api import WealthsimpleAPI as api_factory  # noqa: N806
    return api_factory.login(
        username, password, otp_answer=otp, persist_session_fct=save_session
    )


def client_from_keyring(api_factory=None):
    """Rebuild an authenticated client from the stored session (refreshes persist too)."""
    if api_factory is None:
        from ws_api import WealthsimpleAPI as api_factory  # noqa: N806
    sess = load_session()
    if sess is None:
        raise NoSessionError("no WS session stored — run `finance ws login`")
    return api_factory.from_token(sess, persist_session_fct=save_session)


# ---- activity mapping (pure) -----------------------------------------------

def _to_local_date(occurred_at: str, tz: ZoneInfo) -> str:
    s = occurred_at.strip().replace("Z", "+00:00")
    s = re.sub(r"\.\d+", "", s)  # drop fractional seconds (3.10 fromisoformat safety)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%Y-%m-%d")


def map_activity(
    act: dict, account_key: str, tz: Union[str, ZoneInfo]
) -> Union[NormalizedTxn, SkipResult]:
    """Map one WS activity dict to a NormalizedTxn, or a SkipResult (pending / drift)."""
    if isinstance(tz, str):
        tz = ZoneInfo(tz)
    try:
        status = (act.get("status") or "").lower()
        if "pending" in status:
            return SkipResult("pending", act.get("canonicalId"))

        currency = act["currency"]
        sign = (act.get("amountSign") or "").lower()
        magnitude = Decimal(str(act["amount"]))
        signed = -magnitude if "neg" in sign else magnitude
        amount_minor = money.to_minor(signed, currency)

        posted_date = _to_local_date(act["occurredAt"], tz)
        desc = act.get("description") or f"{act['type']}: {act['subType']}"
        return make_txn(
            account_key=account_key,
            posted_date=posted_date,
            amount_minor=amount_minor,
            currency=currency,
            description_raw=desc,
            dedup_key=f"wsid:{act['canonicalId']}",
            source="ws",
        )
    except (KeyError, AttributeError, TypeError, InvalidOperation) as exc:
        return SkipResult(f"schema-drift: {type(exc).__name__}: {exc}", act.get("canonicalId"))


# ---- sync orchestration ----------------------------------------------------

def resolve_ws_account_map(conn, cfg, ws_accounts: list[dict]) -> dict[str, str]:
    """Map WS account id -> config account key, persisting external_id on the accounts row.

    Matches each WS account to a config wealthsimple account by type (CASH->cash, etc.).
    """
    ws_cfg = [a for a in cfg.accounts if a.institution == "wealthsimple"]
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for wsa in ws_accounts:
        ws_id = wsa.get("id")
        want = _WS_TYPE_MAP.get(wsa.get("unifiedAccountType", ""), None)
        match = next(
            (a for a in ws_cfg if a.key not in used and (want is None or a.type == want)),
            next((a for a in ws_cfg if a.key not in used), None),
        )
        if match is None or ws_id is None:
            continue
        used.add(match.key)
        mapping[ws_id] = match.key
        conn.execute("UPDATE accounts SET external_id = ? WHERE key = ?", (ws_id, match.key))
    conn.commit()
    return mapping


def _capture_ws_balances(conn, cfg, client, id_to_key: dict[str, str]) -> None:
    """Best-effort: snapshot each WS account's CAD cash balance. Degrades silently —
    exact investment market-value fields are confirmed at the real smoke test."""
    from datetime import date as _date
    from decimal import Decimal

    from bankapp.report import advisor

    getb = getattr(client, "get_account_balances", None)
    if getb is None:
        return
    type_by_key = {a.key: a.type for a in cfg.accounts}
    curr_by_key = {a.key: a.currency for a in cfg.accounts}
    id_by_key = {r["key"]: r["id"] for r in conn.execute("SELECT id, key FROM accounts")}
    as_of = _date.today().isoformat()
    for ws_id, key in id_to_key.items():
        try:
            balances = getb(ws_id)  # {security: quantity}; cash under 'sec-c-cad'/'sec-c-usd'
            cash = balances.get("sec-c-cad") if isinstance(balances, dict) else None
            if cash is None:
                continue
            currency = curr_by_key.get(key, "CAD")
            minor = money.to_minor(Decimal(str(cash)), currency)
            minor = advisor.normalize_balance_for_type(minor, type_by_key.get(key, ""))
            aid = id_by_key.get(key)
            if aid is not None:
                advisor.snapshot_balance(conn, aid, as_of, minor, currency, "ws")
        except Exception:
            continue


def sync_ws(conn, cfg, client=None, api_factory=None, how_many: int = 200) -> SyncReport:
    """Fetch WS activities and ingest them. Any API error -> report.errors (scheduler-safe)."""
    from bankapp import db as dbmod
    from bankapp.ingest import core

    report = SyncReport()
    try:
        if client is None:
            client = client_from_keyring(api_factory=api_factory)
        ws_accounts = client.get_accounts()
        id_to_key = resolve_ws_account_map(conn, cfg, ws_accounts)
        if not id_to_key:
            report.errors.append("no WS accounts matched config")
            dbmod.set_meta(conn, "ws_last_error", report.errors[-1])
            return report

        _capture_ws_balances(conn, cfg, client, id_to_key)

        txns: list[NormalizedTxn] = []
        for ws_id, key in id_to_key.items():
            activities = client.get_activities(ws_id, how_many=how_many)
            for act in activities:
                mapped = map_activity(act, key, cfg.timezone)
                if isinstance(mapped, SkipResult):
                    report.skipped += 1
                else:
                    txns.append(mapped)

        inserted, dup_skipped = core.insert_batch(conn, txns)
        report.inserted = inserted
        report.skipped += dup_skipped
        dbmod.set_meta(conn, "ws_last_sync", core._utc_now_iso())
        dbmod.set_meta(conn, "ws_last_error", "")
    except NoSessionError as exc:
        report.errors.append(str(exc))
        dbmod.set_meta(conn, "ws_last_error", str(exc))
    except Exception as exc:  # WS API/network error -> soft-skip, exit 0 from refresh
        report.errors.append(f"{type(exc).__name__}: {exc}")
        dbmod.set_meta(conn, "ws_last_error", report.errors[-1])
    return report
