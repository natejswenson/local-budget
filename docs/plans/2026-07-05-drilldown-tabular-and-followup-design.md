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
  `drill_hint` is a non-empty string AND `rows` is non-empty, append
  `\n\n_{drill_hint}_` after the table body. When `rows` is empty, `table()`
  suppresses the hint line unconditionally, regardless of what the caller
  passed for `drill_hint` — this is the single centralized rule (per this
  design's own rationale for putting drill affordance in `render.py`: a
  deterministic, testable property of the tool output, not something each
  call site has to remember to guard).
- `bars(items, *, width=20, numbered=False, drill_hint=None)` — same
  trailing-line behavior, appended after the bar lines, with the same
  empty-suppression rule: `bars()` keeps its existing "empty `items` → `""`"
  contract unconditionally, so `drill_hint` is never appended when `items`
  is empty — consistent with `table()`'s rule above.
- `drill_hint` is independent of `numbered`: a caller can pass
  `numbered=True, drill_hint=None` for a numbered-but-terminal list (none
  exist today, but the two are orthogonal on purpose so this doesn't need
  revisiting later).

### `tools.py` call sites (5 tools / 6 call sites)

| Tool | Change |
|---|---|
| `get_month_summary` | `bars()` → `table()`, cols `Category, Spent, %`, `numbered=True`; add `drill_hint="Reply with a row number to see that category's transactions."`. The `Spent` column value is `render.money(category_spend_cents)` — signed, matching the existing `get_category_breakdown` precedent (`render.money(int(r["spent"]))`), not `abs()`'d. |
| `top_merchants` | `bars()` → `table()`, cols `Merchant, Spent, %, #` (the `n` count is already selected by the SQL, currently discarded; the `%` column is new — see below), `numbered=True`; add `drill_hint="Reply with a row number to see that merchant's transactions."`; the empty-state check must become an explicit `if not rows: rendered = "(no spend)"` / `else: rendered = render.table(...)` — **not** the current `render.bars(...) or "(no spend)"` pattern. `bars()` returns `""` on empty `items`, which is why `or "(no spend)"` works today; `table()` has the opposite contract (empty `rows` → a truthy header-only table), so the `or` fallback would become dead code once converted and the "(no spend)" message would never show |
| `get_category_breakdown` | unchanged table; add `drill_hint="Reply with a row number to drill into that category's transaction list."` |
| `recurring_charges` | unchanged table; add `drill_hint="Reply with a row number to see that merchant's transactions."` |
| `review_queue` — "Uncategorized merchants" table | unchanged table; add `drill_hint="Reply with a row number to categorize that merchant."` (drills to `set_merchant_category`) |
| `review_queue` — "Checks to review" table | unchanged table; add `drill_hint="Reply with a row number to categorize that transaction."` (drills to `set_txn_category`) |

`get_month_summary` and `get_category_breakdown` both render numbered
category lists and commonly appear together in the same turn (e.g.
`budget-monthly-brief`, `budget-coach`), so their `drill_hint` strings are
deliberately worded differently rather than reusing identical text: one
references what's distinctive about `get_month_summary`'s "Where it goes"
overview ("...see that category's transactions."), the other what's
distinctive about `get_category_breakdown`'s by-category, transaction-count
view ("...drill into that category's transaction list."). This mirrors the
existing differentiation between `review_queue`'s two hints ("categorize
that merchant" vs. "categorize that transaction") and exists specifically
so that when both lists are shown in the same turn, the hint text itself
doesn't leave a user (or the model) unsure which list a bare row number
targets — reducing false "Ambiguous" triggers (per rule 6's Ambiguous
bullet below) even though a bare number is always unambiguous by its
position within any ONE list.

Both converted call sites — `get_month_summary` and `top_merchants` — pass
`table(..., numbered=True, ...)`, the same as the three call sites that
already render a `Row` column (`get_category_breakdown`, `recurring_charges`,
`review_queue`). This is load-bearing, not incidental: per Context item 1,
the entire point of this conversion is that these two stop looking visually
distinct from the other three — omitting `numbered=True` would convert the
bar chart to a table shape while still leaving out the `Row` column that
makes the list numbered and drillable in the first place.

