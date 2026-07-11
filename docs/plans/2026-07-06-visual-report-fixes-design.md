---
ticket: "N/A"
title: "Visual report fixes: month-scoped recurring charges + CSS token-scoping bug"
date: "2026-07-06"
source: "design"
---

# Visual report fixes: month-scoped recurring charges + CSS token-scoping bug

## Context

The first generated report (`docs/plans/2026-07-05-visual-artifact-reports-design.md`,
implemented in commit `a695b56`) surfaced three real issues once actually rendered:

1. The "currently-detected recurring charges" list is unfiltered (as-of-now state,
   per the original design's own reasoning — `recurring_charges` has no
   per-occurrence dates to filter by). The user wants it scoped to charges that
   actually happened in the reported month.
2. A request to switch the category-breakdown chart from a bar chart to a pie
   chart. Investigated and **declined** — see Decision 2.
3. Text was invisible in dark mode: the `<h1>` title, the Spent/Income stat-tile
   values, and the category chart's dollar labels all rendered in default black
   instead of the theme-aware token, because of a CSS custom-property scoping bug
   (root-caused below). Confirmed present in **both** artifacts built this
   session (`june-spending.html` and `budget-report-2026-06.html`).

## Decisions

1. **Recurring charges, scoped to the reported month via cross-reference.**
   `recurring_charges` itself still cannot be month-filtered directly — confirmed
   again: it returns one aggregate row per merchant (`occurrences`, `months`,
   `avg_amount_cents`, `last_date` — the single most recent charge only), with no
   per-occurrence date list. But `query_transactions(month=<reported month>,
   limit=500)` returns actual posted transactions for that month, each with its
   own `merchant_norm`. The explicit `limit=500` matters: `query_transactions`
   defaults to `limit=50`, which silently truncates a busy month partway through
   (verified live against June 2026's 100+ transactions — an unbounded call cut
   off at 2026-06-22, dropping real recurring charges from earlier in the month
   such as Verizon, Netflix, Audible, and Hulu — though raising the limit alone
   does **not** fully recover Hulu specifically: its June charge's
   `merchant_norm` (`HULU`) still fails the exact-match cross-reference against
   `recurring_charges`' aggregate key for that same subscription
   (`HLU HULU.COM BILL`, from other months), a separate `merchant_norm`-drift
   issue documented as a known limitation below — independent of, and not
   solved by, this truncation fix). 500 is comfortably above any
   realistic month's transaction count, so every call to `query_transactions`
   for this cross-reference must pass `limit=500` explicitly. Cross-referencing
   the recurring-merchant list against that month's transactions tells you
   which recurring merchants actually charged in the reported month, subject to
   the `merchant_norm`-drift limitation noted below — it is a real
   per-transaction answer, not a rounding/approximation like the rejected
   `last_date`-in-month check would have been (that approach was rejected in the
   original design for silently dropping merchants that recurred in a historical
   month but have since charged again more recently). `query_transactions`
   hard-caps at `limit=500` with no truncation indicator in its response; if a
   future month's transaction count ever exceeds that, the same silent-
   truncation failure mode this fix is closing would reappear at a higher
   threshold with no way to detect it from the response alone. This is an
   accepted limitation given the current dataset's scale (roughly 100-120
   transactions/month) — not something this addendum re-engineers now — and
   should be revisited only if transaction volume grows significantly.

   **Placeholder merchant values are excluded from the cross-reference
   entirely.** `merchant_norm` falls back to the literal string `UNKNOWN` when
   a payee can't be parsed into a real merchant name — confirmed by reading
   `sanitize.merchant_norm()`, `UNKNOWN` is the only fallback value this
   normalizer ever produces (both its trigger points return that exact
   literal string, nothing else) — this string does not uniquely identify one
   real merchant, so it can never be reliably attributed to a single
   `recurring_charges` row. Verified live: `recurring_charges` has a row for
   `UNKNOWN` (avg $74.78, last charge 2026-06-16), but June 2026's actual
   transactions include at least two unrelated transactions both normalized to
   `UNKNOWN` (-$373.00 Home Improvement on 06-08, -$50.00 Volleyball on
   06-16) — neither matches the $74.78 average. An exact-match
   cross-reference, applied naively, would wrongly attribute one of these
   unrelated charges to the "UNKNOWN recurring charge" and include it in the
   report. Fix: `UNKNOWN` is excluded from the month-scoped cross-reference
   outright — dropped from the month-scoped list rather than matched at all.
   This is a simple,
   safe exclusion rule, not amount-based disambiguation (matching a specific
   transaction to the recurring row by proximity to `avg_amount_cents`, say) —
   that kind of heuristic is deliberately out of scope here to keep the fix
   lean.

   The cross-reference itself must be **exact string equality**, not substring
   matching: `recurring_charges`' `merchant` field *is* the exact
   `merchant_norm` value (confirmed in `detect.py`), so an exact match between
   `recurring_charges`' `merchant` and `query_transactions`' `merchant_norm` is
   both sufficient and safe.

   **A `merchant_norm` match alone is not sufficient — the matched row must
   also be an actual charge, not a refund/credit.** `recurring_charges` itself
   is built from `detect._spend_rows`, which only considers rows where
   `categories.is_spend(category)` is true *and* `amount_cents < 0` (an actual
   outgoing charge) — it does not count positive-amount rows (refunds,
   credits, reversals) or non-spend categories toward "recurring." An
   exact-match cross-reference that ignores sign/category would be looser
   than the very tool it's cross-referencing against. Verified live: this
   pattern is real in this dataset — two merchants already on the recurring
   list, `GABB WIRELESS` and `ONCE UPON A`, have genuine positive-amount rows
   (refunds/credits) on record (`GABB WIRELESS`: $16.18 on 2026-01-20 and
   $9.42 on 2025-01-23; `ONCE UPON A`: $15.95 and $7.33, both on 2026-04-06)
   — demonstrating that refund/credit rows do occur for recurring merchants
   in this data, not a hypothetical concern. Of the two, only `ONCE UPON A`
   demonstrates the sign check doing independent work: its refund rows are
   categorized "Shopping" (spend-eligible), so only `amount_cents < 0`
   excludes them, whereas `GABB WIRELESS`'s refund rows are already
   categorized "Income" and would be excluded by the category filter alone.
   A sign-less `merchant_norm` match against rows like `ONCE UPON A`'s would
   wrongly mark a merchant as "charged this month" in whichever month a
   refund happens to land, even though no actual charge posted. Fix: a `query_transactions` row only counts as a qualifying match
   if, in addition to the exact `merchant_norm` equality, `amount_cents < 0`
   and the row's category is spend-eligible per the same `is_spend()`
   definition `detect._spend_rows` uses — mirroring the underlying tool's own
   "what counts as a charge" logic, not just its merchant names.

   Substring matching is unsafe here — verified live,
   the recurring list contains both `FUCHS SANITATION S` (active, charged in
   June) and `FUCHS SANITATION` (a distinct, dormant merchant last seen ~1.5
   years ago); a substring match on either direction would wrongly conclude the
   dormant merchant also charged in June. This exact-equality check is a
   mechanical, low-ambiguity set-membership comparison between the two
   already-rendered tables (`recurring_charges`' `merchant` values and
   `query_transactions(month=<period>, limit=500)`'s `merchant_norm` values) —
   reliable enough for an LLM to execute directly, without needing a database
   query. `run_sql` is deliberately not used for this: `tests/test_skills_lint.py`
   hard-bans `run_sql` from any skill's `tools:` manifest in this project
   (`assert "run_sql" not in tools`), so it is not an option here regardless of
   convenience. Using `Bash` (available as a host tool, per the sibling
   07-05 design's precedent for `validate_palette.js`) to do a deterministic
   string-diff instead of manual instruction-based comparison was also
   considered and rejected: the data volume here is small and bounded (roughly
   30-34 recurring merchants against at most 500 transaction rows), which is
   comfortably within reliable manual-comparison range and consistent with
   this project's no-shipped-code-artifacts, pure-instructions philosophy for
   its skill set — so the added complexity of a script isn't justified.

   **Known limitation: exact match can produce false negatives when
   `merchant_norm` drifts across billing periods for the same real-world
   subscription.** Statement descriptors for one merchant can differ
   month-to-month, so `recurring_charges`' aggregate `merchant` key (drawn
   from whichever occurrence it last saw) can fail to exact-match a
   genuinely-recurring charge's `merchant_norm` in the reported month.
   Verified live in June 2026: Hulu's June charge has `merchant_norm` `HULU`,
   but `recurring_charges`' aggregate row for that same subscription is keyed
   `HLU HULU.COM BILL` (from other months) — `HULU` != `HLU HULU.COM BILL`
   under exact match, so Hulu's real June charge is silently excluded from
   the cross-reference. Netflix shows the identical pattern (`merchant_norm`
   `NETFLIX.COM NETFLIX.COM` in June vs. `NETFLIX.COM` and `NETFLIX.COM LOS
   GATOS` in other months) and only happens to survive June's report by
   coincidence — its June-specific string happens to match that month's
   aggregate key. This is an accepted tradeoff, not a defect this addendum
   needs to solve: substring or fuzzy matching would avoid this false
   negative but reopen the worse false-positive risk demonstrated by
   `FUCHS SANITATION`/`FUCHS SANITATION S` above, which is the wrong
   direction to fail for a personal single-user tool. Net effect: a
   genuinely-recurring merchant that charged in the reported month may still
   be silently omitted from the month-scoped section if that month's
   `merchant_norm` differs from `recurring_charges`' aggregate key — this
   does not affect the guarantee that substring collisions can't cause false
   positives, only whether every true positive is caught.

   **Displayed values for included merchants must also be month-scoped, not
   the aggregate's global stats.** Qualifying for inclusion (a cross-reference
   match) is not the same as what gets *shown* once included — the original
   design's Recipe 4 displays `recurring_charges`' own aggregate fields
   (`avg_amount_cents` as "Avg amount", `months` as "Months seen", and
   `last_date` — the single most recent charge system-wide — as "Last charge"),
   which are global, as-of-now figures, not scoped to the reported month. Left
   unchanged, a merchant could qualify for a March 2026 report because it
   charged in March, while its displayed "Last charge" column still shows some
   unrelated later date (e.g., a July charge), directly contradicting the
   section's own month-scoped framing. Fix: for each included merchant, the
   displayed date/amount come from that merchant's actual matched
   transaction(s) in the reported month (from the same `query_transactions`
   call used for the cross-reference), not from `recurring_charges`'
   `avg_amount_cents`/`last_date`. The column header changes accordingly:
   `recurring_charges`' own rendered table calls this column "Avg amount"
   (`tools.py` line 273), which is accurate for that tool's global,
   multi-occurrence average but no longer accurate once the displayed figure
   is a single month-scoped charge (per the tie-break rule below, which
   always yields a single row's amount, never a sum) — so for this
   month-scoped recurring-charges display, the column header is renamed
   from "Avg amount" to "Amount" to match what it now actually shows.

   **When a merchant has more than one qualifying match within the reported
   month, display the most recent one.** A merchant can post more than one
   qualifying (sign/category-filtered) charge in a single month — e.g. a
   mid-cycle price change, a plan add-on, or simply two billing events in one
   calendar month — and the doc must say which one wins rather than leaving
   it to whichever the LLM happens to pick. Fix: when multiple
   `query_transactions` rows qualify for the same merchant in the reported
   month, the displayed date and amount are those of the single most recent
   (latest-dated) qualifying row within that month. This mirrors
   `recurring_charges`' own "Last charge" semantics — most-recent-wins — just
   re-scoped from all-time to the reported month, so the tie-break rule is
   consistent with the concept it's replacing, not a new invented convention.
   If multiple qualifying rows share that same latest date (verified live:
   `NEWYORK 529 ACH` has two qualifying rows both dated 2026-06-23,
   demonstrating that such same-date ties do occur in real June data — though
   in this particular case both tied rows happen to share the identical
   $50.00 amount, so this example doesn't itself show the row-selection
   choice changing the displayed figure), no arithmetic is performed on the
   amounts — per `budget-analyst` rule 3, dollar amounts are never summed or
   otherwise computed locally, only ever extracted as already-formatted
   values. Instead the tie is broken by picking a single row rather than
   summing: the first such row in `query_transactions`' returned order
   (rows come back `ORDER BY t.posted_date DESC`, with no secondary sort
   key) is the one used, and its single already-formatted amount is
   displayed as-is — never a sum of two or more rows. This satisfies the
   no-arithmetic requirement (rule 3) regardless of which row is picked; it
   is a "single row, never summed" rule, not a "which specific row" rule.
   Note, though, that because the underlying SQL has no secondary sort key,
   SQL itself does not define row order among same-date rows without a
   tiebreaker column — so this query does not guarantee a stable pick
   across calls if same-date rows ever have genuinely different amounts
   (the order is whatever scan/sort plan SQLite happens to choose, which
   could change with an index, `ANALYZE`, a SQLite version bump, or table
   growth). That's a latent edge case, not something this design needs to
   solve now, since correctness here only requires "single row, never
   summed."

   **"Months seen" is intentionally left as-is — it is not month-scoped.**
   Only `avg_amount_cents` ("Avg amount") and `last_date` ("Last charge") get
   month-scoped replacement rules above; `recurring_charges`' `months` field
   ("Months seen") stays the global, as-of-now aggregate figure. Unlike the
   other two fields, "Months seen" isn't misleading in a month-scoped report —
   "this merchant has recurred N months historically" is useful supporting
   context for a merchant appearing in this month's list, not a claim about
   the reported month specifically, so there's no contradiction to fix.
   The section's label also changes accordingly
   — from "currently-detected recurring charges (as-of-now)" to "recurring
   charges in \<reported month\>" — to match the new month-scoped behavior.
   Because `recurring_charges`' own `rendered` block is still printed
   verbatim earlier in the same brief turn in either the full
   narrative-brief flow or the direct-visual-request carve-out — both of
   which still print `recurring_charges`' verbatim block per
   `budget-analyst` rule 2 — the section's caption also notes that
   these figures are intentionally scoped to the reported month and may
   differ from the all-time average/last-date shown in that earlier block
   for the same merchant — no reconciliation logic, just an explicit
   distinction in the label/caption so two different dollar figures for one
   merchant in the same turn aren't read as a contradiction.

2. **Category breakdown stays a bar chart.** A pie chart with June's 15
   categories was investigated and presented to the user with the tradeoff
   (angle/area judgment degrades past ~5-6 slices; several of June's categories
   are under 1% of total spend and would be unlabelable slivers). User chose to
   keep the existing bar chart. No change to Recipe 2.

3. **Fix the CSS token-scoping bug at its root, and document the fix in
   `budget-visualizer` so future reports don't repeat it.** Root cause: both
   artifacts declared theme tokens (`--text-primary`, `--page`, etc.) as custom
   properties scoped to a `.viz-root` wrapper `<div>`, but then referenced those
   same tokens from `body`'s own `color`/`background` rules. `body` is the
   *parent* of `.viz-root` in the DOM, not a descendant of it — CSS custom
   properties only cascade downward from where they're declared, so `body`'s
   `var(--text-primary)` reference is undefined at `body`'s own scope, making
   that declaration invalid at computed-value time. Any element that doesn't
   explicitly re-declare its own `color` (the `<h1>`, the plain `.stat .value`
   tiles before their `.good`/`.critical` modifier, `.row-value`) inherits
   whatever the browser falls back to — effectively default black — which is
   invisible against a dark theme surface. Fix: define the token custom
   properties on `:root` (true document root, ancestor of `body` itself), not on
   a wrapper div — so `body`'s own rules and everything nested inside can see
   them. This applies to **all four** theme-conditional blocks that define
   these tokens, not just the base declaration: the base `:root { ... }` block,
   the `@media (prefers-color-scheme: dark) { ... }` block, `:root[data-theme="dark"]
   { ... }`, and `:root[data-theme="light"] { ... }` — every one of them must
   target `:root` specifically, never a wrapper `<div>` class (e.g.
   `.viz-root[data-theme="dark"]`). The `Artifact` tool stamps `data-theme` on
   the true root element, not on any wrapper div, so a wrapper-scoped
   `data-theme` selector matches nothing and is a distinct bug beyond the
   base-declaration one diagnosed above — fixing only the base `:root { ... }`
   block while leaving the three theme-conditional blocks on a wrapper div
   would leave theme-switching broken even after this fix. This is a durable
   procedural fix, not a one-off patch: `budget-visualizer` gains an explicit
   note about this exact gotcha, since any future report built from its
   recipes would otherwise repeat it.

## Architecture

Three files change:

- **Edit:** `.claude/skills/budget-visualizer/SKILL.md` — Recipe 4 gains the
  month cross-reference instruction for recurring charges (replacing the
  "render as returned, unfiltered" language from the original design),
  including the placeholder-merchant exclusion rule (`UNKNOWN`, the only
  fallback `merchant_norm` value this project's normalizer produces, is
  dropped from the month-scoped list outright, never matched), the
  sign/category match guard (a qualifying `query_transactions` row must also
  have `amount_cents < 0` and a spend-eligible category, matching
  `detect._spend_rows`' own logic), and the most-recent-within-month
  tie-break rule for merchants with more than one qualifying match. Recipe 4
  also gains the
  month-scoped display rule: once a merchant qualifies for inclusion, its
  displayed date/amount are read from its matched `query_transactions` row(s)
  for the reported month, not from `recurring_charges`' global
  `avg_amount_cents`/`last_date`, and the section's label changes from
  "currently-detected recurring charges (as-of-now)" to "recurring charges in
  \<reported month\>". The chart-authoring procedure gains a CSS gotcha note:
  theme tokens must be defined on `:root`, never on a wrapper `<div>` class,
  since `body`'s own styling needs to see them too.
- **Edit:** `.claude/skills/budget-monthly-brief/SKILL.md` — the "Visual report"
  section gains `query_transactions(month=<period>, limit=500)` as an
  additional call whose purpose is cross-referencing recurring merchants
  (exact match on `merchant` vs. `merchant_norm`, per Decision 1) against the
  reported month, and sourcing that subsection's displayed date/amount once a
  merchant qualifies (not used for display in any other section). This file
  currently
  enumerates its gather-step tool list in **two separate places** — the
  default "If yes" gather step, and the direct-visual-request carve-out's own
  restatement of that gather list (currently phrased as "the same 5 tools")
  — and **both** must be updated consistently to add this call, so that
  neither entry point into the visual report (the normal narrative-brief flow
  or the carve-out) misses the cross-reference. The carve-out's literal "5
  tools" phrasing must also be updated to reflect the new count once this 6th
  call is added, so it doesn't go stale. This `query_transactions` call is
  exempt from `budget-analyst` rule 2 (print each tool's `rendered` block
  verbatim): its `rendered` block is raw transaction data used only
  internally, to compute which recurring merchants qualify for the
  month-scoped "recurring charges" flags section, and is never printed to the
  user as its own block.
- **Edit:** `.claude/skills/budget-analyst/SKILL.md` — rule 2 ("print the
  tool's `rendered` block verbatim") gains a narrow exception for the
  `query_transactions(month=<period>, limit=500)` call `budget-monthly-brief`
  makes internally to cross-reference recurring merchants for the visual
  report's month-scoped flags section: its `rendered` block is raw
  transaction data used only to compute part of the visual artifact, not
  user-facing brief content, so it is not printed. (Already applied in the
  live file as of round 6.)
- No change to Recipe 2 (category breakdown) — the bar-vs-pie investigation
  concluded with no change.

## API Surface

No tool or frontmatter changes. `query_transactions` is already in
`budget-monthly-brief`'s tool list — used both for the cross-reference match
and, for merchants that qualify, as the source of the displayed date/amount in
the recurring-charges subsection (no other section's display changes). Every
call to it for this purpose must pass `limit=500` explicitly — the default
`limit=50` is not sufficient for a full month's transactions. No new MCP
tools; `run_sql` is intentionally not used (banned from skill manifests by
`tests/test_skills_lint.py`).

## Invariants

**Checkable by inspection (once implemented):**
- `budget-visualizer/SKILL.md`'s Recipe 4 states recurring charges are
  cross-referenced against `query_transactions(month=<period>, limit=500)`,
  not rendered unfiltered, requires each matched row to have `amount_cents <
  0` and a spend-eligible category (matching `detect._spend_rows`' own
  logic), and excludes the placeholder `merchant_norm` value `UNKNOWN` (the
  only fallback value this project's normalizer produces) from that
  cross-reference outright. It also documents the known `merchant_norm`-drift
  false-negative limitation: exact-match cross-referencing can miss a
  genuinely-recurring merchant whose statement descriptor changed
  month-to-month (verified live for Hulu in June 2026; Netflix shows the
  same underlying merchant_norm-drift pattern across other months, e.g.
  "NETFLIX.COM" vs. "NETFLIX.COM LOS GATOS" vs. June's "NETFLIX.COM
  NETFLIX.COM", but happens to exact-match and be correctly included in
  June specifically), an accepted tradeoff against the worse false-positive
  risk substring/fuzzy matching would reopen.
- `budget-visualizer/SKILL.md`'s Recipe 4 states the section label is
  "recurring charges in \<reported month\>" (not "currently-detected recurring
  charges (as-of-now)"), that the column header is "Amount" (not "Avg
  amount"), that displayed date/amount for each included merchant come from
  its matched `query_transactions` row(s) — the single most recent one when
  more than one qualifies within the month, with same-date ties broken by
  taking a single row (the first in `query_transactions`' returned order for
  that call, not a guaranteed-stable pick across calls) rather than
  summing across rows — not from `recurring_charges`'
  `avg_amount_cents`/`last_date`, and that "Months seen" remains
  `recurring_charges`' global `months` figure, unchanged.
- `budget-visualizer/SKILL.md`'s chart-authoring procedure includes the
  `:root`-not-wrapper-div token-scoping note, covering all four
  theme-conditional blocks (base, media-query, `[data-theme="dark"]`,
  `[data-theme="light"]`).
- `budget-monthly-brief/SKILL.md`'s **both** gather-tool enumerations (the
  default "If yes" step and the carve-out's own restatement) include the
  `query_transactions(month=<period>, limit=500)` cross-reference call.
- `budget-monthly-brief/SKILL.md` and `budget-analyst/SKILL.md` (rule 2) both
  state that this specific `query_transactions(month=<period>, limit=500)`
  cross-reference call is exempt from rule 2's verbatim-print requirement —
  its `rendered` block is used only internally to compute which recurring
  merchants qualify for the month-scoped flags section, never printed to the
  user as its own block.
- Recipe 2 (category breakdown) is unchanged — still a bar chart.

**Testable:**
- A recurring merchant included in a report's "recurring charges" section must
  have at least one `query_transactions(month=<reported period>, limit=500)`
  row whose `merchant_norm` is *exactly equal* (not substring-matched) to that
  merchant's `recurring_charges`' `merchant` value, **and** whose
  `amount_cents < 0` **and** whose category is spend-eligible per the same
  `is_spend()` definition `detect._spend_rows` uses, and that `merchant_norm`
  value must not be `UNKNOWN` (the only placeholder value this project's
  normalizer produces) — otherwise
  it's excluded from that month's report (though it may still appear in a
  different month's report where it did charge, or where a different real
  merchant matched). A positive-amount row (refund/credit) or a non-spend
  category alone must never satisfy this match, even with an exact
  `merchant_norm` equality.
- For every merchant included in that section, the displayed date and amount
  match a qualifying `query_transactions(month=<reported period>, limit=500)`
  row for that merchant in the reported month — never `recurring_charges`'
  global `avg_amount_cents` or `last_date`, which can point outside the
  reported month. When more than one qualifying row exists for that merchant
  in the reported month, the displayed date/amount must be those of the
  single most recent (latest-dated) qualifying row, never an arbitrary or
  earlier one. When more than one qualifying row shares that same latest
  date, the displayed amount must still be exactly one single row's
  already-formatted amount — whichever row `query_transactions` returns
  first for that call (not a pick guaranteed stable across calls, since the
  underlying query has no secondary sort key) — never a sum of two or more
  rows' amounts.
- Every rendered report artifact renders visible (theme-appropriate, non-black
  on dark) text for its title, stat-tile values, and category-chart dollar
  labels in both light and dark mode — no element relies on an unstyled
  inherited `color` from a token declared on a non-ancestor wrapper.
- A visual-report turn never prints a raw `rendered` block for the
  `query_transactions(month=<reported period>, limit=500)` cross-reference
  call itself — only the month-scoped "recurring charges" section (derived
  from it) appears in the chat. The call's output is consumed purely as
  internal computation input for that section, never surfaced as its own
  tool-output block, per its rule-2 exemption.

## Out of scope (deliberately)

- Any change to how `recurring_charges` itself works (no tool/schema change) —
  the fix is entirely at the report-rendering layer, cross-referencing two
  already-available tools.
- Any pie-chart variant of the category breakdown (investigated, declined by
  the user in favor of keeping the bar chart).
