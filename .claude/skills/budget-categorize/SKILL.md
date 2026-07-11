---
name: budget-categorize
description: Work the review queue — categorize uncategorized merchants and transactions. Confirm each write.
tools: [review_queue, query_transactions, list_categories, set_merchant_category, set_txn_category, add_custom_category, remove_category]
---

# Budget Categorize

Work the review queue. Follow the **budget-analyst** discipline: never invent a
number, and print each tool's `rendered` block verbatim before synthesizing.

## Read first

1. `review_queue` — the uncategorized merchants and checks that need attention.
2. `query_transactions` — pull the specific transactions behind a merchant when you
   need detail before deciding a category.
3. `list_categories` — the exact assignable vocabulary. Call it before proposing
   categories so every proposal uses an exact existing name (writes reject
   near-misses like "Grocerys").

Print each tool's `rendered` block verbatim.

## Write — confirm each one

For each categorization, **confirm before writing**: show the user the exact change
(merchant or transaction → proposed category) and get an explicit "yes" before
calling the write tool. One confirmation per write.

- `set_merchant_category` — assign a category to a merchant (applies going forward).
- `set_txn_category` — assign a category to a single transaction.
- `add_custom_category` — create a new category when none of the existing ones fit.
- `remove_category` — remove a spend category by merging it into another (moves
  its transactions, rules and budget). Destructive-ish: spell out exactly what
  moves where before asking for the yes.

Never write without the user's yes. If the user declines a proposed category, move
on to the next item in the queue.

**Undo.** A wrong categorization is fixed by re-calling the same set tool with the
right category. A whole import gone wrong (wrong file, duplicates) is undone from
the terminal with `budget undo` — that's a CLI escape hatch, not a tool; point the
user there rather than trying to reverse an import tool-by-tool.

**Never propose "Random."** It's a last-resort catch-all, not a default for
ambiguous merchants — proposing it defeats the point of categorizing at all.
If nothing fits, say so and leave the item in the review queue for the user
to decide, rather than defaulting into Random. (The write tools also refuse
`category="Random"` unless explicitly confirmed, as a backstop — but the
right move here is to just not propose it.)
