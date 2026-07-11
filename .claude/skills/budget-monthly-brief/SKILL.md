---
name: budget-monthly-brief
description: Compose the period brief — spent/income/net, where it goes, ways to save, flags — then offer to save it. Also handles visual/chart report requests for a period.
tools: [get_month_summary, get_category_breakdown, insights, monthly_trend, find_anomalies, recurring_charges, query_transactions, save_brief, budget_overview]
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

If yes: gather (or reuse already-fetched) `get_month_summary`,
`get_category_breakdown`, `budget_overview`, `find_anomalies`,
`recurring_charges`, and `query_transactions(month=<period>, limit=500)`
results — the last call cross-references recurring merchants against the
reported month for the flags section's subscriptions & recurring bills
subsection (see **budget-visualizer** Recipe 3); its `rendered` block is
internal-only and exempt from **budget-analyst** rule 2, never printed to the
user — then follow **budget-visualizer**'s recipes to build one HTML page, in
this fixed order: stat row → spend-vs-budget chart → flags. Write it to a
scratch file, then render it to PDF (see **budget-visualizer**'s "Rendering to
PDF" section) at `reports/budget-report-<period>.pdf` (e.g.
`reports/budget-report-2026-06.pdf`) — the `reports/` directory is gitignored,
so this never risks committing personal financial data. Regenerating the same
period overwrites its existing PDF; different periods get their own file, so
nothing collides. Tell the user the file path and that it's theirs to open,
move, or delete — this is a plain local file, fully under their (and your)
control, unlike a published `Artifact`.

If the user later asks for a tweak in the same session, edit the same scratch
HTML file and re-render it to the same PDF path (overwriting it). If the
scratch file was already discarded, rebuild fresh instead: re-gather the tool
data, re-render, and re-run the PDF conversion.

**Direct-visual-request carve-out.** When the request is specifically and only for
the visual/chart report (e.g. "show me the visual report for June," not a general
spending question), skip straight to gathering the same 6 tools (including
`query_transactions(month=<period>, limit=500)` for the recurring-charges
cross-reference) and rendering the artifact — skipping the narrative brief walk
and the save-brief question. "Give me June's numbers and a chart" does **not**
qualify (a numbers request with a visual add-on) — that follows the normal flow
above. Either way, each gathered tool's `rendered` block is still printed
verbatim per **budget-analyst** rule 2 — except `query_transactions`' own
cross-reference call, which is exempt (its `rendered` block is internal-only,
never printed) — the carve-out only skips the narrative synthesis and the
save-brief question, not the rendered blocks themselves.
