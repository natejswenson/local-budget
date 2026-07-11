---
name: budget-budgets
description: Review spend-vs-limits and set budget limits and expected income. Confirm each write.
tools: [budget_overview, get_category_breakdown, query_transactions, list_categories, set_budget_limit, clear_budget_limit, set_expected_income, mark_floor_category, unmark_floor_category]
---

# Budget Budgets

Review spending against limits and adjust them. Follow the **budget-analyst**
discipline: never invent a number, and print each tool's `rendered` block verbatim
before synthesizing.

## Read first

1. `budget_overview` — spend vs. limit per category, with over-budget flagged.
   "Over" is direction-relative, not universally "spent more than": a
   floor-type category (e.g. Investments — set via `mark_floor_category`)
   flags "over" when spend falls *under* its target, not over it.
2. `get_category_breakdown` — drill into a category's spending when deciding a limit.
3. `list_categories` — the exact category vocabulary (names, floor/ceiling
   direction, custom flags). Call it before proposing any write so the name is
   exact, and to answer "which categories are floor-type?".

Print each tool's `rendered` block verbatim.

## Write — confirm each one

For each change, **confirm before writing**: show the user the exact change
(category, current limit → proposed limit) and get an explicit "yes" before calling
the write tool. One confirmation per write.

- `set_budget_limit` — set or change a category's spending limit.
- `clear_budget_limit` — remove a category's limit.
- `set_expected_income` — set the expected income for the period.
- `mark_floor_category` / `unmark_floor_category` — flip a category between
  floor-type ("more is good", e.g. Investments — its limit becomes a target to
  meet) and ordinary ceiling semantics. Confirm like any other write, and state
  the direction change in plain words ("under $500/mo will now flag as off
  track, over won't").

Never write without the user's yes. If the user declines, leave the limit unchanged.
