# Cash-flow Sankey — Overview page (design)

_Status: implemented 2026-07-10._

## What & why

The chart type Graham saw in Monarch Money is a **Sankey diagram**: nodes in
columns, bands between them whose width is proportional to dollars. He wanted it
as the first chart on the BankApp Overview page.

Monarch's is four columns — income sources → Income → category groups →
categories — plus a Savings band. BankApp had a flat `category` column and no
grouping level, so this feature adds a config-defined category→group mapping and
the chart.

## Decisions

- **Four columns (Option B).** Income sources (parsed from descriptions) → Income
  → category groups + Savings → categories.
- **Income side split by employer**, parsed display-only from
  `v_effective.description_norm` (`direct deposit: from <x>` → title-cased). Never
  affects categorization. Fallback `Other income`.
- **Category → group mapping is config-only** — a new `[category_groups]` section
  in `config.toml`. No DB table, no seeding: the web routes already carry
  `request.app.state.cfg`, and the mapping is pure display metadata, so this
  sidesteps the budgets/goals init-vs-refresh seeding wart. Unmapped categories
  and `(uncategorized)` fall into a fallback group `Other`.
- **Month picker**, defaulting to the last complete month (a partial current
  month makes the Savings band misleading).
- **Dominant currency only** per month (the one with the most volume); other
  active currencies are noted in a line under the chart.
- **Rendering:** vendored `chartjs-chart-sankey` v0.14.4 (MIT), which auto-registers
  on load against the vendored Chart.js 4.4.9 — verified in headless Chrome before
  any code depended on it. Fully offline, no CDN.

## The correctness core: reconciliation

The Sankey's income and spend totals are read straight from
`v_monthly_cashflow`, and the per-source / per-category queries reuse that view's
exact predicates (income excludes ungrouped reimbursement inflows; per-category
spend subtracts them within the reimbursement's own category). So partitioned
sums reconcile with the view by construction. Asserted in
`test_month_flows_reconciles_with_v_monthly_cashflow` and confirmed against all
six months of the live ledger.

**Edge cases:**
- **Overspent month:** Savings node omitted, outflows exceed income (the plugin's
  `size:'max'` widens the Income node honestly); a "Overspent by $X" note renders.
  Band-width-equals-dollars is the chart's invariant — never clamp.
- **Negative-net category** (reimbursements exceed a category's spend that month):
  omitted from links (a negative band is unrenderable); the reported spend total
  still reflects the view. On the live ledger this surfaces as ungrouped,
  uncategorized reimbursement inflow sitting in the `(uncategorized)` bucket, so
  the visible category bands can sum higher than net spend while Savings stays
  correct.

## Node-key scheme

Keys are prefixed to prevent cross-column collisions (a real category is literally
named `income`) and to encode the column for the renderer:
`src:` (0) → `inc:Income` (1) → `grp:` / `sav:Savings` (2) → `cat:` (3). A
`labels` map carries the display string so the frontend never parses keys.

## Files

- `src/bankapp/report/analytics.py` — `income_source_label`, `FlowLink`,
  `MonthFlows`, `month_flows`.
- `src/bankapp/config.py` + `config.example.toml` — `[category_groups]`.
- `src/bankapp/web/api.py` — `GET /api/flows?month=`.
- `src/bankapp/web/static/index.html` + `app.css` — the section, month picker,
  chart script, `.chart-wrap.tall`.
- `src/bankapp/web/static/vendor/chartjs-chart-sankey.min.js` + README.
- Tests across `test_config.py`, `test_report_analytics.py`, `test_web_api.py`,
  `test_web_static.py`, plus the `[category_groups]` addition to
  `conftest.py`'s config template.
