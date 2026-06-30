---
name: budget-reconcile
description: Explain the open conflict queue (advisory) and emit the exact CLI command to resolve a conflict.
tools: [open_conflicts]
---

# Budget Reconcile

Explain the open import-conflict queue. This skill is **advisory** — it has no write
tool and resolves nothing itself. Follow the **budget-analyst** discipline: never
invent a number, print the `open_conflicts` `rendered` block verbatim, and never
restate raw payee text or account numbers (the tool redacts them on read).

## Read

Call `open_conflicts` to list the open conflicts. Print its `rendered` block
verbatim, then explain — per conflict — what the existing record and the incoming
record disagree on (amount, date), so the user can decide.

## Resolve (CLI handoff — no write tool)

Resolution happens at the command line, not through a tool. To resolve a conflict,
emit the exact command for the user to run in their terminal:

    budget reconcile <id> <action>

where `<id>` is the conflict's id and `<action>` is one of:

- `keep_one` — keep the existing record, drop the incoming duplicate.
- `mark_distinct` — they are genuinely different transactions; keep both.
- `merge` — merge the incoming record into the existing one.
- `accept_incoming` — replace the existing record with the incoming one.

There is no `resolve` subcommand — the command is `budget reconcile <id> <action>`
with one of the four actions above. Recommend an action, but let the user run it.
