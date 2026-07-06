"""Plaid adapter for TD (automated daily sync). Gated by the TP.0 spike.

API verified against plaid-python 40.1.0 (see docs/plaid-notes.md). Secrets live only
in the OS keyring (service 'bankapp'): plaid-client-id, plaid-secret,
plaid-access-token-td. The cursor lives in meta['plaid_cursor'] (interpretation-layer
state, not bank truth). raw_txn stays immutable: modified/removed events are logged,
never applied.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional, Union

from bankapp import money
from bankapp.ingest.core import NormalizedTxn, make_txn
from bankapp.ingest.ws import SkipResult  # reuse the same skip type

SERVICE = "bankapp"
K_CLIENT_ID = "plaid-client-id"
K_SECRET = "plaid-secret"
K_ACCESS_TOKEN = "plaid-access-token-td"

# Plaid account subtype -> our config account key (institution 'td').
_SUBTYPE_TO_KEY = {"checking": "td-chequing", "credit card": "td-visa"}


class PlaidCredsError(RuntimeError):
    """Plaid client_id/secret not found in keyring — run `finance plaid keys`."""


class PlaidNotLinkedError(RuntimeError):
    """No stored TD access token — run `finance plaid link`."""


@dataclass
class PlaidSyncReport:
    inserted: int = 0
    skipped: int = 0
    errors: list = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


# ---- keyring -----------------------------------------------------------------

def _keyring():
    import keyring

    return keyring


def store_credentials(client_id: str, secret: str) -> None:
    kr = _keyring()
    kr.set_password(SERVICE, K_CLIENT_ID, client_id)
    kr.set_password(SERVICE, K_SECRET, secret)


def load_credentials() -> tuple[str, str]:
    kr = _keyring()
    cid = kr.get_password(SERVICE, K_CLIENT_ID)
    sec = kr.get_password(SERVICE, K_SECRET)
    if not cid or not sec:
        raise PlaidCredsError("Plaid credentials missing — run `finance plaid keys`.")
    return cid, sec


def store_access_token(token: str) -> None:
    _keyring().set_password(SERVICE, K_ACCESS_TOKEN, token)


def load_access_token() -> Optional[str]:
    return _keyring().get_password(SERVICE, K_ACCESS_TOKEN)


# ---- client ------------------------------------------------------------------

def make_client(environment: str = "production"):
    import plaid
    from plaid.api import plaid_api

    client_id, secret = load_credentials()
    host = plaid.Environment.Sandbox if environment == "sandbox" else plaid.Environment.Production
    configuration = plaid.Configuration(host=host, api_key={"clientId": client_id, "secret": secret})
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))


# ---- pure mapper (TP.2) ------------------------------------------------------

def map_plaid_txn(txn: dict, account_key: str) -> Union[NormalizedTxn, SkipResult]:
    """Map one Plaid `added[]` transaction dict to a NormalizedTxn, or a SkipResult.

    Plaid `amount` is positive for money OUT, so we negate to our signed convention.
    Pending transactions are skipped (immutability forbids flipping pending->posted).
    """
    try:
        if txn.get("pending"):
            return SkipResult("pending", txn.get("transaction_id"))
        currency = txn.get("iso_currency_code") or txn.get("unofficial_currency_code") or "CAD"
        amount = Decimal(str(txn["amount"]))
        amount_minor = money.to_minor(-amount, currency)  # positive=out -> negate
        raw_date = txn["date"]
        posted_date = raw_date if isinstance(raw_date, str) else raw_date.isoformat()
        desc = txn.get("merchant_name") or txn.get("name") or "(no description)"
        return make_txn(
            account_key=account_key,
            posted_date=posted_date,
            amount_minor=amount_minor,
            currency=currency,
            description_raw=desc,
            dedup_key=f"plaid:{txn['transaction_id']}",
            source="plaid",
        )
    except (KeyError, AttributeError, TypeError, InvalidOperation) as exc:
        return SkipResult(f"schema-drift: {type(exc).__name__}: {exc}", txn.get("transaction_id"))


# ---- account mapping ---------------------------------------------------------

def _load_account_map(conn) -> dict[str, str]:
    from bankapp import db as dbmod

    raw = dbmod.get_meta(conn, "plaid_account_map")
    return json.loads(raw) if raw else {}


def _save_account_map(conn, mapping: dict[str, str]) -> None:
    from bankapp import db as dbmod

    dbmod.set_meta(conn, "plaid_account_map", json.dumps(mapping))


def resolve_account_map(accounts, cfg) -> dict[str, str]:
    """Map Plaid account_id -> config key by subtype, restricted to configured td accounts."""
    td_keys = {a.key for a in cfg.accounts if a.institution == "td"}
    mapping: dict[str, str] = {}
    for acct in accounts:
        subtype = str(getattr(acct, "subtype", "") or "").lower()
        key = _SUBTYPE_TO_KEY.get(subtype)
        if key and key in td_keys:
            mapping[str(acct.account_id)] = key
    return mapping


# ---- link flow (localhost) ---------------------------------------------------

_LINK_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>bankapp - link TD</title>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script></head>
<body style="font-family:sans-serif;padding:2rem">
<h2>bankapp - connect your bank</h2>
<p id="msg">Opening Plaid Link...</p>
<script>
var handler = Plaid.create({
  token: "%LINK_TOKEN%",
  onSuccess: function(public_token, metadata) {
    document.getElementById('msg').innerText = 'Linked! You can close this tab.';
    fetch('/success?public_token=' + encodeURIComponent(public_token));
  },
  onExit: function(err, metadata) {
    document.getElementById('msg').innerText = 'Link closed. You can close this tab.';
    fetch('/exit');
  }
});
handler.open();
</script></body></html>"""


