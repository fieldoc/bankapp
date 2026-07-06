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

## Access tier — you do NOT need the business "Production access" application

Signing up puts you on the **Trial plan** (US/Canada teams created on/after 2026-04-15):
real **production** data, **free**, up to **10 linked accounts**, Transactions included,
**no business registration / security questionnaire / contract**. It's auto-approved
after a personal **identity verification** (a flagged sign-up may get a 2–3 day manual
review). The Trial plan *is* your production access — just free and capped. You never
apply through the paid Production process for this project (it needs 1 account).

## Steps

1. **Create a Plaid account** at https://dashboard.plaid.com/signup and complete the
   personal identity verification. This lands you on the Trial plan automatically.
2. Confirm you're on the **Trial plan** (dashboard shows your plan + a 10-Item quota).
   Nothing to "request" — no business application.
3. Grab your **client_id** and **secret** from the dashboard (Team Settings → Keys;
   Trial keys run against the production environment). Keep them in the dashboard tab —
   we put them in the OS keyring later, never in a file.
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
