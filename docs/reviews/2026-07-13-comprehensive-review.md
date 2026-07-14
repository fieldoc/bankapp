# Comprehensive review — UI/UX, categorization, goals, feature gaps, usability

**Date:** 2026-07-13 · **Code state:** main @ `53853a6` · **Data:** read-only backup copy of the live DB (2,668 txns, 2024-03 → 2026-07)

## How this review was run

Four lenses, executed in parallel; every reviewer was read-only (no files or data modified; the browser walkthrough ran against a throwaway DB copy on port 8399).

| Lens | Method | Reviewer / model | Why that model |
|---|---|---|---|
| Categorization logic | Code review of `classify/`, `match/`, `normalize.py` + read-only SQL against real rules/txns | Subagent · **Sonnet** | Bounded code-reading with a clear rubric |
| Goals & money math | Code review of `goals.py`, goals API/UI, `advisor.py`, `money.py` + real-DB invariant checks | Subagent · **Sonnet** | Same — focused, local logic |
| Feature gaps | Inventory (README/docs/CLI/API/DB usage) judged against the mission + local-first peers | Subagent · **Opus** | Judgment-heavy product synthesis |
| UI/UX & usability | Live browser walkthrough of all 6 pages, modals, filters, error paths, mobile width | Main agent · **Fable** | Needs the shared browser + holistic judgment; also did final synthesis |

---

## Executive summary

The engine is genuinely strong — immutability enforced by triggers, deterministic rule precedence that resolves all 14 real overlap cases correctly, exact integer money math at the boundary, stateless re-derivable splits. The weaknesses cluster in three places:

1. **Security: the write API is CSRF-exposed (HIGH).** Any webpage you visit while the app runs can POST to `127.0.0.1:8377` — archive goals, create goals, add categorization rules. Starlette parses JSON without checking Content-Type, so `text/plain` form CSRF sails through with no preflight. Fix: same-origin middleware (check `Origin`/`Sec-Fetch-Site`).
2. **Signal drowning in noise.** The "Heads up" panel shows ~60 unactionable items (subscriptions "stopped" 739 days ago, duplicate-charge flags from 2024, habits like 7-Eleven/Canadian Tire detected as "subscriptions"), 137 pending transfer legs of which 95 are >1yr-old and structurally unmatchable (locked-TFSA legs whose counterpart is never ingested). The advisor briefs literally coach around these defects. The mission is "catch money slipping away unnoticed" — the noise is burying exactly that signal.
3. **State communication in the UI.** Goal card shows `BEHIND` + `THIS MONTH: FUNDED` + "−17.01 CAD / 2,000 · 0%" simultaneously; receivables rows say `SETTLED` while showing $103.61 outstanding and a live Settle button; error banners render at page top, far from the modal, prefixed with raw API routes.

---

## 1. Categorization logic (Sonnet agent; verified findings)

**Strengths:** deterministic `(priority, -len(pattern), id)` precedence — re-simulated against all 344 real rules and 591 distinct descriptions, every overlap resolved correctly; DB-level immutability triggers; manual overrides correctly excluded from recompute; splits idempotent by construction (`UNIQUE(raw_txn_id)`); `share_split` conserves cents exactly.

