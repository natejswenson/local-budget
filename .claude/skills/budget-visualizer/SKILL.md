---
name: budget-visualizer
description: Shared visual-report discipline for budget skills — chart recipes, palette rules, and how to render an artifact from tool data. Referenced by name, not directly invoked.
tools: []
---

# Budget Visualizer — the shared chart discipline

This is the shared visual discipline any `budget-*` skill references when producing
a chart or full visual report. It defines HOW to build a chart from tool data; the
individual skill still decides WHEN to offer one.

## Chart-authoring procedure

1. Load the `artifact-design` skill first, to calibrate how much design investment
   the report warrants — the `Artifact` tool's own operating requirement, done once
   before the page is written.
2. Run the `dataviz` skill's pick-form → assign-color → validate → mark → render
   steps for each recipe below. Reuse the already-validated palette (`#2a78d6` /
   `#3987e5` light/dark) rather than re-deriving color theory per report.

`Artifact`, `Write`, `Read`, `Edit`, and `Bash` are Claude Code session-level tools,
already available in an interactive session. Never declare them in a skill's
`tools:` frontmatter — that field is an MCP-domain-tool manifest, validated by
`tests/test_skills_lint.py` against the closed `SPEC_BY_NAME` registry, which has no
entry for any of them.

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

2. **Category breakdown** — a horizontal bar chart: single hue, sorted descending,
   hover tooltip, value at the bar tip. Each bar's dollar label is extracted from
   the "Spent" cell of the corresponding row in `get_category_breakdown`'s rendered
   table (each row like "| 5 | Groceries | $852.46 | 15 |" — Row, Category, Spent,
   #) — never reformatted from the raw `spent` cents field.

3. **Budget vs. actual** — one meter per category that has a budget set (not only
   over/near-limit ones, so the section matches its title even when every budget
   is healthy). Fill = amount spent; unfilled track = dataviz's "Gridline
   (hairline)" chart-chrome neutral (light `#e1e0d9` / dark `#2c2c2a`) — never a
   tint of the fill color, since the fixed Status palette has no tint/step table.
   Fill color uses three of dataviz's fixed Status palette's four tiers — `good`
   (`#0ca30c`), `warning` (`#fab219`), `critical` (`#d03b3b`) — `serious` is
   deliberately unused (only three states needed). The critical tier is keyed to
   `budget_overview`'s own `over` boolean (exact `spent > budget`, the same flag
   behind its `⚠` marker), not the rounded `pct` field — `pct` only decides
   warning (`over == False AND pct >= 80`) vs. good (`over == False AND pct < 80`).
   `pct` drives the fill-width proportion directly (internal layout math); a
   displayed "% used" label, if shown, is extracted from `budget_overview`'s
   rendered table's "% used" column cell instead. Categories with no budget set,
   or an explicitly-set `budget_cents <= 0`, are excluded (nothing to ratio
   against, and `_pct()` returns `None` for budget <= 0 anyway). If no category has
   a budget set, render a "no budgets set" placeholder line instead of an empty
   section.

4. **Flags list** — a plain text/table block for over-budget categories,
   anomalies, and recurring-charge flags (icon + label, never color-alone; not a
   chart, since these are discrete named items). `find_anomalies` returns ~2 years
   of history, not just the reported month — filter its rows to the reported month
   before rendering. `recurring_charges` returns one aggregate row per merchant
   (only a single `last_date`, no per-occurrence dates) — it cannot be
   month-filtered correctly, so render it unfiltered, labeled "currently-detected
   recurring charges" (as-of-now state). Over-budget categories come from
   `budget_overview`, already month-scoped. The three subsections (over-budget,
   anomalies, recurring charges) are each independently shown-or-omitted based on
   whether that subsection has rows — not a single all-or-nothing gate. If all
   three are empty after filtering, render "nothing to flag."

Every new hue a recipe introduces must pass `dataviz`'s own
`scripts/validate_palette.js` (resolved from that skill's own base directory at
invocation time, not a path in this repo) before shipping, via `Bash`. This is a
forward-looking safety net — none of the four recipes above introduces a new hue
as specified.
