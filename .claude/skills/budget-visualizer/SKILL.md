---
name: budget-visualizer
description: Shared visual-report discipline for budget skills — how the deterministic render_report tool works, palette rules, and the manual fallback recipe. Referenced by name, not directly invoked.
tools: []
---

# Budget Visualizer — the shared visual discipline

This is the shared visual discipline any `budget-*` skill references when the
user wants a chart or full visual report. The individual skill still decides
WHEN to offer one.

## The primary path: `render_report`

Visual reports are rendered by the **deterministic renderer**
(`src/local_budget/report/`), not hand-authored per request. The skill that
owns the request (normally `budget-monthly-brief`) calls
`render_report(period, narrative?)` after user confirmation. The renderer:

- builds the fixed page — masthead → stat strip → spend-vs-budget chart →
  monthly trend → flags — directly from `reports.py`/`detect.py` data, so the
  over/floor classification, the shared bar scale, the anomaly month-filter,
  the recurring cross-reference (aliases, bill-like allowlist), and money
  formatting are all computed once, server-side, and covered by tests
  (`tests/test_report_charts.py`, `tests/test_report_flags.py`);
- renders in **PRESS** — the warm-paper editorial brand shared with
  local-fitness, city-report, devlog, ghostwriter and the résumé. Owned by
  `src/local_budget/report/brand.py` (paper `#F5F0E6`, ink `#181510`, dim
  `#6E675C`, accent `#E8501F`), overridable via `BUDGET_BRAND_FILE`, and
  guarded by `tests/test_report_brand.py`. `web/static/palette.css` is the
  DASHBOARD's colors only — the PDF no longer reads it;
- writes `reports/budget-report-<period>.pdf` (gitignored dir, 0700/0600) via
  headless Chrome discovered automatically (`LOCAL_BUDGET_CHROME` overrides).

The **only** LLM-authored content on the page is the optional `narrative` —
1-3 plain-text sentences grounded in `rendered` blocks already printed this
conversation. It is HTML-escaped into a fixed slot; markup is not rendered.

Chart-content changes (row rules, colors, new sections) are code changes to
`report/charts.py` + its golden tests — relay user requests for them as
feedback, don't try to override per request.

An ad-hoc "just one chart" ask outside the monthly report is still served by
the full `render_report` PDF — point the user at it rather than hand-building
a one-off (a lone stat row is the only exception worth hand-writing, and even
then prefer the tool).

## After the report: Amazon follow-ups

Amazon item detail is deliberately **not** on the page. A monthly report is a
fixed one-pager, and a list of twenty-six product titles would swamp it while
answering a question the reader may not have asked. It lives in follow-up
instead, which is also where it is genuinely useful — the report shows that
Shopping ran over, and the follow-up says what it was.

Once a report is rendered, offer the breakdown when the numbers invite it —
Shopping or another Amazon-heavy category over budget, or an anomaly on an
`AMAZON`/`AMZN` merchant:

- `amazon_breakdown(month)` — item titles, quantities, line totals.
- `amazon_coverage(month)` — **check this before trusting a breakdown.**

### Answering "break down my Amazon purchases"

The literal ask, and the one this exists for. Twenty-six undifferentiated rows
is a list, not a breakdown — **group before you print**:

1. Print the tool's `rendered` block. It is the source of truth and its header
   already carries item count, order count and the summed line total.
2. Above it, add a grouped table — the kinds of thing bought (household,
   kids, kitchen, school, personal care…), each with a summed amount.
3. Lead with the one sentence the report could not say. "Two-thirds of July's
   Amazon spend was school supplies and kitchen gear, not discretionary
   shopping" is the answer; the table is the evidence.

Two constraints on that grouping, both load-bearing:

- **The groups are your judgment, and must be labelled as such.** There is no
  product category in the data — only titles, ASINs and sellers. Never present
  a grouping as though the ledger asserted it.
- **Group totals must sum to the printed line total.** Every item lands in
  exactly one group. Amounts come from the `rendered` rows, never re-derived.

**The line total is not the charge total, and saying otherwise contradicts the
report.** Item prices are list prices before discounts, promotions and gift
cards; the charge is what settled, and it is routinely a few percent lower.
Quote the line total as *what the items list at* and the charge total
as *what you spent* — never swap them, and never present their difference as
an error.

