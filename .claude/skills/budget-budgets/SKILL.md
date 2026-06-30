---
name: budget-budgets
description: Review spend-vs-limits and set budget limits and expected income. Confirm each write.
tools: [budget_overview, get_category_breakdown, set_budget_limit, clear_budget_limit, set_expected_income]
---

# Budget Budgets

Review spending against limits and adjust them. Follow the **budget-analyst**
discipline: never invent a number, and print each tool's `rendered` block verbatim
before synthesizing.

## Read first

1. `budget_overview` — spend vs. limit per category, with over-budget flagged.
2. `get_category_breakdown` — drill into a category's spending when deciding a limit.

Print each tool's `rendered` block verbatim.

## Write — confirm each one

For each change, **confirm before writing**: show the user the exact change
(category, current limit → proposed limit) and get an explicit "yes" before calling
the write tool. One confirmation per write.

- `set_budget_limit` — set or change a category's spending limit.
- `clear_budget_limit` — remove a category's limit.
- `set_expected_income` — set the expected income for the period.

Never write without the user's yes. If the user declines, leave the limit unchanged.
