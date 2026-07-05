---
name: budget-monthly-brief
description: Compose the period brief — spent/income/net, where it goes, ways to save, flags — then offer to save it.
tools: [get_month_summary, get_category_breakdown, insights, monthly_trend, find_anomalies, recurring_charges, query_transactions, save_brief]
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