`get_month_summary`'s new `%` column has no existing source value to surface
(unlike `top_merchants`'s `#`, which the SQL already selects and just needs
wiring through): it must match what `render.bars()` actually computes
internally today, which is an ABSOLUTE-VALUE sum — `total = sum(abs(v) for
_, v in items) or 1` — NOT the handler's existing `spend_total` (`spend_total
= sum(spend.values())`, a SIGNED/raw sum used for the headline "Spent"
figure). These two totals only coincide when every category's spend nets
non-negative; they diverge whenever a category nets negative (an offsetting
debit/refund), which this design's own rationale already establishes as a
real case — reusing `spend_total` directly for the `%` column is wrong for
exactly that reason, not just when it happens to hit zero. Concrete failure
if `spend_total` were reused directly: `spend = {"Groceries": 5000,
"RefundCat": -1000}` → `spend_total = 4000`, but `bars()`'s actual total is
`6000` → Groceries would render as `round(5000/4000*100) = 125%` instead of
the correct `round(5000/6000*100) = 83%`.

The fix is a NEW, separate denominator computed alongside `spend_total`, not
a reuse of it: `pct_total = sum(abs(v) for v in spend.values()) or 1`, then
per category `pct = round(abs(category_spend_cents) / pct_total * 100)`,
formatted as a string with a trailing `%` (e.g. `"23%"`). `spend_total`
itself is untouched and still used for the headline `Spent` figure — only
the new `%` column needs `pct_total`. The `or 1` guard on `pct_total` is
load-bearing, not defensive filler, for the same reason `bars()`'s own `or 1`
guard is — but the failure scenario is narrower than the cross-category
offsetting case above: because `pct_total` sums `abs(v)`, offsetting
entries (opposite signs, same magnitude) don't cancel each other out in an
absolute-value sum, so cross-category offsetting alone can't zero it. The
only way `pct_total` is actually `0` is if `spend` is a non-empty dict
whose EVERY individual category value is exactly `0` (each category nets
to zero on its own, not merely against another category) — an edge case,
but `if spend:` being true doesn't rule it out, so dropping the guard would
still reintroduce a division-by-zero that `bars()` has never had.

`get_month_summary`'s existing `if spend: lines += [...]` guard is retained
unchanged (minimal-diff): when there is zero spend for the month, no "Where
it goes" header, table, or `drill_hint` is rendered at all — not even a
header-only table. This is a THIRD, different empty-state mechanism from the
other two call sites below: `get_category_breakdown`/`recurring_charges`
call `table()` unconditionally and fall back to a header-only table (via
`table()`'s own empty-row suppression), while `review_queue` substitutes the
literal string `"(none)"`. This divergence is pre-existing behavior, not
introduced by this design, and is out of scope to change here.

`top_merchants` gets the same `%` column treatment as `get_month_summary`,
for the same reason: `render.bars()` already computes one today
(`pct = round(abs(v) / total * 100)` with `total = sum(abs(v) for _, v in
items) or 1`), and silently dropping it in the table conversion would be an
unexplained regression. In `top_merchants`'s own variable names, the SQL
rows in `rows` each carry a `spent` field (`SUM(-amount_cents)`, already
positive for every row that reaches this list): `total = sum(abs(int(r["spent"]))
for r in rows) or 1`, then per merchant `pct = round(abs(int(r["spent"])) /
total * 100)`, formatted as a string with a trailing `%` (e.g. `"23%"`) —
computed over the same `rows` list that's about to be rendered. The `or 1`
guard is carried over for the same defensive reason as `get_month_summary`'s
`pct_total` guard, even though it's unreachable in practice here: an empty
`rows` list is already routed to the `"(no spend)"` empty-state branch
(see the `top_merchants` row in the Components table above) before the
table — and therefore this `%` column — is ever built, so `total` is only
ever computed over a non-empty `rows` list. This is a genuinely separate
computation from `get_month_summary`'s `pct_total`/`spend_total` split
above: `top_merchants` has no signed-vs-absolute ambiguity to begin with,
since its SQL already filters to `amount_cents < 0` and negates, so every
`spent` value reaching this list is positive already — `abs()` here is
belt-and-suspenders parity with `bars()`'s own formula, not a fix for a
real sign divergence.

`review_queue`'s two tables are an "uncategorized merchants" list (drills to
`set_merchant_category`) and a "checks to review" / uncategorized-transactions
list (drills to `set_txn_category`) — there is no "category conflict" concept
in this tool; that phrase belongs to the separate, unrelated `open_conflicts`
tool and does not apply here.

Note: when either list is empty, `review_queue` substitutes the literal
string `"(none)"` instead of calling `table()`, so `drill_hint` never renders
in that state — this is expected and desired (there's nothing to drill into
when the list is empty), not a gap to close. This is a separate,
pre-existing mechanism from `table()`'s own empty-row suppression (see
`render.py` Components above): `review_queue`'s ternary never calls
`table()` at all when a list is empty, whereas `get_category_breakdown` and
`recurring_charges` call `table()` unconditionally and rely on `table()`
itself to suppress the hint line when `rows` is empty — same end result
(no hint on an empty list), different mechanism, both scoped to
`render.py`/`tools.py` as intended.

`query_transactions`'s `_txn_table()` stays un-numbered (it is the terminal
node — nothing drills further from a transaction row).

### `budget-analyst/SKILL.md` — rule 6, two new bullets

This design adds two new bullets to rule 6, both inserted after the
existing "Ambiguous" bullet (which stays exactly where it is —
"Ambiguous" reads as the intended catch-all closer for rule 6's existing
bullets in both this doc and the prior design doc, so nothing is inserted
between "Going back" and "Ambiguous"). The final bullet order for the
whole rule 6 list, after both additions, is: Terminal list, Tool exists
but isn't yours, Batch references, Invalidation, Going back, Ambiguous,
**Out-of-range reference** (new), **Always continue after a read-only
navigation action** (new, final bullet).

The out-of-range clause comes first of the two additions, since it belongs
with the other reference-resolution bullets (Terminal list, Tool exists
but isn't yours, Ambiguous) rather than with the follow-up-loop bullet:

> **Out-of-range reference:** if a row reference doesn't correspond to any
> shown row (e.g. "7" when the list only has 5 rows), say so plainly (e.g.
> "there's no row 7 — the list only has 5 rows") and re-print the list —
> don't guess or fabricate a row. Re-printing here reuses whichever render
> of the list is CURRENTLY FRESH, the same "reuse whichever render is
> fresh, no second tool call" principle that applies to case (a)/(b) below
> (see the fresh-render paragraph under the follow-up bullet): an
> out-of-range reference doesn't itself invalidate anything — no tool was
> successfully called for it — so no new tool call is needed just to
> re-print the list. This composes with the follow-up bullet below (case
> (c) there): the re-printed list still closes with the same explicit
> follow-up question as any other read-only navigation action.

There is no stated precedence between this bullet and Terminal list for a
reference that is simultaneously out-of-range and targets a terminal list —
but that overlap is currently unreachable, since the only terminal,
un-numbered list in this design's scope is `query_transactions`, which has
no rows to reference in the first place. No precedence rule is needed
today; revisit this if a numbered terminal list is ever added later.

The follow-up bullet goes last, as the new final bullet in rule 6:

> **Always continue after a read-only navigation action:** this rule
> covers three cases, all read-only: (a) a row reference that resolves to a
> READ tool (`query_transactions` or another read tool) — i.e. showing the
> detail behind a row; (b) a successful "Going back" navigation (per
> the Going-back bullet above), which re-prints the parent list; and (c) a
> row reference that doesn't correspond to any shown row (out-of-range, per
> the Out-of-range reference bullet above), which also re-prints that same
> list. Once that read action is fully answered — the assistant has
> printed the read result (the drill-down's rendered detail for case (a),
> the re-printed list for case (b), or the re-printed list plus the
> "no such row" message for case (c)) with no outstanding question of its
> own pending — the response closes with an explicit follow-up question —
> e.g. "Want to look at another category, or ask about a specific
> transaction?" For case (a), closing requires re-printing the parent
> list's `rendered` block (in addition to the drill-down detail already
> shown) before asking the question. For case (b), the list Going-back
> already re-printed IS that parent list, so no second re-print is
> needed — only the follow-up question is added on top of what Going-back
> already produced. For case (c), the list the Out-of-range bullet already
> re-printed IS that same list, so likewise no second re-print is needed —
> only the follow-up question is added on top of what Out-of-range already
> produced. A read-only navigation response never ends without
> this closing question. If the turn shows more than one numbered list
> (e.g. `budget-monthly-brief`, `budget-setup` render 2-3 lists in one
> turn), "the parent list" here means only the single list the row
> reference or back-navigation resolved to — not every list shown that
> turn.
>
> General composition rule for colliding pending questions: if this
> closing question would otherwise collide with an existing,
> still-unanswered question from the skill itself (e.g.
> `budget-monthly-brief` always ends its turn with a required "offer to
> save the brief" question pending an explicit yes before `save_brief`),
> the assistant folds both into a single combined closing message rather
> than asking two separate questions or dropping either — e.g. "Want to
> look at another category, or should I save this brief?" This is a
> general rule, not specific to `budget-monthly-brief`: it applies to any
> skill that closes a turn with its own pending question.
>
> The re-print step — case (a)'s closing re-print, and case (c)'s re-print
> of the list per the Out-of-range reference bullet above — reuses
> whichever render of the parent list is CURRENTLY FRESH at the point that
> re-print is needed; it does not mandate a second, independent re-call on
> top of the existing Invalidation bullet. Case (c) is explicitly included
> here, not just case (a): an out-of-range reference doesn't itself
> invalidate anything, since no tool was successfully called for it, so
> its re-print reuses the same already-fresh render already in context,
> exactly like case (a) — no new tool call is needed just because the
> reference was out of range. If Invalidation already forced a
> re-call/re-print of this list earlier in the SAME turn (before the row
> reference or back-navigation was even resolved) and nothing has changed
> since, that already-fresh render is what gets shown again here — no
> second tool call. A new re-call is only needed if anything invalidates
> the list after the point it was last freshly rendered in this turn —
> e.g. some OTHER write fires in between the read action and this
> follow-up close (rare, but possible: the read action itself is by
> definition a read under this rule, since a write-resolving drill-down
> goes through rule 4 instead, per the paragraph below). Put plainly: the
> follow-up loop never requires more than one fresh render of the parent
> list per turn.
>
> This does NOT change the existing write-confirmation flow (rule 4) for
> categorization actions in `review_queue`/batch references. A write still
> just gets its normal confirmation exchange and outcome report — "fully
> answered" for a write means the confirmation was obtained and the outcome
> was reported, full stop. It does not require re-printing the list and
> asking "what's next" after every single write; doing that on top of batch
> categorization would make it intolerably chatty.

This composes with the existing Invalidation bullet without ever doubling
the re-print: since this rule only applies to read-only navigation actions
(read drill-downs or a successful Going-back), whatever render is already
fresh (re-called by Invalidation earlier this turn, produced by Going-back
itself, or unchanged from when the list was first shown) is what gets
reprinted once; only some other write that happens to fire in between the
read action and the follow-up close forces a genuinely new re-call at this
point.

This design also updates rule 6's preamble sentence, which today (lines
35-36 of `budget-analyst/SKILL.md`) reads "Some tools render a numbered list (a
`Row` column, or a `bars()` line prefixed `N. `)" — describing two current
forms. Once `get_month_summary` and `top_merchants` (the only two
`bars()`-based call sites, per the `tools.py` table above) convert to
`table()`, zero tools render a numbered `bars()` line anymore, so the "or
a `bars()` line prefixed `N. `" clause is vestigial and must be dropped:
the preamble should simply say "a `Row` column" (or equivalent wording),
no longer describing two forms — consistent with this design's own goal
that every numbered list is now a table with a `Row` column.

## Data flow (before / after)

**Before:** user picks row 1 of `get_month_summary`'s bar list → assistant
shows transactions → response ends.

**After:** user sees a table with a trailing hint line → picks row 1 →
assistant shows transactions → assistant re-prints the category table →
assistant asks "want to look at another category, or ask about a specific
transaction?"

## Error handling

`drill_hint=None` (the default) preserves today's
output for any call site not yet updated, so this is backward compatible
call-by-call. The follow-up loop only fires after a drill-down is "fully
answered" — if the row reference itself can't be resolved (ambiguous match,
tool exists but isn't the current skill's, no drill-down tool at all), the
existing rule 6 resolution-failure paths (Terminal list, Tool exists but
isn't yours, Ambiguous) apply unchanged and the follow-up loop does not add
a redundant re-prompt on top of an error message. Batch references and
Invalidation are composition rules — they govern how a row reference
resolves and how the parent list gets re-called/re-printed, respectively —
not failure/exemption cases, so they aren't in that list; see the
Invalidation-composition note above.

An out-of-range row reference (e.g. the user says "7" when the shown list
only has 5 rows) is not covered by any existing rule 6 path in
`budget-analyst/SKILL.md` today: Terminal list means no drill-down tool
exists at all, Tool-exists-but-isn't-yours means a routing issue, and
Ambiguous means multiple plausible matches — none of them is "the number
doesn't correspond to any row," so none of them actually covers this case.
This is not a newly discovered gap, though: the prior design doc
(`2026-07-05-conversational-numbered-drilldown-design.md`, Error Handling)
already specified this exact scenario ("Stale/out-of-range reference... say
so, don't fabricate a row"), but the shipped `budget-analyst/SKILL.md` rule 6
never got that bullet as part of commit `6eecf4c`'s implementation. This
design re-surfaces and closes that gap: the assistant says so plainly (e.g.
"there's no row 7 — the list only has 5 rows") and re-prints the list again
with the same follow-up-question closing, rather than silently guessing or
failing. This is a minimal addition alongside the new follow-up bullet,
not a rewrite of rule 6 itself — see the Components section above for the
exact new "Out-of-range reference" bullet text and its placement
(immediately after Ambiguous, before the follow-up bullet).

Going back is neither of those: it's a normal, successful, read-only
navigation action, not an error, and it IS in scope for the new follow-up
bullet — specifically case (b) in that bullet's blockquote above — not
exempt from it. After a successful "back" re-prints the parent list
per the existing Going-back behavior, the response still
closes with the same kind of explicit follow-up question as any other
read-only navigation action. (If there's no parent to return to, Going
back's existing "say so plainly" behavior is unchanged — there's no list
left to attach a follow-up question to.)

A drill-down tool call that *succeeds* but returns zero rows (e.g. a
category with no transactions this month) is NOT one of these error cases —
it's a normal, successful read result with an empty table. The follow-up
loop fires exactly as it would for a non-empty result: print the (empty)
drill-down result, re-print the parent list, and ask what's next.

## Testing strategy

- `tests/test_render.py`: new unit tests for `drill_hint` on both `table()`
  and `bars()` (present / absent / empty string), plus a case for each
  covering an empty `rows`/`items` list with a non-null `drill_hint` (must
  render with no hint line at all, per the empty-suppression rule above),
  and updated existing assertions for any table golden-output that changes
  shape.
- `tests/test_agent_tools.py`: add golden-output assertions for
  `get_month_summary` and `top_merchants` (bars → table shape) and for the
  `drill_hint` line's presence at all 6 call sites (5 tools;
  `review_queue` contributes two) — this file has no existing bars()/
  table()-shape assertions to update, only new ones to add. Also add a
  dedicated `get_month_summary` case guarding the `pct_total` fix above: a
  month where one category nets negative (e.g. an offsetting debit/refund)
  alongside a positive category — assert the specific `%` values in
  `rendered` are computed against the absolute-value `pct_total` (matching
  `bars()`'s real math), not against the signed `spend_total`. E.g. for
  `spend = {"Groceries": 5000, "RefundCat": -1000}`, assert Groceries shows
  `83%` (`round(5000/6000*100)`), not the `125%`
  (`round(5000/4000*100)`) that reusing `spend_total` would produce — i.e.
  assert the exact percentages, not just that a `%` column exists. Also add
  a dedicated `top_merchants` regression case guarding the empty-state
  rewrite in Components above: a month with zero matching rows must render
  the tool's returned `result["rendered"]` as exactly the full wrapped
  string `f"## Top merchants — {month}\n(no spend)"` — not a truthy
  header-only table, and not the bare substring `"(no spend)"` on its own.
  (Per `tools.py`, `result["rendered"]` is always built as `f"## Top
  merchants — {month}\n{rendered}"`, where the local `rendered` value —
  the bars/table output or `"(no spend)"` — is never returned bare; the
  test must assert against that full wrapped string, not the unwrapped
  local value.) This guards against the `bars()`-vs-`table()` empty-row
  truthiness flip (`bars()` returns `""` on empty `items`, so `or "(no
  spend)"` used to work; `table()` returns a truthy header-only table on
  empty `rows`, so the same `or` pattern would silently stop showing "(no
  spend)" if the `if not rows: ... else: ...` rewrite were ever reverted or
  bypassed).
