---
name: categorize
description: Categorize the bankapp review queue (uncategorized transactions) by adding generalizable rules via the finance CLI. Subscription-billed via Claude Code; never uses the Anthropic API. Trigger when the user asks to categorize transactions, clear the review queue, or after a sync leaves unknowns.
---

# Categorize bankapp transactions

You classify the uncategorized transactions in the local bankapp SQLite store by
writing **rules** (the learn-once cache), never by touching the database directly.
This runs on the user's Claude Code subscription — **do not use the Anthropic API and
do not require any `ANTHROPIC_API_KEY`.** If this skill never runs, unknowns simply
wait in the queue; the pipeline stays fully functional without it.

## Contract (follow in order)

1. **Check the queue size.** Run `finance review count`. If it prints `0`, stop and
   say the queue is empty.
2. **Export the queue.** Run `finance review export --format json`. Each item has
   `raw_txn_id, account_key, posted_date, amount_minor, currency, description_norm,
   description_raw`.
3. **Decide a category per item**, and choose the **most generalizable *safe* pattern**:
   - Prefer a stable **merchant token** over the whole raw string
     (`netflix`, not `netflix.com 866-579-7172 on`).
   - Use `--kind substring` for plain merchant tokens; use `--kind regex` only when a
     token alone is ambiguous, and keep the regex tight.
   - If a line looks like a transfer between the user's own accounts (e.g. `tfr-to`,
     `eft`, `e-transfer to wealthsimple`), tag it with `--role transfer` (leave
     `--category` unset) so the matcher nets it out instead of counting it as spend.
   - Suggested category vocabulary (not exhaustive): `groceries, dining, subscriptions,
     transport, utilities, rent, income, fees, shopping, health, entertainment,
     transfer`.
4. **Persist each verdict as a rule via the CLI only:**
   ```
   finance rules add --kind substring --pattern "<token>" --category "<category>" --source claude
   finance rules add --kind substring --pattern "tfr-to" --role transfer --source claude
   ```
   Always pass `--source claude`. The rule IS the durable cache — the same merchant
   never needs classifying again.
5. **Apply and confirm shrinkage.** Run `finance categorize` then `finance review count`.
   The count should drop. Repeat for any remaining clear cases.
6. **Hand back genuinely ambiguous leftovers** to the human in plain English (id, date,
   amount, description) rather than guessing — a wrong rule mis-categorizes every future
   match.

## Hard rules

- **CLI only.** Never write to the DB, edit `txn_interp`, or run raw SQL. `finance rules
  add` is the sole write path.
- **No Anthropic API.** Subscription-billed Claude Code only.
- **Don't weaken existing rules** or add overlapping patterns that would shadow a more
  specific rule (lower `--priority` wins; default 100).
- Patterns are matched case-insensitively against `description_norm` (already lowercase).
