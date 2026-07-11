---
name: budget-setup
description: First-run onboarding — load data via the CLI, then hand off to categorize and budgets.
tools: [get_month_summary, review_queue, query_transactions, list_categories]
---

# Budget Setup

First-run orchestration. Follow the **budget-analyst** discipline: never invent a
number, print each tool's `rendered` block verbatim, and confirm before any write.
This skill performs no writes of its own — it loads data and hands off.

## Step 1 — Load the data (CLI, not a tool)

Data import happens at the command line, not through a tool. For a true first
run, tell the user to start with:

- `budget setup` — initializes the database and asks for their name (reports
  and briefs render with it; skipping this just means unnamed reports).

Then load transactions with one of:

- `budget import <file>` to load a specific statement file (CSV/OFX), or
- `budget intake` to pull in everything staged for import.

These are CLI commands. Do not try to call them as tools — direct the user to run
them, then continue once data is loaded.

## Step 2 — Confirm data landed

Call `get_month_summary` to confirm transactions loaded, then call `review_queue`
to show what still needs attention. Print each tool's `rendered` block verbatim and
add a short note on what the user is looking at.

## Step 3 — Hand off

Once data is in, point the user to the next skills:

- Run `/budget-categorize` to work the review queue and clean up categories.
- Run `/budget-budgets` to set spending limits and expected income.
