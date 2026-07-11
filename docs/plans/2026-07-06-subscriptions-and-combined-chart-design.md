---
ticket: "N/A"
title: "Visual report v2: subscriptions-only recurring flags + combined spend/budget chart"
date: "2026-07-06"
source: "design"
---

# Visual report v2: subscriptions-only recurring flags + combined spend/budget chart

## Context

After the first round of fixes (`docs/plans/2026-07-06-visual-report-fixes-design.md`,
implemented in commit `48d4606`) was exercised on a real June 2026 report, two more
issues surfaced:

1. The month-scoped "recurring charges" section included merchants the user
   patronizes repeatedly in person (Circle K, Costco, a local pizza place) —
   not true subscriptions. The signal the user wants is "what am I being
   automatically billed for," not "what merchant do I visit most months."
2. "Where it goes" (category bar chart) and "Budget vs. actual" (per-category
   meters) are both per-category, one-row-per-category displays. Showing both
   back to back is redundant — the user asked to merge them into one chart
   that shows spent, budget, and % used together.

## Decisions

1. **Recurring flags narrow from "any spend-eligible category" to a
   bill-like allowlist.** `recurring_charges` has no category field, but the
   existing month cross-reference against `query_transactions` already
   returns each matched row's `category` for free. The match guard's
   category condition changes from `is_spend(category)` to exact
   membership in a fixed allowlist: `{"Subscriptions", "Utilities",
   "Insurance", "Housing", "NY529", "Sewer/Water/Trash"}`. Correction on the
   guard being replaced: `is_spend()` (`categories.py`) is not a curated
   list of 18 legitimate spend categories — it's a 3-item *blocklist*
   (Income, Transfer, Uncategorized) that returns True for every other
   category, including every custom category the user has ever created
   (Kid Activities, Large Purchases, Lawyer, Volleyball, Home Improvement,
   Sewer/Water/Trash, NY529, etc.). That miscounted mental model of the old
   guard — treating it as a fixed set of legitimate spend categories rather
   than "everything that isn't structural" — is exactly why
   Sewer/Water/Trash was nearly missed when narrowing to the new allowlist:
   it was already silently passing the old guard alongside every other
   custom category. The first four allowlist entries are this project's
   built-in categories that represent inherently auto-billed obligations
   (`categories.py`'s own comment on `Subscriptions` is literally
   "recurring software/services"). `NY529` and `Sewer/Water/Trash` are the
   user's own custom categories for recurring obligations (a 529
   contribution, and sanitation/water/trash utility bills, respectively) —
   named explicitly rather than inferred, since a custom category's meaning
   can't be derived generically; if the user adds further custom bill-like
   categories later, this allowlist needs a manual update (documented as a
   known maintenance cost, not solved generically). All other existing
   guards (exact `merchant_norm` match, `amount_cents < 0`, `UNKNOWN`
   exclusion, most-recent-within-month tie-break, month-scoped display
   values) are unchanged. Verified live against June 2026: this allowlist
   keeps Chewy.com, Verizon (Utilities), NEWYORK 529 ACH (NY529), and FUCHS
   SANITATION S (Sewer/Water/Trash — confirmed live as a stable
   ~$45-50/charge recurring bill, with a genuine June 2026-06-17 charge) —
   Netflix, Audible also qualify (Subscriptions) — and drops Circle K,
   Costco, TST Zorbaz Of, Just For Kix, Olo Pizza Maple, which were in the
   prior (broader) June result set purely because they recur monthly, not
   because they're bills. Anthropic/Claude does *not* survive June's
   cross-reference despite clearly belonging in Subscriptions:
   `recurring_charges`' aggregate key for it is `CLAUDE.AI SUBSCRIP
   ANTHROPIC.COM`, but June's actual rows are `ANTHROPIC CLAUDE
   ANTHROPIC.COM` (06-04, 06-12) and `PURCHASE ANTHROPIC C` (06-11) — none
   exact-match. This is the same `merchant_norm`-drift false-negative
   already documented for Hulu (and, coincidentally-surviving, Netflix) in
   the sibling addendum (`2026-07-06-visual-report-fixes-design.md`); this
   narrower allowlist doesn't change that pre-existing limitation — it's
   still live, and June 2026 shows it now also excludes Claude specifically.
   RED RIVER RURAL (Sewer/Water/Trash) is now within the allowlist's
   category scope, so it would appear in any month it actually charges, but
   has no June 2026 transaction at all (`recurring_charges` shows its
   `last_date` as 2026-05-12, May) — a timing non-issue, not a matching
   failure, distinct from the Claude/Hulu drift problem above.

   The section label changes from "recurring charges in \<reported month\>"
   to "subscriptions & recurring bills in \<reported month\>" to match the
   narrower, more accurate scope.

2. **"Where it goes" and "Budget vs. actual" merge into one bullet-style
   chart.** Per-category, one row each: a horizontal bar whose length is
   that category's dollars spent this month, a thin tick mark at the
   category's budget amount (when a budget is set), the bar/tick colored by
   the same three-tier status logic old Recipe 3 already used (`good` /
   `warning` / `critical`, keyed to `budget_overview`'s exact `over` boolean
   for critical), and trailing text `"$spent of $budget · pct%"` for
   budgeted categories or just `"$spent"` for unbudgeted ones. A category
   with no budget set at all (e.g. Large Purchases: $4,055.91 spent, no
   budget — `pct` is `None`, `over` is `False`) is treated as unbudgeted for
   coloring too: it gets the `good` tier color (there's no over-budget
   signal possible without a budget to compare against — this is not a new
   fourth visual tier, just the existing `good` color) and no tick mark,
   distinguished from a good-and-under-budget row only by the tick's
   absence (its trailing text is also just "$spent", per the unbudgeted-row
   rule above). Rows are sorted by dollars spent descending, preserving
   "where it goes"'s ranking function, with ties (e.g. multiple budgeted
   zero-spend categories tied at $0.00) broken by category name,
   alphabetically — the simplest deterministic rule, no new heuristic
   needed. For sorting purposes only, a negative-net-spend row is also
   treated as $0.00 (consistent with it being "treated identically to the
   exactly-$0.00 case throughout" — see below), so it sorts among the
   zero-spend rows and uses the same alphabetical tie-break, rather than
   sorting by its actual negative figure (e.g. Volleyball's -$180.00 row
   sorts as if it were $0.00, not below every $0.00 row). The row set is the union of every category whose *actual* spend
   this month is positive — nonzero AND positive is the actual inclusion
   criterion, not merely nonzero; a category whose net spend is NEGATIVE
   this month (its only transaction(s) net to a negative total — e.g.
   Volleyball's lone March 2026 transaction was a refund/credit with no
   offsetting charge, netting -$180.00) is treated the same as the exactly-
   $0.00 case below, not
   as a qualifying value just because it's nonzero (determined by the
   dollar figure itself — see "Where the row's numbers are extracted from"
   below for both cases, and why a `get_category_breakdown` row's mere
   presence is not sufficient either way) — and
   every category with a budget set (from
   `budget_overview`, excluding `budget_cents <= 0` exactly as
   before — though this exclusion is defensive/documentation-only, not a
   live risk today: three separate write paths (`budgets.py`'s
   `set_limit()`, `normalize.py`'s `_reconcile_subscription_budgets()`, and
   `categorize/manual.py`'s `_upsert_limit()`) each independently
   guarantee positivity — `set_limit()` rejects any non-positive limit at
   write time with `BudgetError("limit must be positive")`;
   `_reconcile_subscription_budgets()` only writes a survivor row `if survivor > 0:`;
   `_upsert_limit()` sums already-positive existing limits — so a live
   `budget_cents <= 0` row cannot actually appear in `budget_overview`'s
   output today, though a future change to any one of these three paths
   without updating the others could introduce a non-positive row this
   design doesn't currently guard against at the rendering layer) — a
   category with a budget but
   zero June spend still gets a row (empty bar, visible tick, "\$0.00 of
   \$X · 0%"), since "you haven't touched this budget yet" is meaningful
   information the old two-panel design also surfaced (via the meter
   existing regardless of spend). If the resulting row set is empty (no
   category has `spent > 0` this month and no category has a budget set),
   render a placeholder line — e.g. "no spending or budgets to show" —
   instead of an empty section, mirroring old Recipe 3's existing "no
   budgets set" placeholder rule.

   Note: "no category has `spent > 0`" above means actual positive spend
   (nonzero AND positive — a negative net spend does not satisfy `spent >
   0` either), not "no category has a `get_category_breakdown` row" — see
   below.

   **Where the row's numbers are extracted from.**
   `get_category_breakdown`'s SQL is a `GROUP BY category` over actual
   transactions with no `HAVING spent > 0` filter and no zero-fill: it
   omits a category only when it has zero *transactions* that month, not
   zero *net spend*. Three cases — two zero-dollar, one negative — follow
   from this. A budgeted category with zero spend this month (e.g.
   Housing: $0 spent, $8,234 budgeted) produces no `get_category_breakdown`
   row at all. An unbudgeted (or budgeted) category with a charge fully
   offset by an equal same-month refund/credit *does* still produce a
   `get_category_breakdown` row, but that row's own Spent value reads
   exactly $0.00 — a row existing is not proof of positive spend. The
   third case is a category whose only transaction(s) that month net to a
   negative total (e.g. a lone refund/credit with no offsetting charge, or
   multiple transactions where refunds happen to outweigh charges),
   netting to a NEGATIVE Spent value — verified live: Volleyball's lone
   March 2026 transaction was a refund/credit with no offsetting charge,
   netting -$180.00. A negative value is technically nonzero, but a
   negative bar length / scale position is undefined, so this design
   treats a negative net Spent value identically to the exactly-$0.00
   case throughout: excluded from the row set if the category is
   unbudgeted, or falling back to `budget_overview`'s own (equally
   negative) "Spent" figure if the category has a budget — that fallback
   figure is likewise never rendered as a bar. Only the bar itself is
   suppressed for such a row (there is no bar to show a negative length);
   the tick and status color are unaffected and render normally for a
   budgeted, negative-net-spend row — the tick still appears at the
   budget's proportional position on the shared scale, and the status
   color still follows the same three-tier `over`/`pct` logic as every
   other row, which naturally reads as `good` for a negative-pct row
   since `pct < 80` and `over` is always `False` for negative spend (e.g.
   Volleyball's March 2026 row — spent -$180.00, budget $444.42, `over`
   False, `pct` -41% — gets a `good`-colored tick with no bar). Put plainly, the actual
   inclusion/extraction criterion is "nonzero AND positive" — not merely
   "nonzero" — everywhere this design says a category's spend qualifies it
   for a row or a bar: the combined chart only ever renders a bar for a
   positive spent amount, never for a negative or exactly-zero net (there
   is nothing meaningful to compare against a budget once net spend has
   gone negative for the month — a rare edge case, not a normal state).
   The "$spent" figure is extracted from `get_category_breakdown`'s
   rendered table only when that category's row there shows a positive
   Spent value; when no row exists, a row exists but nets to exactly
   $0.00, or a row exists but nets to a negative value, "$spent"
   is instead extracted from `budget_overview`'s own rendered "Spent"
   column, and only if the category has a budget set (which does show
   $0.00, or a negative figure, for such budgeted categories). An
   unbudgeted category whose `get_category_breakdown` row nets to exactly
   $0.00 or to a negative value has no budget row to
   fall back to and is excluded from the row set entirely, per the row-set
   rule above — it is not rendered as a $0.00 (or negative), no-signal
   row. Likewise,
   the "pct%" figure is extracted from
   `budget_overview`'s rendered "% used" column, never recomputed from the
   raw `pct` field — consistent with the general sourcing rule and the old
   Recipe 3's already-established convention. "$budget" is extracted from
   `budget_overview`'s rendered "Budget" column.

   **Shared scale, not per-row scale.** Both the bar length and the tick
   position for every row share one axis: `max(every listed category's
   spent, every listed category's budget)`. Verified live against June
   2026: Housing's budget ($8,234.00) exceeds every category's actual June
   spend (Home Improvement tops out at $4,767.39) — a per-row scale (each
   bar relative only to its own budget, as the old meters did) would place
   Housing's tick correctly but make bars incomparable to each other; a
   scale fixed only to max-spent would push Housing's tick off the right
   edge of its row entirely. A single shared scale across every row
   guarantees every tick renders within its row (no clipping, no
   per-category special-casing) at the cost of the top-spend category's bar
   no longer necessarily reading as "full width" — an accepted tradeoff,
   since a wide near-empty track with a far-right tick is itself legible
   information ("large budget, barely touched"), not a rendering bug.
   Internally, the bar-length/scale positioning math reads the raw
   numeric field behind that displayed text, mirroring the same
   primary/fallback branching as the "$spent" string above: it reads
   `get_category_breakdown`'s `data.breakdown[].spent` field (cents) when
   that category has a row there, or `budget_overview`'s
   `data.categories[].spent_cents` field as the fallback — never a value
   recomputed or re-parsed from rendered text.

   This is a single-axis chart (dollars), not a dual-axis violation —
   `spent` and `budget` are the same unit compared against each other,
   distinct from `dataviz`'s dual-axis anti-pattern (two different
   measures with different scales sharing a plot). This does not map to a
   named "bullet chart" mark type — `dataviz`'s reference files define no
   bullet-chart mark and no tick/marker-overlay technique; its relevant
   existing marks are bar and meter (fill-only, no tick). The tick is
   instead a small addition on top of `dataviz`'s existing meter/bar mark
   spec: a thin (2px), neutral/chart-chrome-colored (not a new hue)
   absolutely positioned vertical line overlaid on the bar's track at the
   budget's proportional position along the shared scale — built directly
   in HTML/CSS (`position: absolute` within the track div, `left` offset
   computed from budget/scale as a percentage), a straightforward extension
   of marks `dataviz` already supports, not a new named chart form. The
   unfilled track behind the bar/tick carries forward the identical spec
   old Recipe 3 already used: dataviz's "Gridline (hairline)" chart-chrome
   neutral (light `#e1e0d9` / dark `#2c2c2a`), never a tint of the fill
   color, consistent with the existing rule that the fixed Status palette
   has no tint/step table — this is what a budgeted-but-zero-spend row
   renders as an empty bar against (empty track, visible tick).

