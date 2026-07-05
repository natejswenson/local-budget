---
ticket: "#TBD"
title: "Conversational Numbered Drill-Down for Budget Reports"
date: "2026-07-05"
source: "design"
---

# Conversational Numbered Drill-Down

## Problem

Money conversations in this app currently require scrolling back to re-read a
category name before asking about it ("break down what's in Home Improvement").
The user wants to reply with a bare reference to a row in the last report
("2", "#2", "the second one") and have the agent expand it in place, chained
across multiple levels (category → its transactions → back to categories),
without re-typing labels.

## Decisions

1. **Numbering lives server-side, not in agent improvisation.** The whole
   system is built around one hard rule (`budget-analyst` persona rule #2):
   "print the tool's `rendered` block verbatim... clean & beautiful is a
   regression-guarded property, not a model whim." Numbering therefore belongs
   in `render.py`'s renderers, not as something the agent freehandly prepends
   when it feels like it — otherwise it's inconsistent and violates the
   verbatim-print discipline the app is built on.
2. **A bare/flexible reference always means "row N of the most recently
   rendered numbered list."** No expiry, no re-anchoring ritual. If more than
   one numbered list is in play and it's ambiguous which one is meant, the
   agent asks instead of guessing.
3. **Drill-down is chained/multi-level** (category → transactions → back),
   not a single hop. "Going back" needs no new code — it's re-calling the same
   tool with the same args and re-printing its `rendered` block, never
   recalled from memory (this preserves persona rule #1: never invent a
   number).
4. **Level 2 is uniformly `query_transactions(category=X)`** — it exists
   today and works for every category. Categories with subcategories (e.g.
   Subscriptions) get an optional extra level via `subcategory_breakdown`,
   but that's a bonus, not the required path.
5. **Bonus use case:** the same mechanism numbers `review_queue`'s merchant
   rows, so categorization can happen by row number ("1 → Dining Out") instead
   of retyping merchant strings — directly motivated by the manual
   categorization work done earlier in this session.

## Architecture

One small render-layer capability, one data invariant, one persona rule — not
a new subsystem, no schema/data-model change, no new MCP tools.

### 1. Render layer (`src/local_budget/agent/render.py`)

Add an optional `numbered: bool = False` parameter to `table()` and `bars()`:

- `table(rows, cols, *, numbered=False)` — when `True`, prepend a `#` column
  (1-indexed) as the first column of the rendered markdown table.
- `bars(items, *, numbered=False)` — when `True`, prefix each line with
  `N. ` before the label.

Default `False` everywhere — zero behavior change for any existing caller
until a call site explicitly opts in.

### 2. Data invariant (no new fields)

For any tool call site that renders with `numbered=True`, row N of the
`rendered` block corresponds to `data.<list>[N-1]` of that same tool
response — true by construction (both are built from the same underlying
row list in the same order today). This is documented as a hard invariant,
not implemented as new schema.

### 3. Persona rule (new — `budget-analyst` rule #6)

> When you print a numbered list, a follow-up reply that references a row —
> a bare number, "#2", "the second one", or a phrase matching a shown row's
> label — means: look up that row from the last numbered list's `data`, and
> call the matching drill-down tool for it. A back-reference ("go back",
> "show that again") means: re-call the same tool that produced the parent
> list and re-print its `rendered` block verbatim — never reconstruct it from
> memory. If more than one numbered list is in play and it isn't clear which
> is meant, ask.

This rule is added to `.claude/skills/budget-analyst/SKILL.md` alongside the
existing five rules; individual `budget-*` skills don't need per-skill
changes beyond referencing the persona as they already do.

### 4. Rollout scope (v1) — tools that opt into `numbered=True`

| Tool | Numbered list | Drill-down target |
|---|---|---|
| `get_month_summary` (bars section) | category rows | `query_transactions(category=X)` |
| `get_category_breakdown` | category rows | `query_transactions(category=X)` |
| `query_transactions` | transaction rows | terminal (no further drill; category context still supports "recategorize row N" → `set_txn_category`) |
| `review_queue` | uncategorized merchant rows | `set_merchant_category` (by row reference instead of retyped merchant string) |
| `top_merchants` | merchant rows | `query_transactions(merchant=X)` |
| `recurring_charges` | recurring merchant rows | `query_transactions(merchant=X)` |

`subcategory_breakdown` is left un-numbered in v1 (optional future extension
for categories with subcategories); nothing else in the tool surface changes.

## Data Flow (example)

1. User: "spending report for June" → `get_category_breakdown(month="2026-06")`,
   numbered rows 1–15.
2. User: "2" → agent resolves row 2 from the last numbered list's `data` →
   `query_transactions(category="Large Purchases", days=...)` → numbered
   transaction rows (terminal level; no further numbering needed since these
   are individual transactions).
3. User: "back" → agent re-calls `get_category_breakdown(month="2026-06")`
   (the parent list's exact original call) and re-prints it verbatim.
4. User: "1" → row 1 this time (Home Improvement) → drills again.

## Error Handling

- **Ambiguous reference** (two numbered lists shown close together, unclear
  which "2" means): ask, don't guess.
- **Stale/out-of-range reference** ("12" when the last list only had 8 rows):
  say so, don't fabricate a row.
- **Non-numbered-list bare number** (e.g. user says "50" meaning a dollar
  amount, not a row): if no numbered list has been shown recently, don't
  apply this rule at all — treat literally.

## Testing Strategy

- **Unit** (`tests/test_render.py` or wherever render tests live): assert
  `table(rows, cols, numbered=True)` prepends 1-indexed rows correctly;
  assert `numbered=False` (default) output is byte-identical to current
  behavior — regression guard for the "zero blast radius on existing callers"
  claim.
- **Skill eval** (mock tier, `scripts/eval.py`): one new deterministic
  transcript exercising the chain "show June categories" → "2" → "back" →
  "1", asserting the agent calls the correct tool with the correct args at
  each step and never fabricates a number between tool calls.

## API Surface

```python
# src/local_budget/agent/render.py
def table(rows: list[dict], cols: list[tuple[str, str]], *, numbered: bool = False) -> str: ...
def bars(items: list[tuple[str, int]], *, width: int = 20, numbered: bool = False) -> str: ...
```

No MCP tool schema changes — `numbered=True` is applied at existing call
sites in `agent/tools.py`; no new tools, no new `data` fields.

## Invariants

**Checkable by inspection:**
- `numbered` defaults to `False` on both renderers; no existing call site's
  behavior changes unless explicitly updated to pass `numbered=True`.
- Numbering never alters a dollar figure, row order, or underlying `data`
  content — purely an added index.

**Testable:**
- Rendered row N ⇔ `data[N-1]` for every tool listed in the v1 rollout scope.
- The eval transcript chain (category → drill → back → drill) resolves each
  step to the correct tool call without the agent inventing a figure between
  tool calls (persona rule #1 held under the new rule #6).