If a grouping suggests items are miscategorised in the ledger — school
supplies sitting in Shopping — say so and offer to recategorise the
transaction. Do not recategorise as a side effect of answering.

Coverage is the honesty gate. It is the share of Amazon **dollars** with items
behind them, and it is routinely below 100%: subscription billing like Audible
is charged through Amazon but is not an order, so it has nothing to attribute.
A breakdown at 60% coverage is not "what you bought at Amazon", it is what you
bought in the 60% that reconciled — say so rather than presenting a partial
list as the whole. Both tools print a coverage warning; do not drop it.

Item data refreshes automatically before a `report-pdf` for the current or
previous month, and never at the report's expense — if the Amazon session has
expired the report still renders and the CLI prints a note. If coverage looks
stale or a period was never synced, the fix is `budget amazon sync`, which the
user runs; do not attempt it as a side effect of a question.

## Displayed figures are extracted, never recomputed

Wherever a skill DOES place a figure in prose or narrative: use the dollar
string exactly as a tool's `rendered` block formatted it — never re-derive
from raw cents or do money arithmetic (budget-analyst rule 3). Figures are
point-in-time snapshots; they drift as new transactions post.

---

## Appendix: manual fallback (only when `render_report` errors)

Use ONLY when the tool reports Chrome missing/failed and the user still wants
a report now. This is the condensed original recipe — the renderer implements
exactly these rules; when in doubt, read `report/charts.py`/`flags.py`.

1. **Data.** `get_month_summary`, `get_category_breakdown`, `budget_overview`,
   `monthly_trend`, `find_anomalies`, `recurring_charges`, plus
   `query_transactions(month=<period>, limit=500)` for the recurring
   cross-reference. That last call is internal-only — its `rendered` block is
   never printed (the one exemption from budget-analyst rule 2). Every OTHER
   gathered tool's `rendered` block is still printed verbatim.
2. **Page.** One HTML file, fixed light theme, tokens on `:root` (never a
   wrapper div — descendants of `body` can't see wrapper-scoped tokens).
   PRESS tokens from `report/brand.py`: `--paper` `--ink` `--dim` `--accent`
   `--ink-mid`. Sections: masthead → stat strip → spend-vs-budget → flags.
   **The accent is spent at most twice** — the stamp and the one headline
   figure — plus the overspend bar segment. Nothing else is orange.
3. **Spend-vs-budget rules.** Row set: positive spend only, EXCEPT a floor row
   (`floor == true` in `budget_overview`'s payload — never from memory) with
   `over == true`, which always renders (bar floors at zero; tick + mark +
   trailing text still show). **Every bar is ink** — there is no traffic
   light. Over-budget is carried by a `⚠` mark and by the accent segment past
   the budget tick, so the orange is the SIZE of the overspend. Floor rows are
   never split; exceeding a savings target is the goal, not an overrun. Scale
   = max over CEILING rows only, so a $13k investment transfer cannot crush
   every spending row to a stub; anything past it clamps with a `»` mark.
   Trailing text "$spent of $budget · pct%" (or "$spent") extracted from
   rendered blocks. Sort by spend desc, ties alphabetical. Empty set → "no
   spending to show".
4. **Flags rules.** Anomalies: filter to the month, drop any merchant present
   in `recurring_charges`' full list. Recurring: exact-merchant match only
   (aliases in `report/flags.py:MERCHANT_ALIASES`), `merchant_norm != UNKNOWN`,
   amount < 0, category in `report/flags.py:BILL_LIKE_CATEGORIES`; display the
   latest qualifying txn's own date/amount (never sum; tie → first in returned
   order); header "Amount", label "subscriptions & recurring bills in
   <month>", caption noting month-scoping. Both subsections independently
   shown-or-omitted; both empty → "nothing to flag".
5. **Render.** Write scratch HTML, then
   `<chrome-binary> --headless --disable-gpu --no-pdf-header-footer
   --force-device-scale-factor=2 --print-to-pdf=reports/budget-report-<period>.pdf
   file://<scratch>` using the same browser-discovery order as
   `report/pdf.py` (env `LOCAL_BUDGET_CHROME` → app bundles → PATH). Delete
   the scratch HTML afterwards — it holds a full month of financials.
