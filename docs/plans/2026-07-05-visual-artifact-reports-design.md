---
ticket: "N/A"
title: "Visual artifact reports: shared chart discipline + full-month report"
date: "2026-07-05"
source: "design"
---

# Visual artifact reports: shared chart discipline + full-month report

## Context

This session produced an ad-hoc visual: given "show my spending graph for June,"
Claude loaded the (non-budget-specific) `dataviz` skill, hand-built a validated
single-hue horizontal bar chart as an HTML file, ran the palette validator, and
published it as a Claude Artifact. It worked, but nothing in the `budget-*` skill
set says this is how a budget skill should respond to a visualization request —
it happened because the general-purpose `dataviz` skill triggered on the word
"graph," not because any `budget-*` skill knew to reach for it.

The user asked for three things:
1. Standardize that `budget-*` skills produce visuals as Artifacts, the same
   validated way, whenever a visual is warranted.
2. After creating an artifact, ask whether it should be auto-deleted.
3. A reusable way to generate a full-month report (not just one chart) in this
   format, on demand.

## Finding: artifact auto-deletion is not a capability that exists

Before designing (2), I checked the full tool surface available to Claude in
this project (including deferred/MCP tools) for any artifact-delete capability.
There is none. The `Artifact` tool can create or redeploy (update-in-place) a
page; nothing can remove a published one. A published Artifact is "private by
default" already — nothing more happens to it unless the user shares it.

The only thing actually deletable is the local scratchpad `.html` source file
used to generate/redeploy an artifact — and deleting it doesn't touch the
already-published page, it only forfeits the ability to redeploy that same
artifact later with an edit.

So "auto-delete" is reframed as: **after a report artifact is published, ask
once whether to delete the local scratch file**, framed honestly as source-file
cleanup, not artifact deletion. Default answer if the user doesn't care: keep it
(cheap, and enables a same-session redeploy if they ask for a tweak).

## Decisions

1. **Chart authoring stays freshly-generated per report**, not a static template
   file. Each report re-runs the existing `dataviz` skill procedure (pick form →
   assign color → validate palette → apply marks → render) rather than filling
   placeholders in a checked-in HTML skeleton. Chosen over a template file for
   consistency with how every `budget-*` skill already works: pure instructions,
   no shipped code artifacts to maintain or drift from the tool schema.
2. **New shared reference skill, `budget-visualizer`** (`tools: []`), following
   the exact precedent of `budget-analyst` (a shared discipline doc other skills
   reference by name, not a directly-invoked skill). It pins the specific chart
   recipes budget reports use, so every budget visualization looks like the same
   product instead of each skill reinventing layout.
3. **The full-month report is an extension of `budget-monthly-brief`**, not a new
   skill. It already gathers most of the data a report needs, plus one addition
   (`budget_overview`, per the Architecture section below), and already ends its
   turn with an offer ("save this brief?"). Add one more offer after that:
   "Want this as a visual report too?"

## Architecture

Three files change, one file is added:

- **New:** `.claude/skills/budget-visualizer/SKILL.md` — shared visual discipline,
  `tools: []`, referenced by name from any budget skill that renders a chart.