def _serve_link_and_wait(link_token: str, port: int, timeout: int = 300) -> Optional[str]:
    """Serve a localhost page hosting Plaid Link; return the public_token on success."""
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    result: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/success":
                qs = parse_qs(parsed.query)
                result["public_token"] = qs.get("public_token", [None])[0]
                self._ok(b"OK")
                done.set()
            elif parsed.path == "/exit":
                self._ok(b"OK")
                done.set()
            else:
                self._ok(_LINK_HTML.replace("%LINK_TOKEN%", link_token).encode("utf-8"))

        def _ok(self, body: bytes):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        webbrowser.open(f"http://127.0.0.1:{port}/")
        done.wait(timeout=timeout)
    finally:
        server.shutdown()
    return result.get("public_token")


def run_link_flow(conn, cfg, client=None, port: int = 8710) -> dict:
    """One-time: create a link token, drive Plaid Link, exchange + store the token, and
    persist the Plaid account_id -> config key mapping. Returns the mapping."""
    from plaid.model.country_code import CountryCode
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.products import Products
    from plaid.model.accounts_get_request import AccountsGetRequest

    if client is None:
        client = make_client(cfg.plaid.environment)

    lt = client.link_token_create(
        LinkTokenCreateRequest(
            user=LinkTokenCreateRequestUser(client_user_id="bankapp-local"),
            client_name="bankapp",
            products=[Products("transactions")],
            country_codes=[CountryCode("CA")],
            language="en",
        )
    )
    public_token = _serve_link_and_wait(lt.link_token, port)
    if not public_token:
        raise RuntimeError("Link was closed before completing — no public token received.")

    exchange = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token)
    )
    store_access_token(exchange.access_token)

    accounts = client.accounts_get(AccountsGetRequest(access_token=exchange.access_token)).accounts
    mapping = resolve_account_map(accounts, cfg)
    _save_account_map(conn, mapping)
    return mapping


# ---- sync (TP.2/TP.3) --------------------------------------------------------

def sync_plaid(conn, cfg, client=None) -> PlaidSyncReport:
    """Cursor-based /transactions/sync -> raw_txn. Any Plaid error -> report.errors
    (scheduler-safe). ITEM_LOGIN_REQUIRED is surfaced for `finance status`."""
    from bankapp import db as dbmod
    from bankapp.ingest import core
    from bankapp.report import advisor

    report = PlaidSyncReport()
    try:
        if load_access_token() is None:
            raise PlaidNotLinkedError("no TD access token — run `finance plaid link`")
        if client is None:
            client = make_client(cfg.plaid.environment)

        account_map = _load_account_map(conn)
        id_by_key = {r["key"]: r["id"] for r in conn.execute("SELECT id, key FROM accounts")}
        type_by_key = {a.key: a.type for a in cfg.accounts}
        access_token = load_access_token()
        cursor = dbmod.get_meta(conn, "plaid_cursor")

        from plaid.model.transactions_sync_request import TransactionsSyncRequest

        while True:
            kwargs = {"access_token": access_token}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.transactions_sync(TransactionsSyncRequest(**kwargs))

            txns: list[NormalizedTxn] = []
            for added in resp.added:
                td = added.to_dict() if hasattr(added, "to_dict") else dict(added)
                key = account_map.get(str(td.get("account_id")))
                if key is None:
                    report.skipped += 1
                    continue
                mapped = map_plaid_txn(td, key)
                if isinstance(mapped, SkipResult):
                    report.skipped += 1
                else:
                    txns.append(mapped)
            if resp.modified or resp.removed:
                report.errors.append(
                    f"{len(resp.modified)} modified / {len(resp.removed)} removed posted txns ignored (raw_txn is immutable)"
                )

            inserted, dup = core.insert_batch(conn, txns)
            report.inserted += inserted
            report.skipped += dup

            # capture balances best-effort
            for acct in resp.accounts:
                key = account_map.get(str(getattr(acct, "account_id", "")))
                bal = getattr(getattr(acct, "balances", None), "current", None)
                if key and bal is not None:
                    aid = id_by_key.get(key)
                    currency = getattr(acct.balances, "iso_currency_code", None) or "CAD"
                    minor = money.to_minor(Decimal(str(bal)), currency)
                    minor = advisor.normalize_balance_for_type(minor, type_by_key.get(key, ""))
                    if aid is not None:
                        from datetime import date as _date

                        advisor.snapshot_balance(conn, aid, _date.today().isoformat(), minor, currency, "plaid")

            cursor = resp.next_cursor
            dbmod.set_meta(conn, "plaid_cursor", cursor)  # persist only after the page applied
            if not resp.has_more:
                break

        dbmod.set_meta(conn, "plaid_last_sync", core._utc_now_iso())
        dbmod.set_meta(conn, "plaid_last_error", "")
    except PlaidNotLinkedError as exc:
        report.errors.append(str(exc))
        dbmod.set_meta(conn, "plaid_last_error", str(exc))
    except PlaidCredsError as exc:
        report.errors.append(str(exc))
        dbmod.set_meta(conn, "plaid_last_error", str(exc))
    except Exception as exc:  # API/network error -> soft-skip
        msg = f"{type(exc).__name__}: {exc}"
        if "ITEM_LOGIN_REQUIRED" in str(exc):
            msg = "ITEM_LOGIN_REQUIRED — re-run `finance plaid link`"
        report.errors.append(msg)
        dbmod.set_meta(conn, "plaid_last_error", msg)
    return report
