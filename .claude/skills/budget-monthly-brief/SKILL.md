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

1. `get_month_summary` — the headline: spent, income, net.
2. `get_category_breakdown` — where the money goes.
3. `monthly_trend` — the spend trend across recent months.
4. `insights` — concrete ways to save.
5. `find_anomalies` — unusual charges worth flagging.
6. `recurring_charges` — subscriptions and recurring flags.

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
reported month for the flags section (see **budget-visualizer** Recipe 4);
its `rendered` block is internal-only and exempt from **budget-analyst** rule
2, never printed to the user — then follow **budget-visualizer**'s recipes to
build one HTML artifact, in this fixed order: stat row → category chart →
budget-vs-actual meters → flags. Name the scratch file per period (e.g.
`budget-report-2026-06.html`) so reports for different months in the same session
don't collide. Publish via the `Artifact` tool. Then ask one cleanup question:
"Want me to delete the local scratch file now, or keep it in case you want
changes?" — never phrase this as deleting the artifact/page itself, since that
isn't possible (there is no tool to delete a published Artifact).

If the user later asks for a tweak in the same session, redeploy to the same
Artifact URL by reading the same scratch file, editing it, and calling `Artifact`
again — this is why "keep it" is the sensible default when the user is ambiguous.
If the scratch file was already deleted, rebuild fresh instead: re-gather the tool
data, re-render, and publish via a new `Artifact` call (this produces a new URL).

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