3. **The Flags section drops its "Over budget" subsection.** The new
   combined chart already marks over-budget categories with `⚠` and the
   `critical` color — a separate flags table repeating the same categories
   is now redundant. Flags keeps two subsections: unusual charges
   (`find_anomalies`, month-filtered) and subscriptions & recurring bills
   (per Decision 1). Each subsection is still independently shown-or-omitted
   based on whether it has rows, per the original design's rule. The
   section's "nothing to flag" placeholder rule changes accordingly: it now
   renders only when both remaining subsections (anomalies, subscriptions &
   recurring bills) are empty, not when all three of the original
   subsections were empty.

## Architecture

Three files change, all PENDING — none of these edits have been applied to
the live skill files yet. They are implemented together, atomically, in one
pass, only after this design doc passes its quality gate, to avoid the live
skills ever being in a mutually-inconsistent intermediate state (e.g.
budget-coach describing a merged Recipe 2 that doesn't exist yet in
budget-visualizer):

- **Edit:** `.claude/skills/budget-visualizer/SKILL.md` — Recipe 2 (category
  bar chart) and Recipe 3 (budget meters) are replaced by one new recipe (a
  combined spend/budget chart per Decision 2, including the shared-scale
  rule and the categories-with-budget-but-no-spend inclusion rule). This
  merged recipe becomes the new "Recipe 2" (replacing the old category-chart
  Recipe 2 and meter Recipe 3), and the old Recipe 4 (flags) is renumbered
  to Recipe 3. Recipe 3 (flags, renumbered from 4) narrows its
  recurring-charges category guard to the bill-like allowlist per Decision
  1, relabels the section, and drops the now-redundant over-budget
  subsection per Decision 3. Post-merge, `budget-visualizer/SKILL.md` has 3
  recipes: 1 (stat row, unchanged), 2 (combined spend/budget chart, new),
  and 3 (flags, renumbered from 4, with Decision 1 and Decision 3's changes
  applied). This same edit must also update the file's trailing palette
  note (currently "none of the four recipes above introduces a new hue as
  specified") to read "three recipes" — it becomes stale the moment the
  recipe count drops from 4 to 3.
- **Edit:** `.claude/skills/budget-monthly-brief/SKILL.md` — the fixed
  render order changes from "stat row → category chart → budget-vs-actual
  meters → flags" to "stat row → combined spend/budget chart → flags"
  (one fewer distinct visual section, reflecting the Recipe 2+3 merge).
- **Edit:** `.claude/skills/budget-coach/SKILL.md` — its "## Charts"
  section currently tells the user this skill can do ad-hoc
  `budget-visualizer` recipes 1 (stat row) and 2 (category breakdown),
  since the old Recipe 2 needed only `get_category_breakdown`, which is in
  this skill's tool list. The new merged Recipe 2 (per Decision 2) also
  needs `budget_overview`, which is not in `budget-coach/SKILL.md`'s
  frontmatter tool list (`get_month_summary`, `get_category_breakdown`,
  `query_transactions`, `compare_periods`, `top_merchants`) — so that
  claim becomes false the moment this design ships. This is a minimal
  wording fix to the "## Charts" section only: it now says this skill
  covers recipe 1 (stat row) alone, and redirects a mid-conversation ask
  for the combined chart (recipe 2) to the full `/budget-monthly-brief`
  report, using the same redirect language already used there for the
  flags case. This does not add `budget_overview` to `budget-coach`'s
  tool list or otherwise change what it can do — that's a scope decision
  beyond this design.

No change to `budget-analyst/SKILL.md` — the rule-2 exemption for the
internal `query_transactions` cross-reference call (added in the prior
addendum) is unaffected by narrowing which categories qualify as a match.

## API Surface

No tool or frontmatter changes. Same tools as before
(`get_month_summary`, `get_category_breakdown`, `budget_overview`,
`find_anomalies`, `recurring_charges`, `query_transactions`); this design
only changes how their results are filtered, combined, and rendered.

## Invariants

**Checkable by inspection (once implemented):**
- `budget-visualizer/SKILL.md` has one combined chart recipe (not two
  separate category-chart/budget-meter recipes), specifying: bar length =
  spent, tick = budget (when set), shared `max(spent, budget)` scale across
  all rows, status color matching `budget_overview`'s `over`/`pct` logic,
  row set = union of positive-actual-spend and budget-set categories (a
  `get_category_breakdown` row's mere presence does not imply positive
  spend, and a negative net spend — refunds exceeding charges — is
  excluded/falls-back exactly like the exactly-$0.00 case, never treated
  as qualifying just because it's nonzero), sorted by spent descending.
- `budget-visualizer/SKILL.md`'s combined chart recipe states that a
  category with no budget set at all gets the `good` tier color (never a
  new fourth tier) and no tick mark, distinguished from a good-and-under-
  budget row only by the tick's absence.
- `budget-visualizer/SKILL.md`'s Recipe 3 (flags, renumbered from 4) states
  the recurring-charges category guard is exact membership in
  `{Subscriptions, Utilities, Insurance, Housing, NY529,
  Sewer/Water/Trash}`, not the broader `is_spend()` check, and the section
  label reads "subscriptions & recurring bills in \<reported month\>".
- `budget-visualizer/SKILL.md`'s flags section no longer has an
  over-budget subsection.
- `budget-monthly-brief/SKILL.md`'s visual-report render order lists 3
  sections (stat row, combined chart, flags), not 4.

**Testable:**
- A merchant included in the subscriptions/recurring-bills section has a
  matched `query_transactions` row whose category is exactly one of
  `{Subscriptions, Utilities, Insurance, Housing, NY529,
  Sewer/Water/Trash}` — a match whose category is any other spend-eligible
  category (e.g. Transportation, Dining Out, Shopping) must never appear,
  even with an otherwise-qualifying exact merchant/sign match.
- For every row in the combined chart, the tick position (when a budget is
  set) is computed against the same shared scale as every other row's bar
  length — no row's tick renders outside its own row's width.
- Every category whose actual spend this month is positive (i.e. its
  `get_category_breakdown` row, if any, shows a positive Spent value — a
  row's mere presence does not qualify, since a charge offset by an
  equal same-month refund/credit can produce a row reading $0.00, and
  refunds exceeding charges can produce a row reading negative) or
  which has a `budget_cents > 0` set appears exactly once in the combined
  chart; a category with neither — including an unbudgeted category whose
  `get_category_breakdown` row nets to exactly $0.00 or to a negative
  value — is absent.
- For every row's "$spent" figure: if the category's
  `get_category_breakdown` row shows a positive Spent value, "$spent" is
  extracted from that tool's rendered table; otherwise (no
  `get_category_breakdown` row exists, one exists but nets to exactly
  $0.00, or one exists but nets to a negative value) and the category has
  a budget set, "$spent" is extracted from
  `budget_overview`'s rendered "Spent" column instead — never left blank
  or computed locally. (An unbudgeted category whose
  `get_category_breakdown` row nets to $0.00 or to a negative value has no
  row in the chart at
  all, per the row-membership invariant above.)

## Out of scope (deliberately)

- Any generic mechanism for the skill to auto-detect a custom category as
  "bill-like" — the `NY529` and `Sewer/Water/Trash` allowlist entries are
  each a one-time, explicit, user-confirmed addition, not a
  pattern-matching heuristic.
- Any change to `recurring_charges`, `budget_overview`, or
  `get_category_breakdown` themselves (no tool/schema change) — this
  design is entirely at the report-rendering layer.
