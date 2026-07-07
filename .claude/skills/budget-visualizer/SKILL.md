---
name: budget-visualizer
description: Shared visual-report discipline for budget skills — chart recipes, palette rules, and how to render a report PDF from tool data. Referenced by name, not directly invoked.
tools: []
---

# Budget Visualizer — the shared chart discipline

This is the shared visual discipline any `budget-*` skill references when producing
a chart or full visual report. It defines HOW to build a chart from tool data; the
individual skill still decides WHEN to offer one.

## Chart-authoring procedure

1. Load the `artifact-design` skill first, to calibrate how much design investment
   the report warrants, even though the output is a local PDF rather than a
   published Artifact — the same typography/palette/layout discipline still
   applies to a well-made report page.
2. Run the `dataviz` skill's pick-form → assign-color → validate → mark → render
   steps for each recipe below. Reuse the already-validated palette (`#2a78d6`)
   rather than re-deriving color theory per report. Reports render in a single
   fixed light theme only — no dark-mode variant, no `prefers-color-scheme` or
   `data-theme` handling needed, since a static PDF has no viewer-side toggle.
3. Render the finished HTML to a PDF (see "Rendering to PDF" below) instead of
   publishing via the `Artifact` tool.

**CSS gotcha — theme tokens still go on `:root`, never a wrapper `<div>`.** Even
with a single fixed theme, define custom properties (`--text-primary`, `--page`,
etc.) on `:root` (the true document root), not on a wrapper element like
`.viz-root`. `body` is `:root`'s descendant but a wrapper div's *ancestor* — a
token declared only on the wrapper is invisible to `body`'s own
`color`/`background` rules, and any element that doesn't explicitly re-declare
`color` (an `<h1>`, a plain `.stat .value` tile, `.row-value`) falls back to
default black. This is a DOM-ancestry bug, not a theming one — it applies
regardless of how many themes a page supports.

**Rendering to PDF.** Write the finished HTML to a scratch file, then convert it
to PDF via headless Chrome, using `Bash`:

```
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --no-pdf-header-footer \
  --print-to-pdf=<output-path>.pdf \
  file://<scratch-html-path>
```

This is an environment-specific dependency (this exact Chrome path, on this
machine) — if Chrome is missing or moves, this step breaks and should be
reported to the user rather than silently falling back to something else.

`Write`, `Read`, `Edit`, and `Bash` are Claude Code session-level tools, already
available in an interactive session. Never declare them in a skill's `tools:`
frontmatter — that field is an MCP-domain-tool manifest, validated by
`tests/test_skills_lint.py` against the closed `SPEC_BY_NAME` registry, which has
no entry for any of them.

## General rule: displayed figures are extracted, never reformatted

Two different uses of a tool-provided number:

- **Displayed/labeled text** — a dollar or percentage figure the user reads as a
  number (a stat-tile value, a bar label, a flag-row cell, a "% used" label) — is
  read as an already-formatted substring or table cell out of a tool's `rendered`
  output (a composite line, or a markdown table row). Never recomputed or
  reformatted locally from a raw cents/int field in `data` — per `budget-analyst`
  rule 3.
