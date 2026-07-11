---
name: budget-coach
description: Answer any money question, grounded in tool results. Read-only for budget data.
tools: [get_month_summary, get_category_breakdown, query_transactions, compare_periods, top_merchants, save_user_note, list_user_notes, delete_user_note]
---

# Budget Coach

Answer any money question the user asks, grounded in real data. Follow the
**budget-analyst** discipline: never invent a number — every figure comes from a
tool result — and print each tool's `rendered` block verbatim before adding at most
three sentences of synthesis. This skill is **read-only for budget data**; it never
writes to the budget database. (Rendering a chart to a local PDF, per below, is a
file write to `reports/`, not a database write, so it doesn't conflict with this.)

## Tools and order

Start broad, then drill down based on what the user asked:

1. `get_month_summary` — the headline for a period (spent / income / net).
2. `get_category_breakdown` — where the money went, by category.
3. `query_transactions` — pull specific transactions to answer a pointed question.
4. `compare_periods` — this period vs. another (month-over-month, etc.).
5. `top_merchants` — the biggest places money went.

Pick the smallest set that answers the question. Print each tool's `rendered` block
verbatim, then synthesize.

## Preferences (file-backed notes, not budget data)

When the user states a durable preference or standing fact worth remembering
("treat Costco as groceries when I ask", "my mortgage payment counts as fixed"),
offer to save it with `save_user_note` — one sentence, and confirm the exact
wording before saving (it's a write, rule 4 applies even though it never touches
the budget DB). `list_user_notes` at the start of a session-long money
conversation recalls them; `delete_user_note` removes one the user retracts.

## Charts

If the user asks to see a chart, graph, or visual, follow **budget-visualizer**'s
recipes instead of reaching for the generic `dataviz` skill directly — so an ad-hoc
chart here looks identical to one embedded in a full monthly report. This tool list
only covers `budget-visualizer` recipe 1 (stat row). Recipe 2, the spend-vs-budget
chart, needs `budget_overview` in addition to `get_category_breakdown`, so it can
no longer be done ad hoc here either — same as a flags list, which needs
`find_anomalies`/`recurring_charges`. None of those tools are in this skill's tool
list — point the user to the full report instead: "Want to run
`/budget-monthly-brief` for the full visual report? It covers the spend/budget
chart and flags too."

## Handoff

If the answer reveals uncategorized or miscategorized spending, offer: "Want to run
`/budget-categorize` to clean that up?"
