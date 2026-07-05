---
name: budget-subscriptions
description: Audit recurring charges and subscriptions — price creep, splits, limits. Confirm each write.
tools: [recurring_charges, subcategory_breakdown, get_category_breakdown, query_transactions, split_subscriptions, set_budget_limit]
---

# Budget Subscriptions

Audit recurring charges and subscriptions. Follow the **budget-analyst** discipline:
never invent a number, and print each tool's `rendered` block verbatim before
synthesizing.

## Read first

1. `recurring_charges` — the recurring/subscription charges detected.
2. `subcategory_breakdown` — spend within a category by subcategory (e.g. which
   subscriptions roll up under a category).
3. `get_category_breakdown` — the broader category context.

Print each tool's `rendered` block verbatim, then call out price creep and anything
unused or worth cancelling.

## Write — confirm each one

For each change, **confirm before writing**: show the user the exact change and get
an explicit "yes" before calling the write tool. One confirmation per write.

- `split_subscriptions` — split a bundled subscription charge into its parts.
- `set_budget_limit` — set a limit on the subscriptions/recurring category.

Never write without the user's yes.