- **Internal layout math** — a value used only to size or order a UI element and
  never itself shown as a number (a meter's fill-width proportion, a sort order) —
  may use an already-tool-computed `data` field directly (e.g. `pct`). This isn't a
  new financial claim, it's reusing a number the tool already computed to size an
  element.

Live figures quoted anywhere in a rendered report are point-in-time snapshots, not
values to hold constant — they drift as new transactions post.

## Recipes

1. **Stat row** — spent / income / net as three plain stat tiles (dataviz "figures"
   spec: label, value). No delta, no trend arrow. All three values are extracted
   as substrings of `get_month_summary`'s composite `rendered` line ("Spent **$X**
   · Income **$Y** · Net **$Z**") — `data` has no standalone `net_cents` field, so
   Net has no dedicated field to read in the first place; Spent and Income do have
   dedicated `data` fields but are extracted from `rendered` anyway, to keep the
   extraction approach uniform across the row. Net's tile text color: `critical`
   (`#d03b3b`) when negative, `good` (`#0ca30c`) when zero or positive.

2. **Spend vs. budget** — one row per category, combining what used to be two
   separate displays (a category bar chart and a budget meter) into one. A
   horizontal bar whose length is that category's dollars spent this month
   (positive spend only — see below), with a thin (2px) tick mark at the
   category's budget position when a budget is set. Unfilled track = dataviz's
   "Gridline (hairline)" chart-chrome neutral (`#e1e0d9`) — never a tint of the
   fill color, since the fixed Status palette has no tint/step table. Fill color
   uses three of dataviz's fixed Status
   palette's four tiers — `good` (`#0ca30c`), `warning` (`#fab219`), `critical`
   (`#d03b3b`) — `serious` is deliberately unused. The critical tier is keyed to
   `budget_overview`'s own `over` boolean (exact `spent > budget`, the same flag
   behind its `⚠` marker), not the rounded `pct` field — `pct` only decides
   warning (`over == False AND pct >= 80`) vs. good (`over == False AND pct <
   80`). A category with no budget set at all, or a zero/negative net spend
   this month, also gets `good` (there's no over-budget signal possible without
   a budget to compare against, or without positive spend to compare) — this is
   not a new fourth tier, just the existing `good` color. Trailing text reads
   `"$spent of $budget · pct%"` for budgeted rows or just `"$spent"` for
   unbudgeted rows.

   **Row set and sourcing.** A category belongs in the row set only if its
   actual spend this month is *positive* (check the value itself — a category
   whose only transaction(s) net to exactly $0.00 or negative, e.g. an
   offsetting refund, does not count as positive spend even though
   `get_category_breakdown` can still return a $0.00/negative row for it). A
   category with a budget set but zero spend this month (e.g. Housing before
   any rent has posted) is excluded from the chart entirely — a budget with
   nothing to show against it isn't worth a row, even though the tick/color
   machinery below would technically support rendering one. "$spent" is
   extracted from `get_category_breakdown`'s rendered table. For a row with a
   budget set, "$budget" is extracted from `budget_overview`'s rendered
   "Budget" column and "pct%" from its rendered "% used" column — never
   recomputed from raw fields. Internal bar-length/scale position math (not
   displayed text) may read `get_category_breakdown`'s `data.breakdown[].spent`
   cents field directly.

   **No bar for negative net spend.** A row only ever gets a bar for a positive
   spent amount — a category whose only transaction(s) net negative (e.g. a
   lone offsetting refund) is excluded from the row set entirely, same as the
   zero-spend case above.

   **Shared scale, not per-row scale.** Bar length and tick position for every
   row share one axis: `max(every listed category's spent, every listed
   category's budget)`. This guarantees every tick renders within its row (a
   large, barely-touched budget's tick doesn't clip off the edge) at the cost of
   the top-spend category's bar not always reading as "full width."

   **Sort.** Rows sort by dollars spent descending. Ties broken by category
   name, alphabetically.

   **Empty case.** If the row set is empty (no category has positive spend
   this month), render a "no spending to show" placeholder line instead of an
   empty section.