- **Edit:** `.claude/skills/budget-monthly-brief/SKILL.md` — add `budget_overview`
  to its tool list and a final "offer a visual report" step. No other
  `tools:` change is needed: `Artifact`, `Write`, `Read`, `Edit`, and `Bash`
  are Claude Code session-level tools, already available in an interactive
  session regardless of what a skill's `tools:` frontmatter lists — this
  project's `tools:` field is an MCP-domain-tool manifest (validated by
  `tests/test_skills_lint.py` against the closed `SPEC_BY_NAME` registry in
  `src/local_budget/agent/tools.py`), not a place to declare generic host
  tools, and there is no mechanism to register one there. Its `description:`
  frontmatter must also be updated to mention that it handles visual/chart
  report requests for a period (today it only says "Compose the period brief
  — spent/income/net, where it goes, ways to save, flags — then offer to save
  it"), since the direct-visual-request carve-out below depends on a request
  like "show me the visual report for June" actually routing to this skill,
  and skill selection needs a textual hook in the description to make that
  match.
- **Edit:** `.claude/skills/budget-coach/SKILL.md` — one line: if the user asks
  for a chart/graph/visual mid-conversation, follow `budget-visualizer` instead
  of reaching for the generic `dataviz` skill directly. No frontmatter `tools:`
  change is needed here either, for the same reason as above. `budget-coach`'s
  MCP tool list is unchanged by this design — it still has only `get_month_summary`,
  `get_category_breakdown`, `query_transactions`, `compare_periods`, and
  `top_merchants`. That caps which `budget-visualizer` recipes it can render
  ad hoc: the stat row and the category-breakdown chart both work from tools
  already on its list, but the budget-vs-actual meters (recipe 3, needs
  `budget_overview`) and the flags list (recipe 4, needs `find_anomalies` and
  `recurring_charges`) are not reachable from a `budget-coach` conversation —
  those two recipes are only produced via the full `budget-monthly-brief`
  report. Separately, `budget-coach`'s `description:` frontmatter still reads
  "Read-only." — that description should be lightly amended (e.g. to clarify
  that "read-only" refers to the budget data, not to artifact rendering) so a
  future reader doesn't mistake its use of the `Artifact` tool for a
  contradiction of the read-only contract.
- **Edit:** `.claude/skills/budget-analyst/SKILL.md` — rule 6's "General
  composition rule for colliding pending questions" passage states as settled
  fact that "`budget-monthly-brief` always ends its turn with a required
  'offer to save the brief' question pending an explicit yes before
  `save_brief`." The direct-visual-request carve-out below makes that no
  longer universally true — a direct-visual-only request skips the
  save-brief question entirely, never asking it that turn. Add a brief
  exception note immediately after that sentence acknowledging the carve-out
  as an explicit, narrow exception to the "always" claim, so the shared
  discipline doc stays accurate once this ships — e.g. "(the
  direct-visual-request carve-out in `budget-monthly-brief` is a narrow,
  explicit exception to this 'always': it skips the save-brief question
  entirely for a request that asks only for the visual report)." This is a
  body-text-only edit; `budget-analyst`'s `tools: []` frontmatter is
  unchanged.

No MCP server changes, no new tools of the `mcp__budget__*` kind — this is
purely a skill-instruction layer change. `Artifact`, `Write`, `Read`, `Edit`,
and `Bash` are host capabilities already available in an interactive Claude
Code session; they are used by these skills' instructions but are not, and
cannot be, declared in either skill's `tools:` frontmatter.

### `budget-visualizer` contents

Reuses the `dataviz` skill's procedure and validated palette (`#2a78d6` /
`#3987e5` light/dark, already validated this session) rather than re-deriving
color theory per report.

**Chart-authoring procedure**, in order: (1) load the `artifact-design` skill
first, to calibrate how much design investment the report warrants — this is
the `Artifact` tool's own operating requirement, done once before the page is
written; (2) then run `dataviz`'s existing pick-form → assign-color → validate
→ mark → render steps for each recipe below.

**General rule — every displayed dollar/percentage figure is extracted, never
reformatted; internal layout math is a separate case.** This rule has two
halves that apply to two different uses of a tool-provided number:

- **Displayed/labeled text** — a dollar or percentage figure the user actually
  reads as a number (a stat-tile value, a bar label, a flag-row cell, a "%
  used" label) — must be read as an already-formatted substring or table cell
  out of one of the MCP tools' `rendered` output (a composite line such as
  "Spent **$X** · Income **$Y** · Net **$Z**", or a markdown table row such as
  "| 5 | Groceries | $852.46 | 15 |" — the leading cell is the tool's own
  numbered `Row` column, since these tools render via `render.table(...,
  numbered=True)`). None of these displayed figures are
  recomputed or reformatted locally from a raw cents/int field in a tool's
  `data` dict — consistent with `budget-analyst` rule 3.
- **Internal layout math** — a value used only to size or order a UI element
  and never itself shown as a number (e.g. a meter's fill-width proportion, a
  sort order) — may use an already-tool-computed field straight out of `data`
  (e.g. `pct`), because doing so isn't presenting a new financial claim, it's
  reusing a number the tool itself already computed to size a UI element. This
  is not the same as re-deriving a dollar amount from raw cents, which remains
  forbidden for display purposes.

