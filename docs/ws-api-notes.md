# ws-api 0.35.0 — verified probe notes (T3.1 gate)

Source read from the installed package on 2026-07-05 (Python 3.14 venv). These are
**verified against installed source**, so ws-api snippets in the plan are no longer
illustrative for the pieces documented here. Anything not covered below (real field
*values*, e.g. exact `amountSign`/`status` strings) is flagged as an assumption to
confirm against a real account during the manual smoke test.

## Package surface

`from ws_api import WealthsimpleAPI, WSAPISession` plus exceptions:
`LoginFailedException`, `OTPRequiredException`, `ManualLoginRequired`,
`WSApiException`, `CurlException`, `UnexpectedException`.

## Auth + session persistence

```
WealthsimpleAPI.login(
    username, password, otp_answer=None,
    persist_session_fct=None, scope='invest.read trade.read tax.read'
) -> WSAPISession                                  # interactive: raises OTPRequiredException until otp_answer given
WealthsimpleAPI.from_token(sess: WSAPISession, persist_session_fct=None, username=None)
```

- `WSAPISession` is a dataclass with `.to_json()` / `WSAPISession.from_json(str)`.
- **Token-refresh persistence hook**: `persist_session_fct` is called by the library on
  (re)auth with `self.session.to_json()` — a **JSON string**. It accepts a 1-arg
  `fn(json_str)` or 2-arg `fn(json_str, username)` form (it inspects the signature).
  => Persist that JSON string to keyring; on every call overwrite it so refreshed
  tokens survive. Restore with `WSAPISession.from_json(keyring_value)` +
  `from_token(sess, persist_session_fct=save_cb)`.

## get_accounts / balances

```
get_accounts(open_only=True, use_cache=True)      # list[dict]; each has id, currency,
                                                  # unifiedAccountType, nickname, custodianAccounts, ...
get_account_balances(account_id)                  # dict {security_or_'sec-c-cad'/'sec-c-usd': quantity}
```
Account dict identity: `account["id"]` is the WS account id → store as accounts.external_id.

## get_activities — the transaction feed (for the mapper)

```
get_activities(account_id: str | list[str], how_many=50, order_by='OCCURRED_AT_DESC',
               ignore_rejected=True, start_date=None, end_date=None, load_all=False) -> list[dict]
```
- Internally runs `FetchActivityFeedItems`, applies `format_activity_description(act)` to
  each (so a human `description` key is present), and filters out
  rejected/cancelled/expired and `LEGACY_TRANSFER` when `ignore_rejected`.
- Each activity dict node (fragment `Activity on ActivityFeedItem`) carries:
  `accountId, amount, amountSign, currency, canonicalId, occurredAt, status, type,
  subType, spendMerchant, eTransferName, eTransferEmail, institutionName,
  aftOriginatorName, externalCanonicalId, opposingAccountId, securityId, description(added)`.

### Fields the mapper uses
| need | field | notes |
|---|---|---|
| dedup id | `canonicalId` | -> `wsid:<canonicalId>` |
| account | `accountId` | WS account id -> map to config key via accounts.external_id |
| magnitude | `amount` | string, unsigned magnitude |
| sign | `amountSign` | **ASSUMED** values `"positive"`/`"negative"`; mapper treats a value containing `neg` as negative, else positive. VERIFY on real data. |
| currency | `currency` | e.g. `CAD`/`USD` |
| instant | `occurredAt` | ISO-8601 **UTC** instant -> convert to America/Vancouver local date (midnight boundary matters) |
| status | `status` | **ASSUMED** posted-ish; mapper skips when status lowercases to contain `pending`. VERIFY. |
| description | `description` | pre-formatted by the lib; fall back to `"{type}: {subType}"` |

## Exceptions to catch in `finance sync ws`
`OTPRequiredException` (need `finance ws login`), `LoginFailedException`/`ManualLoginRequired`
(re-login), `WSApiException`/`CurlException`/`UnexpectedException` (warn + soft-skip so the
scheduler run still exits 0). Per-activity `KeyError/AttributeError/TypeError` -> SkipResult
(schema drift degrades, never crashes).

## Confirmed installability
`ws-api==0.35.0` installs cleanly on Python 3.14 (no build issues), so no fallback needed.
