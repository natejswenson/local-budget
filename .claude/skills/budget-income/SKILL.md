---
name: budget-income
description: Income by source, expected vs. actual. Read-only.
tools: [income_by_source, income_transactions, get_month_summary, query_transactions]
---

# Budget Income

Show where income comes from and how actual compares to expected. Follow the
**budget-analyst** discipline: never invent a number, and print each tool's
`rendered` block verbatim before adding at most three sentences of synthesis. This
skill is **read-only**; it never writes.

## Tools and order

1. `income_by_source` — income grouped by source for the period.
2. `income_transactions` — the individual income transactions for a given source.
3. `get_month_summary` — the period headline, to frame income against total spend.

Print each tool's `rendered` block verbatim, then synthesize: is actual income
tracking expected, and which source drives the difference.

## Handoff

If expected income looks wrong or unset, offer: "Want to run `/budget-budgets` to
set expected income?"
