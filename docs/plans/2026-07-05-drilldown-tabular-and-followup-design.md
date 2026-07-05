---
ticket: "N/A"
title: "Conversational drill-down: tabular rendering + post-drill-down follow-up loop"
date: "2026-07-05"
source: "design"
---

# Conversational drill-down: tabular rendering + post-drill-down follow-up loop

## Context

Commit `6eecf4c` (design doc `2026-07-05-conversational-numbered-drilldown-design.md`)
added a numbered-row drill-down convention: several MCP tools render a `Row`
column (or a `bars()` line prefixed `N. `), and `budget-analyst` rule 6 defines
how a follow-up message referencing a row resolves to a drill-down tool call.

Using the feature surfaced two gaps:

1. Two of the five numbered-list tools (`get_month_summary`'s "Where it goes",
   `top_merchants`) still render as ASCII bar charts (`render.bars()`)
   instead of GFM tables (`render.table()`), so they look visually distinct
   from the other three (`get_category_breakdown`, `recurring_charges`,
   `review_queue`), which already render as tables with a `Row` column.
2. Nothing tells the user that typing a number does anything, and nothing
   requires the assistant to keep the conversation going after a drill-down —
   in practice a drill-down response can just... end, with no indication
   there's more to explore.

This design closes both gaps and is a refinement of the existing convention,
not a new one — it reuses `render.py`'s existing `table()`/`bars()`
functions and `budget-analyst` rule 6's existing structure.

## Architecture

Three independent changes, each at the layer that already owns that kind of
behavior in this codebase:

1. **Rendering (render.py + tools.py):** convert the two remaining
   `bars()` call sites to `table()`, so every numbered list in the system is
   a table. Layer: MCP tool surface — same layer that already renders the
   other three numbered lists.
2. **Drill affordance (render.py + tools.py):** add an opt-in `drill_hint`
   param to `table()`/`bars()` that appends a trailing hint line. Layer:
   same rendering layer — centralizing this here (rather than in skill
   prose) makes it a deterministic, testable property of the tool output
   instead of something the model has to remember to say. This mirrors
   `render.py`'s own stated design intent ("clean & beautiful" as a
   regression-guarded property, not a model whim).
3. **Follow-up loop (budget-analyst/SKILL.md rule 6):** add a new sub-rule
   requiring that a drill-down response always ends by re-showing the
   parent list and asking an explicit follow-up question. Layer: shared
   persona — the only layer that spans multiple tool calls/turns, and
   already owns every other cross-cutting row-reference behavior (batch
   identity, invalidation, back-navigation).

No new tools, no new MCP surface, no schema changes.

## Components

### `render.py`

- `table(rows, cols, *, numbered=False, drill_hint=None)` — when
  `drill_hint` is a non-empty string, append `\n\n_{drill_hint}_` after the
  table body.
- `bars(items, *, width=20, numbered=False, drill_hint=None)` — same
  trailing-line behavior, appended after the bar lines.
- `drill_hint` is independent of `numbered`: a caller can pass
  `numbered=True, drill_hint=None` for a numbered-but-terminal list (none
  exist today, but the two are orthogonal on purpose so this doesn't need
  revisiting later).

### `tools.py` call sites (5 numbered lists)

| Tool | Change |
|---|---|
| `get_month_summary` | `bars()` → `table()`, cols `Category, Spent, %`; add `drill_hint="Reply with a row number to see that category's transactions."` |
| `top_merchants` | `bars()` → `table()`, cols `Merchant, Spent, #` (the `n` count is already selected by the SQL, currently discarded); add `drill_hint="Reply with a row number to see that merchant's transactions."` |
| `get_category_breakdown` | unchanged table; add `drill_hint` |
| `recurring_charges` | unchanged table; add `drill_hint` |
| `review_queue` (both tables) | unchanged tables; add `drill_hint` per table (merchant vs. category conflict rows have different downstream tools, so different hint copy) |

`query_transactions`'s `_txn_table()` stays un-numbered (it is the terminal
node — nothing drills further from a transaction row).

### `budget-analyst/SKILL.md` — rule 6, new sub-rule

Appended after the existing "Going back" bullet:

> **Always continue after a drill-down:** once a drill-down response is
> fully answered, re-print the parent list's `rendered` block (verbatim if
> nothing invalidated it since; re-called per the Invalidation rule if a
> write happened) and close with an explicit question — e.g. "Want to look
> at another category, or ask about a specific transaction?" A drill-down
> response never ends on the detail alone.

This composes with the existing Invalidation bullet: if the drill-down
itself involved no write, the parent list is still valid and can be
reprinted verbatim without a re-call.

## Data flow (before / after)

**Before:** user picks row 1 of `get_month_summary`'s bar list → assistant
shows transactions → response ends.

**After:** user sees a table with a trailing hint line → picks row 1 →
assistant shows transactions → assistant re-prints the category table →
assistant asks "want to look at another category, or ask about a specific
transaction?"

## Error handling

No new failure modes. `drill_hint=None` (the default) preserves today's
output for any call site not yet updated, so this is backward compatible
call-by-call. The follow-up loop only fires after a drill-down is "fully
answered" — if the drill-down itself fails (bad row reference, ambiguous
match, no data), the existing rule 6 error paths apply unchanged and the
follow-up loop does not add a redundant re-prompt on top of an error message.

## Testing strategy

- `tests/test_render.py`: new unit tests for `drill_hint` on both `table()`
  and `bars()` (present / absent / empty string), and updated existing
  assertions for any table golden-output that changes shape.
- `tests/test_agent_tools.py`: update golden-output assertions for
  `get_month_summary` and `top_merchants` (bars → table shape) and for the
  `drill_hint` line's presence on all 5 numbered tools.
- The follow-up loop (rule 6 addition) is prose/persona, not code — no unit
  test possible. Covered by an eval-corpus case if `scripts/eval_gen_corpus.py`
  has scenario coverage for rule 6; otherwise this is a manual verification
  item during implementation review, not an automated gate.

## API Surface

```python
def table(
    rows: list[dict],
    cols: list[tuple[str, str]],
    *,
    numbered: bool = False,
    drill_hint: str | None = None,
) -> str: ...

def bars(
    items: list[tuple[str, int]],
    *,
    width: int = 20,
    numbered: bool = False,
    drill_hint: str | None = None,
) -> str: ...
```

No MCP tool schemas change (`data` payloads are untouched; only `rendered`
strings change shape).

## Invariants

**Checkable by inspection:**
- Every one of the 5 numbered-list call sites in `tools.py` passes a
  `drill_hint` (or explicitly omits it with a one-line reason if a list is
  numbered-but-terminal — none exist today).
- `get_month_summary` and `top_merchants` no longer call `render.bars()`.
- `drill_hint=None` produces byte-identical output to today's (no trailing
  line) for any call site that doesn't opt in.

**Testable (requires running tests / manual check):**
- `table()`/`bars()` with a non-null `drill_hint` append exactly one
  trailing italic line, no extra blank lines beyond one separator.
- After any drill-down, the very next assistant turn contains both a
  re-rendered parent list and an explicit follow-up question (manual
  conversational check — not unit-testable).
