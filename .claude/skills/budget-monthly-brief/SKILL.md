---
name: budget-monthly-brief
description: Compose the period brief — spent/income/net, where it goes, ways to save, flags — then offer to save it. Also handles visual/chart report requests for a period.
tools: [get_month_summary, get_category_breakdown, insights, monthly_trend, find_anomalies, recurring_charges, query_transactions, save_brief, budget_overview, render_report]
---

# Budget Monthly Brief

Compose the period brief. Follow the **budget-analyst** discipline: never invent a
number, print each tool's `rendered` block verbatim, then add at most three
sentences of synthesis per section.

## Compose the brief (read tools, in order)

1. `get_month_summary` — the headline: spent, income, net, AND the numbered
   "Where it goes" category table (this is the brief's category section —
   do NOT also call `get_category_breakdown` here: two numbered lists of the
   same rows in one turn make every row reference ambiguous by construction;
   keep `get_category_breakdown` for later drill-downs only).
2. `monthly_trend` — the spend trend across recent months.
3. `insights` — concrete ways to save.
4. `find_anomalies` — unusual charges worth flagging (pass the brief's month
   so the rendered block is scoped, not two years of history).
5. `recurring_charges` — subscriptions and recurring flags.

Print each tool's `rendered` block verbatim. The brief reads: spent / income / net
→ where it goes → ways to save → flags.

## Save the brief (write — confirm first)

After composing, offer to save the brief with `save_brief`. **Confirm before
writing:** show the user the exact brief text you would save and get an explicit
"yes" before calling `save_brief`. If the user declines, leave it unsaved.

## Visual report (on demand)

After the save-brief question resolves (yes or no), offer one more thing: "Want
this as a visual report too?"

If yes: confirm, then call `render_report(period, narrative)` — the
deterministic renderer builds the whole page (stat row → spend-vs-budget chart
→ trend → flags) server-side and writes
`reports/budget-report-<period>.pdf` (gitignored, 0600). The optional
`narrative` is 1-3 plain-text sentences grounded ONLY in figures from
`rendered` blocks already printed this conversation — it's the one free-text
slot on the page; never restate numbers from memory into it. Print the tool's
`rendered` confirmation verbatim: the path is the user's to open, move, or
delete. Regenerating the same period overwrites that period's PDF; different
periods never collide.

Chart-content tweaks ("hide category X", "different colors") aren't
per-request options — the page is deterministic by design. Relay such requests
as feedback on the renderer; only the narrative varies per render. If the tool
errors with a Chrome-missing message, follow the fallback appendix in
**budget-visualizer**.

**Direct-visual-request carve-out.** When the request is specifically and only
for the visual/chart report (e.g. "show me the visual report for June," not a
general spending question), call `render_report` straight away (still confirm
the file write first) — skipping the narrative brief walk and the save-brief
question. "Give me June's numbers and a chart" does **not** qualify (a numbers
request with a visual add-on) — that follows the normal flow above.