| Sev | Finding | Where |
|---|---|---|
| HIGH | Transfer pairing is amount+date only; confirmed real mispairing (group 7 pairs legs with different WS sub-account codes; the true counterpart went to group 11; an unrelated $500 e-transfer left permanently pending). No balance corruption (both legs zeroed), but the pairing record is factually wrong. | `match/transfers.py:39-75` |
| HIGH (latent) | Currency ignored in transfer matching — `Leg` has no currency field. Dormant (all accounts CAD); first USD account makes cross-currency false pairs. | `match/transfers.py:21-27` |
| MED-HIGH | Locked-account transfer legs can never match: `sync_ws` skips locked `ws-tfsa` txns but rule 343 still tags the outbound leg `transfer` → 41 TFSA legs among **137 pending (95 >1yr, oldest 861d)**, permanently polluting status/digest/dashboard. | `ingest/ws.py:354`, `schema.sql:129` |
| LOW-MED | Deactivated split templates orphan their groups forever (cleanup only sweeps active templates). | `match/splits.py:372-396` |
| LOW-MED | CLI `rules add` doesn't recompute, unlike `rules rm`/`set-counterparty`/web API — queue looks unchanged until a manual `finance categorize`. | `cli.py:378-401` |
| LOW | Refunds on categorized expenses counted as *income* in `v_monthly_cashflow` (≈$274 misrouted in history) instead of reducing spend. | `schema.sql:188-199` |
| LOW | Normalization is lowercase+whitespace only → rule proliferation (`petro canada` + `petro-canada` both exist) and merchant names like "monthly", "the" on the Subscriptions page. | `normalize.py:22-24` |
| Data | Rule 67 `catherine van oort → transport` (source: claude) — person-name e-transfers categorized as transport; observed on two June txns. Person-name rules deserve a category sanity check in the categorize skill. | rules table |

**Test gaps:** no `test_review.py`; determinism-but-not-correctness tests for transfer tie-breaks; nothing for currency, locked-account pending, template deactivation, refund cashflow routing.

## 2. Goals logic (Sonnet agent; verified findings)

**Strengths:** `money.py` rejects floats at the boundary; allocation cap enforced through one choke point used by every path (CLI/API/seed/archive/restore); seed-once ledger survives renames (regression-tested); server-side validation authoritative; XSS properly escaped everywhere checked (including LLM-brief markdown, which escapes first).

