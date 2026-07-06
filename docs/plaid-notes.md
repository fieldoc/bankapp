# Plaid notes (TP.0 gate)

Status: **API grounded; TD-link decision pending the real link.** The plaid-python
surface is verified against the installed version, so the adapter code is grounded. The
one thing only a live link settles — *does TD Canada Trust connect on the Trial tier* —
is confirmed the first time `finance plaid link` succeeds. Plaid's own dashboard shows
"Automatic bank access — no action needed on the free trial" for US/CA institutions.

## Verified API (plaid-python 40.1.0, introspected from installed source)

Docs: https://plaid.com/docs/api/products/transactions/#transactionssync
      https://plaid.com/docs/api/link/#linktokencreate
      https://plaid.com/docs/api/items/#itempublic_tokenexchange

Client:
```
plaid.Configuration(host=plaid.Environment.Production,
                    api_key={"clientId": <id>, "secret": <secret>})
plaid.ApiClient(configuration) -> plaid_api.PlaidApi(api_client)
```
Environments: `Production` (https://production.plaid.com), `Sandbox`. Trial runs on
Production. (No Development env in 40.x.)

- `link_token_create(LinkTokenCreateRequest(user=LinkTokenCreateRequestUser(client_user_id=...),
   client_name=..., products=[Products("transactions")], country_codes=[CountryCode("CA")],
   language="en", webhook=...))` -> `.link_token`
- `item_public_token_exchange(ItemPublicTokenExchangeRequest(public_token=...))`
   -> `.access_token`, `.item_id`
- `transactions_sync(TransactionsSyncRequest(access_token=..., cursor=..., count=...))`
   -> `.added`, `.modified`, `.removed`, `.has_more`, `.next_cursor`, `.accounts`
- `accounts_get(AccountsGetRequest(access_token=...))` -> `.accounts` (each: `account_id`,
   `type`, `subtype`, `mask`, `balances{current, available, iso_currency_code}`)

## Transaction shape (`added[]` items — field names only)

`transaction_id` (stable id -> our `plaid:<id>` dedup key), `account_id`, `date`
(local date), `amount` (**positive = money OUT** -> negate to our signed convention),
`iso_currency_code` (+ `unofficial_currency_code` for crypto), `pending` (skip when
true), `merchant_name` / `name` (description), `personal_finance_category`, ...

## Account mapping

Plaid `account.subtype` -> our config key: `checking` -> `td-chequing`,
`credit card` -> `td-visa` (institution `td`). Stored in `meta['plaid_account_map']`
(account_id -> key) at link time; liabilities normalized negative for balances.

## Secrets

Keyring (service `bankapp`): `plaid-client-id`, `plaid-secret`,
`plaid-access-token-td`. Nothing in config but `[plaid] enabled = true`. Cursor lives
in `meta['plaid_cursor']` (interpretation-layer state, not bank truth).