- `tests/test_mcp_server.py`: two existing tests hard-assert the old
  `bars()`-only format for these same two call sites and WILL break once
  they render as tables — both must be rewritten, not just left passing by
  accident:
  - `test_top_merchants_data_carries_resolved_month` asserts `"Row" not in result["rendered"]` with the comment "bars() has no header row at all". Once `top_merchants` renders as a `table()`, a `Row` header is expected (numbered tables always add one) — this assertion must be flipped to assert the `Row` header IS present (or moved to a dedicated header-shape test, mirroring `test_get_category_breakdown_row_column_does_not_collide_with_count_column`'s pattern).
  - `test_get_month_summary_dict_order_matches_numbered_bars_order` parses `bars()`'s line syntax directly (`ln[0].isdigit()`, then `ln.split(". ", 1)[1].split("  ")[0]`) to check that `data["spend_by_category"]`'s dict-insertion order matches the rendered order. This must be rewritten to re-derive the order check against the markdown table's rows/cells instead (e.g. split `rendered` on `\n`, filter to lines starting with `"| "`, then drop the first two matches — the header line and the separator line, both of which also start with `"| "` and would otherwise be mis-parsed as data rows — before splitting each remaining row on `" | "` and reading the `Category` cell) — the underlying invariant (row N of the render == Nth entry of `spend_by_category`) is unchanged, only the parsing of `rendered` needs to change from bars-line syntax to table-row syntax. Note that `rendered` now has trailing content after the table (the `drill_hint` line and `_flag_lines` output) in the same string, unlike the old bars()-only output where `ln[0].isdigit()` naturally excluded everything but bar lines — the rewritten parser must explicitly filter to lines starting with `"| "` (or equivalent) to isolate table rows, then drop the header and separator lines as described above, before reading cells. Two more details the parser must account for: (1) a naive split of a row string like `"| 1 | Groceries | $50.00 | 17% |"` on `" | "` leaves a stray leading `"| "` fragment on the first cell and a stray trailing `" |"` fragment on the last cell, so strip the row's leading `"| "` and trailing `" |"` before splitting on `" | "` (or use a regex/split approach that discards the outer pipes); (2) because of the prepended `Row` column, the `Category` cell is now at index `1` in the resulting list, not index `0` as it would be without a `Row` column.
- The follow-up loop (rule 6 addition) is prose/persona, not code — no unit
  test possible. Automated eval-corpus coverage for it is NOT currently
  possible: `EvalSpec` (`tests/evals/specs.py`) has only a `prompt: str`
  field, with no multi-turn/session-resume mechanism, and the prior design
  doc's own "Skill eval reality check" section already settled this — a
  genuinely multi-turn scenario (a list shown in one turn, referenced in a
  later turn) can't be authored today at all. This is a manual verification
  item during implementation review, full stop, not an automated gate.

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
- Every one of the 6 call sites (5 tools; `review_queue` contributes two)
  in `tools.py` passes a `drill_hint` (or explicitly omits it with a
  one-line reason if a list is numbered-but-terminal — none exist today).
- `get_month_summary` and `top_merchants` no longer call `render.bars()`.
- `drill_hint=None` produces byte-identical output to today's (no trailing
  line) for any call site that doesn't opt in.

**Testable (requires running tests / manual check):**
- `table()`/`bars()` with a non-null `drill_hint` and non-empty
  `rows`/`items` append exactly one trailing italic line, no extra blank
  lines beyond one separator. With empty `rows`/`items`, no hint line is
  appended regardless of what `drill_hint` was passed — suppression lives
  in `render.py` itself, not in each call site.
- After any drill-down, the very next assistant turn contains both a
  re-rendered parent list and an explicit follow-up question (manual
  conversational check — not unit-testable).