| Sev | Finding | Where |
|---|---|---|
| HIGH | CSRF on all goal-mutating endpoints (plus categorization POSTs): no origin check, no CSRF token; bodyless archive/unarchive are simple-request forgeable; Starlette `Request.json()` ignores Content-Type so `text/plain` form CSRF works on create/edit. | `web/app.py:56`, `web/api.py:359-396` |
| MED | `funded = round(net * pct / 100)` — float true-division + banker's rounding in the one place goals math leaves integer land; violates the project's own no-float rule (matters more if BTC/satoshi goals appear — the UI already offers BTC). | `report/advisor.py:587` |
| MED | No allocation history: editing `allocation_pct` (or archive→bump survivor) retroactively rewrites all-time `funded` — the number lies about history. Document or add effective-dating. | `advisor.py:562-587` |
| MED | Cleared **Allocation %** field silently saves as 0 — the exact bug just fixed for `priority` (the fix's own comment describes it) wasn't applied to the sibling field three lines up. | `goals.html:329` |
| LOW | Per-goal independent rounding = no defined remainder-cent owner across a currency (fine per design; deserves a doc line). | `advisor.py:585` |
| LOW | Stale docstrings still claim categorization POSTs are "the sole write path" — 6 goal routes + receivables settle now exist. | `web/api.py:1`, `web/app.py:3` |

**Test gaps:** currency-change on update, negative-net months at `goals_status` level, overfunded goals, XSS round-trip, and any JS-level guard for the modal logic.

## 3. UI/UX walkthrough (main agent, live app on DB copy)

**What works well:** coherent terminal-dark aesthetic; header sync-dates pill; month filter correctly resets pagination, updates per-currency subtotal chips, hides empty currencies; categorize modal is rule-first with editable pattern + one-off option, Esc/backdrop close, stays open on API failure; `PRICE UP` chips; bulk-categorize bar appears on selection; responsive enough at 375px (nav wraps, cards fluid).

Ranked issues:

1. **Overview is a ~6,800px single column** — budgets, Heads up, reconciliation and the brief are below several screens of charts; the four-bucket safe-to-spend card (the hero number) shares the fold with a mostly-empty net-worth card row. IA/layout pass needed (grid, or collapse/reorder sections).
2. **Heads-up wall (see exec summary)** — no dismiss/acknowledge, no recency cap; 45+ duplicate-charge rows back to 2024.
3. **Contradictory state chips** — goal card `BEHIND` + `THIS MONTH: FUNDED` + `−17.01 / 2,000 · 0%`; receivables `SETTLED` + outstanding + Settle button; netted transfer rows show `382.87 CAD` struck through next to `0.00` *and* a "＋ categorize" button (why categorize a matched transfer leg?).
4. **Error surfacing** — banners render at page top (modal can be 1,000px away), prefixed `"/api/goals failed:"`; no field highlighting. (The allocation message itself is excellent: "CAD is 20% allocated; this goal can take at most 80%".)
5. **Form affordances** — Target's `3000.00` placeholder is visually indistinguishable from a value; Allocation defaults to 100% with no headroom hint even though the server knows it (invited error).
6. **Chart legibility** — Sankey right-column labels collide at small values ("utilities/insurance", "vices/entertainment"); Subscriptions donut has a 28-entry legend with truncated names ("ris", "bca", "goo"); donuts carry no amounts; "Spend by category (selected month)" is ambiguous when filter = All months; net-worth chart is a single dot spanning a 12,400-range axis (see G1); recent-months chart legend always shows 3 USD series even when USD is empty.
7. **Sign/format nits** — "CAD net worth $-18.35" (should be −$18.35); "like to save −168.09 CAD" reads as nonsense to a fresh eye.
8. **Advice page** — markdown tables in older briefs render as raw `| pipe |` text with hard-wrapped lines (renderer whitelist lacks tables; the advisor skill emits them).
9. **Leaks table** — unbounded, mixes 2024 and 2026 rows sorted by all-time total; "tim" appears ~20 times as separate month-rows. Needs merchant aggregation + recency filter.
10. Minor: category column is plain text (no chips/color → weak scannability); unlabeled checkbox column; `outline: none` on modal inputs (border-color change is the only focus cue).

*(Testing note: the Browser pane rendered black frames whenever the page was scrolled — a pane compositing artifact, not an app bug; worked around with a tall viewport + translateY, so coverage was complete. Workaround saved to memory.)*

## 4. Feature gaps (Opus agent)

All four `docs/plans/` shipped; no abandoned work. Gaps, ranked by mission fit ("catch leaks → fund joy"):

1. **G1 — Net-worth history is 7 days deep** despite 28 months of flows; back-fill the curve by walking `v_effective` backward from today's balance. [M]
2. **G2 — No per-category month-over-month trend** — the core leak-catching loop (dining 250→310→390) has no surface; `spend_by_category` is single-month. [M]
3. **G5 — Reconciliation hidden while feeds go stale** — td-chequing has 2 snapshots vs 7 for WS accounts; promote drift/staleness to an alert. [S]
4. **G3+G7 — Upcoming-bills calendar + "sweep underspend into a goal" nudge** — detector already knows cadence + last charge; goals' `fixed_monthly` bucket has zero adopters because nothing pulls you into funding. [S–M]
5. **G8 — Briefs are generated daily but never delivered** — write to `~/finance/briefs/` or local notification; a pull-only coach coaches nobody. [S]

Also noted: G4 (no txn notes/tags/manual splits), G6 (safe-to-spend income = median of 3 months while income swings $2.5k–$7.4k — the hero number inherits that volatility), G9 (no CSV export). **Anti-recommendations** (deliberately don't build): cloud/mobile sync, API-based auto-categorization, investment advice, full YNAB envelopes, TD scraping.

---

## Suggested priority order

1. Same-origin middleware on the write API (CSRF) — small, closes the only security hole found.
2. Noise reduction bundle: recency-cap + dismiss for Heads up; stop tagging locked-account transfers as `transfer`; require cadence-consistency before calling a merchant a subscription (or add "not a subscription" action).
3. State-communication fixes: goal chips, receivables SETTLED-vs-outstanding, netted-row display, field-anchored errors without the `/api/...` prefix.
4. `allocation_pct` cleared-field guard + float-division fix (two one-liners already precedented in the codebase).
5. G1 net-worth back-fill + G2 category trends (the two mission-critical chart gaps).
6. The categorize skill: add a "person-name e-transfer ≠ spending category" guard (rule 67 case) and have `rules add` recompute.