Each recipe below states which `rendered` line or table row its displayed
figures draw from, and separately calls out any `data` field it uses purely
for layout math; none of them re-derive a dollar string from raw cents for
display. (Live figures quoted as examples throughout this doc — e.g. the
Groceries row above, or Recipe 3's Shopping/Volleyball percentages below — are
a point-in-time snapshot from 2026-06 data at design time; they will drift as
new transactions post and are illustrative only, not values to re-verify
against.)

Pins three recipes:

1. **Stat row** — spent / income / net as three plain stat tiles (dataviz
   "figures" spec: label, value). No delta, no trend arrow — nothing here is
   derived from `monthly_trend`. Per the general rule above, all three tile
   values are extracted as substrings of `get_month_summary`'s single
   composite `rendered` line ("Spent **$X** · Income **$Y** · Net **$Z**") —
   not read from `data["spend_total_cents"]` or `data["income_cents"]`
   (Spent and Income do each have a dedicated `data` field, but per the
   general rule they are not used for display), and not by locally computing
   `income_cents - spend_total_cents` for Net. `data` carries no standalone
   `net_cents` key at all, so Net has no dedicated field to read in the
   first place; sourcing all three from the one `rendered` line keeps the
   extraction approach uniform across the row instead of mixing strategies
   within the same recipe. The Net tile's text color uses the same fixed
   Status palette pinned in Recipe 3 below: `critical` (`#d03b3b`) when Net
   is negative (overspent), `good` (`#0ca30c`) when Net is zero or positive —
   applied only to the tile's text color, not as a new mark or element.
2. **Category breakdown** — the horizontal bar chart already built this
   session: single hue, sorted descending, hover tooltip, value at the bar
   tip. Reused unchanged. Per the general rule above, each bar's dollar
   label is extracted from the "Spent" cell of the corresponding row in
   `get_category_breakdown`'s rendered markdown table (each row like
   "| 5 | Groceries | $852.46 | 15 |" — Row, Category, Spent, #, since the
   tool renders via `render.table(..., numbered=True)`) — never reformatted
   from the raw `spent` cents field in `data["breakdown"]`.