3. **Flags list** — a plain text/table block for unusual charges and
   subscriptions/recurring bills (icon + label, never color-alone; not a chart,
   since these are discrete named items). `find_anomalies` returns ~2 years of
   history, not just the reported month — filter its rows to the reported month
   before rendering. There is no separate over-budget subsection here — Recipe
   2's combined chart already marks over-budget categories with `⚠` and the
   `critical` color, so a duplicate table would be redundant. The two
   subsections (anomalies, subscriptions & recurring bills) are each
   independently shown-or-omitted based on whether that subsection has rows —
   not a single all-or-nothing gate. If both are empty after filtering, render
   "nothing to flag."

   **Unusual charges excludes known-recurring merchants.** After filtering
   `find_anomalies` to the reported month, drop any row whose `merchant` is
   *exactly equal* to a `merchant` value in `recurring_charges`' own list
   (the full list — not just the bill-like allowlist below). A merchant
   `recurring_charges` already recognizes as a stable, predictable pattern
   showing up again as "unusual" is a confusing double-flag, not a real
   anomaly (e.g. a recurring bill whose amount ticks up slightly month to
   month can trip `find_anomalies`' statistical threshold while still being
   entirely expected).

   **Subscriptions & recurring bills, scoped to the reported month via
   cross-reference.** `recurring_charges` returns one aggregate row per
   merchant (`avg_amount_cents`, `months`, `last_date` — the single most recent
   charge system-wide, no per-occurrence dates), so it cannot be month-filtered
   directly. Instead, cross-reference it against
   `query_transactions(month=<reported period>, limit=500)` — the explicit
   `limit=500` is required; the default `limit=50` silently truncates a busy
   month partway through. A `query_transactions` row qualifies as a match for a
   `recurring_charges` merchant only if **all** of:
   - its `merchant_norm` is *exactly equal* (never substring-matched) to that
     merchant's `recurring_charges` `merchant` value, **or** to one of that
     merchant's known aliases below — substring matching is unsafe in general
     (e.g. `FUCHS SANITATION` vs. `FUCHS SANITATION S` are distinct merchants),
     so a merchant whose statement descriptor drifts across billing periods is
     only recovered via an explicit, manually-curated alias, never a fuzzy
     rule:
     - `CLAUDE.AI SUBSCRIP ANTHROPIC.COM` (the `recurring_charges` key) also
       matches `ANTHROPIC CLAUDE ANTHROPIC.COM` and `PURCHASE ANTHROPIC C`;
     - `HLU HULU.COM BILL` (the `recurring_charges` key) also matches `HULU`.

     If the user's statement descriptors drift again later, this list needs a
     manual update — not solved generically;
   - its `merchant_norm` is not the placeholder value `UNKNOWN` (the only
     fallback value `sanitize.merchant_norm()` ever produces) — `UNKNOWN` rows
     are excluded from the cross-reference outright, never matched, since the
     placeholder can't be reliably attributed to one recurring merchant;
   - `amount_cents < 0` and its category is an *exact match* for one of a fixed
     bill-like allowlist — `{"Subscriptions", "Insurance", "Housing", "NY529"}`
     — not the broader `is_spend()` check. `Subscriptions`, `Insurance`, and
     `Housing` are this project's built-in categories; `NY529` is the user's
     own custom category, named explicitly since a custom category's meaning
     can't be derived generically. Essential fixed-utility categories (`Phone`,
     `Electricity`, `Gas/Propane`, `Internet`, `Sewer/Water/Trash`) are
     deliberately excluded — Verizon, the electric co-op, propane, and the
     garbage hauler recur every month by nature and aren't "subscriptions" the
     user would ever reconsider, so flagging them here is just noise. This
     narrower guard is what keeps the section to genuine discretionary
     subscriptions and large fixed obligations (Netflix, Claude, mortgage)
     rather than merchants the user just happens to visit most months (gas
     stations, grocery/warehouse stores, restaurants) — or bills that recur but
     were never in question. A refund/credit, a non-spend category, or a
     spend-eligible-but-not-allowlisted category must never count as a match,
     even with an exact `merchant_norm` equality. If the user adds further
     discretionary-subscription categories later, this allowlist needs a
     manual update — not solved generically.

   A recurring merchant with no qualifying match in the reported month is
   omitted from that month's section. **Known limitation:** because the match
   is exact-string (aliases above excepted), a merchant whose `merchant_norm`
   drifts across billing periods (a changed statement descriptor) can be
   silently missed even though it genuinely charged that month — an accepted
   tradeoff against the worse false-positive risk substring/fuzzy matching
   would reopen. The alias list closes this gap for the two merchants known to
   drift today; any other merchant that starts drifting needs the same manual
   treatment.

   For each included merchant, the displayed date and amount come from its
   matched `query_transactions` row(s) for the reported month — never
   `recurring_charges`' own `avg_amount_cents`/`last_date`, which are global,
   as-of-now figures that can point outside the reported month. When more than
   one row qualifies for the same merchant in the month, display the single
   most recent (latest-dated) one. When multiple qualifying rows share that
   latest date, no arithmetic is performed on the amounts (never sum them, per
   `budget-analyst` rule 3) — display the first such row in `query_transactions`'
   returned order instead. The column header reads "Amount" (not "Avg amount",
   which no longer describes a single month-scoped charge). "Months seen" stays
   `recurring_charges`' own global `months` figure, unchanged — it's not
   misleading in a month-scoped report. The section label reads "subscriptions &
   recurring bills in \<reported month\>" (not "recurring charges" or
   "currently-detected recurring charges (as-of-now)"). Because
   `recurring_charges`' own `rendered` block is still printed verbatim earlier
   in the same turn (per `budget-analyst` rule 2), add a short caption noting
   these figures are intentionally scoped to the month and may differ from the
   all-time figures shown in that earlier block for the same merchant.

   This `query_transactions(month=<period>, limit=500)` cross-reference call is
   exempt from `budget-analyst` rule 2 — its `rendered` block is raw transaction
   data used only internally to compute this section, never printed to the user
   as its own block.

Every new hue a recipe introduces must pass `dataviz`'s own
`scripts/validate_palette.js` (resolved from that skill's own base directory at
invocation time, not a path in this repo) before shipping, via `Bash`. This is a
forward-looking safety net — none of the three recipes above introduces a new
hue as specified.
