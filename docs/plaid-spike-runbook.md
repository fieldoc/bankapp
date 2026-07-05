# TP.0 Plaid spike — runbook (do this together, ~30 min)

**Goal:** confirm TD Canada Trust is linkable on Plaid's free Trial tier, pull one
`/transactions/sync` page, and record the payload *shape* (field names only, no real
data) plus a go/no-go decision into `docs/plaid-notes.md`. **No Plaid adapter code gets
written until that notes file exists** — this is a hard gate.

## Credential boundary (important)
- **You** create the Plaid account and accept its terms (I can't create accounts or
  accept ToS on your behalf).
- **You** enter your TD EasyWeb credentials — only ever inside **Plaid Link's own UI**.
  I never see or type your bank password; Plaid handles it and returns a revocable
  token. bankapp stores that token in your OS keyring, never in the repo.

## Steps

1. **Create a Plaid account** at https://dashboard.plaid.com/signup (free; the Trial
   plan created ≥ April 2026 allows up to 10 production Items — we need 1).
2. In the dashboard, request/confirm **Production** (or Trial-production) access. Note
   whether TD Canada Trust appears as an available institution for Canada.
3. Grab your **client_id** and **production secret** from the dashboard
   (Team Settings → Keys). Keep them in the dashboard tab; we'll put them in the OS
   keyring later, not in any file.
4. **Link TD** using Plaid's **Hosted Link** (dashboard can generate a hosted Link URL
   for a quick test) or Link in Sandbox first to see the flow. In the real link, choose
   TD Canada Trust and sign in with your EasyWeb credentials *in Plaid's UI*. Confirm
   both **chequing** and **Visa** come back as accounts.
5. Pull **one** `/transactions/sync` page for the linked Item (the dashboard's API
   explorer or a one-off `curl`/script works). Look at one `added[]` transaction.

## What to capture in `docs/plaid-notes.md` (redacted — field names only)

- Decision: **PROCEED** (TD linkable on the tier) or **FALL BACK** to file-drop only.
- The `account_id` values and their mapping to our config keys (`td-chequing`, `td-visa`).
- The shape of one `added[]` transaction: which fields exist —
  `transaction_id`, `account_id`, `date`, `amount` (and its **sign convention** —
  Plaid uses positive = money out), `iso_currency_code`, `pending`, `merchant_name` /
  `name`, plus whatever else is present. **No real amounts, merchants, or dates** — just
  the keys and their types.
- The `next_cursor` field name and how pagination/`has_more` works.
- The `plaid-python` version you used (so we pin it).

Once that file exists with a PROCEED decision, I build TP.1–TP.3 (client+keyring, sync
mapper+cursor, wire into refresh) against the real field names. If it says FALL BACK, the
pipeline is already fully functional on file-drop and we simply skip Phase 3B.
```