3. **Budget vs. actual** — one **meter** per category that has a budget set
   (dataviz "meter" spec), not only the over/near-limit ones — so the section
   matches its title even when every budget is healthy: fill = amount spent,
   unfilled track = a fixed neutral — dataviz's "Gridline (hairline)"
   chart-chrome color already defined in its palette reference (light
   `#e1e0d9` / dark `#2c2c2a`), never a tint/lighter step of the fill color,
   since the fixed Status palette used for the fill (below) has no tint/step
   table defined — only one flat hex per role. Fill color uses three of
   dataviz's fixed Status palette's four tiers — `good` (`#0ca30c`), `warning`
   (`#fab219`), `critical` (`#d03b3b`) — deliberately skipping the palette's
   fourth tier, `serious`, since the meter only needs three states (under
   budget / approaching limit / over limit). The critical
   tier is keyed directly to `budget_overview`'s own `over` boolean
   (`data["categories"][i]["over"]`, an exact `spent > budget` comparison —
   the same flag that drives the `⚠` marker in that tool's `rendered` table)
   rather than to the rounded `% used` field, because `pct` is a rounded
   integer and can disagree with `over` at a rounding boundary (e.g. $0.50
   over a $300 budget has `over=True` but rounds to `pct=100`, which a
   `pct > 100` rule would misclassify as warning, not critical). So the
   tiers are: `over == True` → critical, regardless of what `pct` rounds to;
   `over == False AND pct >= 80` → warning; `over == False AND pct < 80` →
   good. `% used` (`data["categories"][i]["pct"]`, e.g. Shopping at 81%,
   Volleyball at 78% for 2026-06) still drives the fill amount and the
   warning/good split; it just no longer decides the critical tier. The
   meter's fill-width proportion is internal layout math per the general
   rule's second half above, so reading `pct` directly out of `data` for that
   purpose is fine — it is not re-deriving a dollar/percentage figure for
   display, it's reusing a value `budget_overview` already computed to size
   the fill. If the meter also shows a displayed "% used" label as text,
   that label text is extracted from `budget_overview`'s rendered table's "%
   used" column cell instead, per the general rule's displayed-text half —
   never reformatted from the raw `pct` int. Chosen over a grouped two-bar-per-
   category chart because the actual question ("how close to the limit") is
   a single ratio per category, which a meter encodes directly — a second
   bar would force the eye to compute the same ratio manually. Categories
   with no budget set are excluded from this section (nothing to ratio
   against). A category with an explicitly-set $0 budget limit (`budget_cents
   <= 0`) is treated the same as "no budget set" and is also excluded — this
   sidesteps `reports.py`'s `_pct()`, which returns `None` whenever budget
   <= 0, leaving the fill-width/tier logic undefined for that case; treating
   `budget_cents <= 0` as "no budget" is a simple, defensible v1 rule. If no
   category has a budget set at all, render a short "no budgets set"
   placeholder line instead of an empty section.
4. **Flags list** — a plain text/table block for over-budget categories,
   anomalies, and recurring-charge flags, using the dataviz status palette
   (icon + label, never color-alone) — not a chart, since these are discrete
   named items, not a magnitude comparison. `find_anomalies` is not a
   month-scoped tool — a live call returns roughly two years of history, not
   just the reported month — so before any anomaly row is rendered here,
   filter the returned rows to those whose date falls within the reported
   month. `recurring_charges` is different: it returns one aggregate row per
   merchant (occurrences, months, avg_amount_cents, `stable`, and a single
   `last_date` — the most recent charge only), not one row per occurrence, so
   there is no per-row date to filter by — filtering on `last_date` would only
   work for the current/most-recent month and would silently drop
   genuinely-recurring merchants for any historical month report. So
   recurring-charge rows are never month-filtered: render them as returned,
   labeled in the report as **currently-detected recurring charges**
   (as-of-now state, reflecting all data seen so far — not scoped to the
   reported period), rather than implying they're scoped to the reported
   month. Over-budget categories come from `budget_overview`, which is
   already month-scoped, so no filtering is needed for that part of the list
   either. If there are no
   flags at all after filtering (no over-budget categories, no anomalies in
   the reported month, and no currently-detected recurring charges), render a
   short "nothing to flag" placeholder line instead of omitting the section.
   The three subsections — over-budget list, anomalies, recurring charges —
   are each independently shown-or-omitted based on whether that subsection
   has any rows; this is not a single all-or-nothing gate for the whole
   flags section. Since `recurring_charges` is never month-filtered, a user
   with any recurring merchant at all will almost always have a non-empty
   recurring-charges subsection, so the fully-empty case above is rare in
   practice — but the "nothing to flag" placeholder still applies per
   subsection: it is normal and expected to see, for example, zero
   over-budget categories and zero anomalies each render "nothing to flag"
   while a populated recurring-charges list sits alongside them in the same
   section.

Every new hue combination introduced by a recipe must be run through
`dataviz`'s own `scripts/validate_palette.js` before shipping, exactly as done
for today's single chart, via `Bash` (a host-level tool already available in
an interactive session, not something declared in any skill's `tools:`
frontmatter). That script is
not a path in this repo — it lives inside the globally-bundled `dataviz` skill
package (a version-hashed cache location) and must be resolved from that
skill's own base directory at invocation time, the same way it was located and
run earlier in this session. This is a forward-looking safety net for any
*future* recipe that introduces a new hue — it does not fire for any of v1's
four pinned recipes as specified: Recipe 2 reuses the already-validated
series-1 blue (`#2a78d6`/`#3987e5`), and Recipes 3 and 4 reuse the fixed,
pre-validated Status and chrome colors verbatim, so none of the four recipes
introduces a new color.

### `budget-monthly-brief` new final step

After the existing save-brief offer resolves (yes/no), add:

> "Want this as a visual report too?"

If yes: gather (or reuse already-fetched) `get_month_summary`,
`get_category_breakdown`, `budget_overview`, `find_anomalies`, and
`recurring_charges` results. `find_anomalies` returns roughly two years of
history, not just the reported month, so filter its rows down to the reported
month before rendering. `recurring_charges` returns one aggregate row per
merchant rather than per-occurrence dates, so it is rendered as returned
(unfiltered) and labeled as currently-detected recurring charges rather than
month-scoped — see Recipe 4 above. Then
build one HTML artifact per the
`budget-visualizer` recipes, in this fixed order: stat row → category chart →
budget-vs-actual meters → flags. The scratch file is named per period (e.g.
`budget-report-2026-06.html`) so that generating reports for two different
months in the same session doesn't collide on the same path. Publish via the
`Artifact` tool. Then ask the
one cleanup question: "Want me to delete the local scratch file now, or keep it
in case you want changes?" — never phrased as deleting the artifact/page
itself, since that isn't possible.

If the user later asks for a tweak in the same session, redeploy to the same
Artifact URL by reading the same scratch file, editing it, and calling
`Artifact` again (same path) — this is why "keep it" is the sensible default
when the user is ambiguous. `Read`, `Edit`, `Write`, and `Artifact` are host
capabilities already available in an interactive session; nothing needs to be
declared in the skill's `tools:` frontmatter to use them. If the scratch file
was deleted (the user chose "delete" at the cleanup question) and a tweak is
asked for later in the same session, there is no file left to read or edit —
rebuild the artifact fresh instead: re-gather the tool data and re-render per
the recipes above, then publish via a new `Artifact` call (this produces a new
URL, since redeploying in place requires the original scratch file).

**Direct-visual-request carve-out.** The default flow above only offers a
visual after the full text brief and the save-brief question — not
on-demand if the user asked only for the visual. Carve-out: when the
request is specifically and only for the visual/chart report (e.g. "show me
the visual report for June," not a general spending question),
`budget-monthly-brief` skips straight to gathering the same 5 tools —
`get_month_summary`, `get_category_breakdown`, `budget_overview`,
`find_anomalies`, `recurring_charges` — and rendering the artifact per the
recipes above, skipping the narrative brief walk and the save-brief question.
By contrast, "give me June's numbers and a chart" does **not** qualify — it's
a numbers request with a visual add-on, not visual-only — so it follows the
normal default flow (full narrative brief → save-brief question →
visual-report offer). The carve-out is an alternate entry point into the
same rendering logic, not a replacement for the default path.

What "skips the full text brief" does *not* skip: `budget-analyst` rule 2
still requires each gathered tool's `rendered` block printed verbatim. The
carve-out skips only the **narrative** — the "spent/income/net → where it
goes → ways to save → flags" synthesis prose stitching sections together —
not the 5 tools' `rendered` blocks themselves, which are still printed (per
rule 2) before or alongside the artifact, regardless of what each one
renders. Separately, rule 6's numbered-list drill-down only applies to
blocks that render a `Row` column: `get_month_summary`,
`get_category_breakdown`, and `recurring_charges` do; `budget_overview` and
`find_anomalies` don't, so a follow-up "tell me more about #3" has nothing
to resolve against in those two.

### `budget-coach` one-line addition

Add to its tool-selection guidance: "If the user asks to see a chart, graph, or
visual, follow `budget-visualizer`'s recipes instead of the generic `dataviz`
skill directly" — so an ad-hoc single-category chart mid-conversation (like
today's) looks identical to one embedded in a full monthly report. In
practice this only covers recipes 1 (stat row) and 2 (category breakdown),
since those are the only ones `budget-coach`'s existing MCP tool list can
feed; a mid-conversation ask for a budget-vs-actual meter or a flags list
should be answered by pointing the user to the full `budget-monthly-brief`
report instead of `budget-coach` attempting a partial render.

`budget-coach`'s existing read-only contract is stated in two separate places,
not one verbatim line: its frontmatter `description:` ends "...Read-only.",
and its body separately states "This skill is **read-only**; it never
writes." Both refer to financial-data writes — categorization, budget limits,
expected income, and the like via the `mcp__budget__set_*` / `save_*` tools.
Publishing a visual `Artifact` is not a write to the budget database (it's a
Claude-side page render), so it does not conflict with or require loosening
that contract.

## API Surface

No new MCP tools. No changes to any `mcp__budget__*` tool signature.
`budget-monthly-brief`'s `tools:` frontmatter list gains `budget_overview`
(already an existing tool, just not previously in this skill's list). That is
the only `tools:` frontmatter change any of the three edited skills needs —
the `budget-analyst` edit (the rule 6 exception note, above) is a body-text
change only; its `tools: []` frontmatter is untouched.

`Artifact`, `Write`, `Read`, `Edit`, and `Bash` are not part of this design's
API surface in the MCP sense at all: they are Claude Code session-level
tools, already available in any interactive session regardless of what a
skill's `tools:` frontmatter lists. This project's `tools:` field is
specifically an MCP-domain-tool manifest — `tests/test_skills_lint.py`
asserts every token in it exists in the closed `SPEC_BY_NAME` registry built
from `TOOL_SPECS` in `src/local_budget/agent/tools.py`, which contains only
`mcp__budget__*`-style domain tools. There is no entry for `Artifact`,
`Write`, `Read`, `Edit`, or `Bash` in that registry, and no mechanism to add
one — declaring any of them in a skill's frontmatter would fail that test.
So both skills use these host tools per their instructions above, without
declaring them anywhere.

## Invariants

**Checkable by inspection (once implemented):**
- `budget-visualizer/SKILL.md` will exist with `tools: []` (shared reference
  doc, not directly invoked — matches `budget-analyst`'s pattern).
- Every chart recipe in `budget-visualizer` must name a specific dataviz mark
  spec (stat tile / horizontal bar / meter) rather than open-ended "make a
  chart."
- `budget-monthly-brief`'s frontmatter `tools:` list must include
  `budget_overview`.
- Neither `budget-monthly-brief/SKILL.md` nor `budget-coach/SKILL.md`'s
  frontmatter `tools:` list should include `Artifact`, `Write`, `Read`,
  `Edit`, or `Bash` — these are host-level tools, not MCP domain tools, and
  adding them would fail `tests/test_skills_lint.py`'s check against
  `SPEC_BY_NAME`.
- `budget-monthly-brief/SKILL.md`'s `description:` frontmatter must be
  updated to mention handling visual/chart report requests for a period, and
  `budget-coach/SKILL.md`'s `description:` frontmatter must be updated to
  clarify that "read-only" refers to budget-data writes, not artifact
  rendering — both edits are required by this design but not yet applied.
- The four recipes render in a fixed order within the artifact: stat row →
  category chart → budget-vs-actual meters → flags. Never reordered per
  report.
- The scratch file is named per period (e.g. `budget-report-2026-06.html`),
  not a fixed/shared filename — so generating reports for two different
  months in the same session doesn't collide on the same path.

**Testable (requires running the skill):**
- For a general brief request, the full-report offer only fires after the
  save-brief question is already resolved (yes or no) — it never preempts or
  replaces it. The one exception is the direct-visual-request carve-out: when
  the user's request is specifically and only for the visual report, that
  request skips the narrative brief walk and the save-brief question entirely
  rather than sitting behind them — but each of the 5 gathered tools'
  `rendered` blocks is still printed verbatim, per `budget-analyst` rule 2.
- No dollar figure in the artifact is computed by re-doing arithmetic on raw
  cents; every value is the tool's already-formatted string, per
  `budget-analyst` rule 3.
- All three stat-row values — Spent, Income, and Net — are extracted as
  substrings of `get_month_summary`'s single composite `rendered` line
  ("Spent **$X** · Income **$Y** · Net **$Z**"), never read from
  `data["spend_total_cents"]` / `data["income_cents"]` and never recomputed
  locally as `data["income_cents"]` minus `data["spend_total_cents"]` for
  Net. `data` has no standalone `net_cents` field at all, so Net has no
  dedicated field to read in the first place; Spent and Income do have
  dedicated `data` fields but are extracted from the `rendered` line anyway,
  per the general dollar/percentage extraction rule.
- The cleanup question at the end never uses the word "artifact" as the thing
  being deleted — only "local file" / "scratch file," since deleting the
  artifact itself is not offered because it is not possible.
- `dataviz`'s own `scripts/validate_palette.js` — resolved from that skill's
  base directory at invocation time, not a path inside this repo, and invoked
  via `Bash` — passes (light and dark) for any new hue introduced by the
  meter, before the report is published.
- Recipe 3 (budget-vs-actual) uses three of dataviz's fixed Status palette's
  four tiers — `good` (`#0ca30c`), `warning` (`#fab219`), `critical`
  (`#d03b3b`), with `serious` deliberately unused — never a flat two-color
  scheme — with the critical tier keyed directly to `budget_overview`'s own
  `over` boolean (exact, matches the tool's `⚠` flag by construction) rather
  than to the rounded `pct` field; `pct` only decides warning
  (`over == False AND pct >= 80`) vs. good (`over == False AND pct < 80`)
  when the category is not already over budget. The meter's fill-width
  proportion reads `pct` directly from `data` as internal layout math; any
  displayed "% used" label text is separately extracted from
  `budget_overview`'s rendered table's "% used" column cell.
- Recipe 3 excludes any category with no budget set from its meters; if no
  category has a budget set at all, it renders the "no budgets set"
  placeholder line instead of an empty section.
- Recipe 4 evaluates the "nothing to flag" placeholder independently per
  subsection (over-budget categories; anomalies filtered to the reported
  month; currently-detected recurring charges rendered unfiltered) — a
  subsection with no rows shows the placeholder for that subsection only,
  never as a single gate for the whole flags section. It is normal for the
  recurring-charges subsection to be populated (it's never month-filtered)
  while the other two subsections independently show "nothing to flag."

## Out of scope (deliberately)

- A static/checked-in HTML template file (rejected above — freshly authored
  wins for this project's size and consistency with existing skills).
- A standalone `budget-report` skill (rejected — folded into
  `budget-monthly-brief`, which already owns this exact data gathering).
- Any real artifact-deletion mechanism (does not exist; not something this
  design can add — it would require a platform-level capability outside the
  tools available to this session).
- Wiring `budget-visualizer` into any `budget-*` skill other than
  `budget-coach` and `budget-monthly-brief`. Those two are the only skills
  with an immediate, obvious visual use case (an ad-hoc mid-conversation chart,
  and a full-month report). `budget-budgets`, `budget-subscriptions`,
  `budget-income`, `budget-categorize`, `budget-reconcile`, and `budget-setup`
  are left untouched by this pass — deliberately, not by oversight.
  `budget-visualizer`'s recipes are keyed to data shape (stat row, category
  breakdown, budget-vs-actual, flags) rather than to a specific skill, so any
  of the untouched skills can adopt the same one-line pointer later without
  re-designing `budget-visualizer` itself.
- Proactive, judgment-based visual offering. Despite goal 1's phrasing
  ("whenever a visual is warranted"), the mechanism this pass actually
  delivers only fires on an explicit user request — a chart/graph/visual
  keyword, or the monthly-brief's own post-brief offer. A skill deciding on
  its own, without being asked, that a visual would help the user right now
  is a possible future extension, not something this design implements.
- Subcategory-level budgets. `budget_overview`'s actual output nests a
  `subcategories` list inside each category, each with its own
  `budget_cents`/`spent_cents`/`pct`/`over`, and setting a subcategory-level
  limit is a real, existing workflow — `split_subscriptions` assigns each
  Subscriptions merchant its own subcategory label (grouping only; it does
  not set any limit), and `set_budget_limit` (called with a `subcategory`
  argument, e.g. Subscriptions/Netflix) is the tool that actually sets the
  limit on it. Recipe 3's
  meters and Recipe 4's flags only ever read top-level category data from
  `budget_overview`; v1 of the visual report does not represent any
  subcategory budget anywhere — a subcategory over its limit is invisible
  in the artifact even though it may be visible in `budget_overview`'s raw
  output. Left as a natural extension for a later pass, consistent with
  keeping this a lean v1.
- A "ways to save" section in the visual report. `budget-monthly-brief`
  normally produces four sections from four tools — spent/income/net,
  where it goes, ways to save (via `insights`), and flags — but the visual
  report's 4 recipes (stat row, category chart, budget-vs-actual, flags)
  only cover three of them. `insights`' output is actually magnitude-shaped
  (label + amount) and could plausibly get a bar-list recipe similar to
  Recipe 2 — but v1 deliberately caps the visual surface at 4 recipes to
  stay lean; a 5th "ways to save" bar list is a natural, low-risk future
  addition, not something ruled out by the data shape. The visual report
  intentionally covers the numeric/magnitude side of the brief and leaves
  "ways to save" to the text brief only — the visual
  report complements the text brief, it does not replace it.
