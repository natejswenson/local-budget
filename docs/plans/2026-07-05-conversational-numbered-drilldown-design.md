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
   system is built around one hard rule: `budget-analyst` persona rule #2
   requires the agent to "print the tool's `rendered` block verbatim" — each
   tool returns a `rendered` markdown block, shown to the user exactly as
   returned, plus at most three sentences of synthesis — and, separately,
   `render.py`'s own module docstring independently states that its
   formatting logic lives there so that "clean & beautiful is a
   regression-guarded property... rather than a model whim." Numbering
   therefore belongs in `render.py`'s renderers, not as something the agent
   freehandly prepends when it feels like it — otherwise it's inconsistent
   and violates the verbatim-print discipline the app is built on.
2. **A bare/flexible reference always means "row N of the most recently
   rendered numbered list."** No expiry, no re-anchoring ritual: the
   most-recent list wins regardless of how many turns have elapsed. There are
   exactly two carve-outs, both made precise in Error Handling and nowhere
   else:
   - *Ambiguity* — a natural generalization covering two situations, both
     resolved the same way (ask, don't guess): **(a) cross-list** — two or
     more numbered lists were rendered in the SAME response with no
     natural-language disambiguator; **(b) same-list, multi-row** — a
     phrase-match reference (not a bare number/ordinal, which is inherently
     unique within a list since row positions are unique) matches more than
     one row within the SAME numbered list — e.g. `review_queue`'s
     checks-to-review table is built by `manual.checks_to_review()`, which
     has no `GROUP BY` and lists one row per transaction, so two checks
     written to the same payee produce two rows sharing an identical
     `merchant_norm` label, and a phrase like "the landlord check" can match
     both. This is the ONLY thing "ambiguous" means. In either case, and
     only these cases, the agent asks instead of guessing.
   - *Invalidation* — a write invalidates EVERY numbered list rendered before
     it, not just the list it was resolved against (Architecture §2),
     forcing a re-render of whichever list is being referenced before "most
     recent wins" applies again. This is deliberately BROAD/conservative
     rather than scoped to "the one list the write touched": a write like
     `set_merchant_category` changes a merchant's category, which changes
     per-category spend totals, which can silently reorder an earlier
     `get_category_breakdown` list (`ORDER BY spent DESC`) even though that
     list was never directly referenced by the write. Tracking precisely
     which lists a given write could perturb is not worth the complexity for
     v1 — invalidate everything rendered so far and require a fresh render
     before trusting any of it again.
3. **Drill-down is chained/multi-level** (category → transactions → back),
   not a single hop. "Going back" needs no new code — it's re-calling the same
   tool with the same args and re-printing its `rendered` block, never
   recalled from memory (this preserves persona rule #1: never invent a
   number).
4. **Level 2 is uniformly `query_transactions(category=X)`** — it exists
   today and works for every category, including ones with subcategories
   (e.g. Subscriptions). `subcategory_breakdown` is a separate, pre-existing
   MCP tool that is explicitly OUT OF SCOPE for v1's drill-down chain: it
   does not opt into `numbered=True` and has no trigger condition anywhere
   in this design. Wiring it into the chain (instead of, or in addition to,
   `query_transactions` for subcategorized categories) is a candidate future
   extension, not part of v1.
5. **Bonus use case:** the same mechanism numbers `review_queue`'s
   uncategorized-merchant rows AND its checks-to-review rows (both tables,
   same UI pattern — see rollout table), so categorization can happen by row
   number ("1 → Dining Out") instead of retyping merchant strings — directly
   motivated by the manual categorization work done earlier in this session.
   Because `review_queue`'s lists are re-derived live from the DB on every
   call, this is only safe under the identity-resolution + invalidation rule
   in Architecture §2/§3: multiple row references given in the same message
   are resolved to their underlying identity (merchant string / `txn_id`)
   against ONE `data` snapshot before any write executes, and any write
   invalidates EVERY numbered list rendered before it (not just this one)
   for further row references in later turns. This identity resolution is
   orthogonal to, and does not replace, the existing confirm-before-write
   rule (persona rule #4) — it determines WHAT gets written, not WHETHER a
   confirmation is needed; the agent still proposes the resolved changes and
   waits for an explicit "yes" before writing. See the worked example in
   Data Flow.

## Architecture

One small render-layer capability, one data invariant, one persona rule, and
two narrowly-scoped MCP schema additions — `query_transactions` gains an
optional `month` INPUT parameter, and `top_merchants`'s `data` payload gains
a `month` OUTPUT field (see the named exceptions in §2) — not a new
subsystem, no data-model change, no new MCP tools. These are the ONLY TWO MCP
tool schema changes in this design; see §2 for why each is necessary and API
Surface for the corrected "no schema changes" framing.

### 1. Render layer (`src/local_budget/agent/render.py`)

Add an optional `numbered: bool = False` parameter to `table()` and `bars()`:

- `table(rows, cols, *, numbered=False)` — when `True`, prepend a row-index
  column (1-indexed) as the first column of the rendered markdown table,
  headered **`Row`** — deliberately NOT `#`. `#` is already a live header on
  two tools in the v1 rollout: `get_category_breakdown` (`"#": r["n"]`, a
  transaction count, `tools.py:163`) and `review_queue`'s merchant table
  (`"#": r["count"]`, `tools.py:445`). Reusing `#` for the row index would
  silently collide with those existing count columns in the same table —
  two different meanings under one header. `Row` was checked against every
  existing `table()`-based header on every v1-rollout table (below) and
  collides with none; any future tool that opts into `numbered=True` must
  repeat that check before landing. This check is scoped to `table()`-based
  tools only — `get_month_summary` AND `top_merchants` (bars section) render
  via `bars()`, which has no column headers at all (see the `bars()` bullet
  immediately below), so there is no header to collide and no check to run
  for either of them; see Architecture §4's rollout table for which
  mechanism each tool uses.
- `bars(items, *, numbered=False)` — when `True`, prefix each line with
  `N. ` before the label. `bars()` has no column headers, so there is no
  collision risk there; see the prefix-width note under Testing Strategy.

Default `False` everywhere — zero behavior change for any existing caller
until a call site explicitly opts in.

### 2. Data invariant (no new fields as a rule; two named schema exceptions below)

For most v1 tools, row N of the `rendered` block corresponds directly to
`data.<list>[N-1]` of that same tool response — true by construction, no
schema change:

| Tool | `data` key | Row N ⇔ |
|---|---|---|
| `get_category_breakdown` | `breakdown` | `data["breakdown"][N-1]` |
| `query_transactions` | `rows` | `data["rows"][N-1]` — no `txn_id` present (see exception) |
| `review_queue` — merchants | `merchants` | `data["merchants"][N-1]` — merchant string lives under the dict key `"merchant"` (from `manual.needs_review()`), NOT `"merchant_norm"`; that same string is what gets passed as the `merchant_norm=` argument to `set_merchant_category` (the tool's own parameter name — data key and tool parameter name differ) |
| `review_queue` — checks | `checks` | `data["checks"][N-1]` — includes `txn_id` |
| `top_merchants` | `rows` | `data["rows"][N-1]` — merchant identity is `data["rows"][N-1]["merchant_norm"]` |
| `recurring_charges` | `recurring` | `data["recurring"][N-1]` — merchant identity is `data["recurring"][N-1]["merchant"]` (from `detect.find_recurring()`; NOT `"merchant_norm"`) |

**Named exception — `get_month_summary`:** its numbered section
(`spend_by_category`) is a **dict**, not a list, so the flat invariant does
not apply verbatim. The precise, checkable sub-invariant is: row N of the
`bars()` output ⇔ the Nth entry of `list(data["spend_by_category"].items())`
in Python dict-insertion order. This holds because the handler builds both
the dict and the `bars()` input from `sorted(spend.items(), key=..., reverse
=True)` — called twice, with identical arguments, over the same source
`spend` dict, in the same handler invocation. Dict insertion order therefore
equals `bars()` render order by construction, not by assumption. This is a
sub-invariant that must stay checkable: if a future edit changes either
call's sort/filter, it breaks silently unless a unit test pins it (Testing
Strategy).

**Named exception — `query_transactions`:** its SQL projection selects
`posted_date, amount_cents, category, merchant_norm, a.acct_last4 AS
account_last4, txn_type` (`tools.py`) — so the output dict key is
`account_last4`, not `acct_last4` — and no `txn_id`. `set_txn_category`
requires `txn_id`, so `data["rows"][N-1]`
cannot key a recategorize call. **Decision for v1: do not add `txn_id` to
this tool.** `query_transactions`'s numbered rows are read-only/terminal —
there is no "recategorize row N" claim for this tool (the rollout table
below is corrected accordingly). This keeps `query_transactions`'s own
`data` payload free of new fields (see the two, unrelated, named schema
exceptions elsewhere in this section — `query_transactions`'s `month`
input parameter and `top_merchants`'s `data["month"]` output field — neither
of which touches this tool's `data` shape). Adding `txn_id` here is a
reasonable future extension, explicitly out of scope now — `review_queue`'s
checks-to-review list already gets the same capability for free, since its
underlying data happens to include `txn_id` today.

**Named exception — `query_transactions` gains a `month` parameter (one of
the TWO MCP tool schema changes in this design — the other being
`top_merchants`'s `data["month"]` addition, named below):** `query_transactions` today has no
way to scope to a past calendar month. Its only date filter, `days`, is a
lower-bound-only lookback (`t.posted_date >= today - days`, `tools.py`) with
no upper bound, and its `ORDER BY posted_date DESC LIMIT` (default 50, max
500/`ROW_CAP`) returns the most-recent rows first. Drilling from a category
row in a PAST month's breakdown — this doc's own June example, with a July 5
system date — cannot be correctly scoped by `days` alone. Because `days` is
lower-bound-only (`t.posted_date >= today - days`, no upper bound), the
window always extends unbounded through today — a small `days` value can
only ever exclude EARLIER dates directly; it can never exclude later months,
since nothing bounds the query from above. So there is really just one
condition that matters here: `days` must be set large enough to reach back
far enough to include June. Once that one condition holds, the same setting
produces one of two bad outcomes depending on data volume: either later
(July) rows are included alongside June's, contaminating the result with
unrelated transactions, or — for a high-volume category — those later-month
rows fill the `ORDER BY posted_date DESC LIMIT` before the query ever reaches
back to June's older rows at all, excluding the target month entirely. Either
way, `days` cannot correctly scope a past month by itself. **Decision for v1:
add one new optional parameter,
`month: str` (format `YYYY-MM`), to `query_transactions`'s input schema,**
filtering `t.posted_date LIKE 'YYYY-MM-%'` — the same pattern already used by
`get_month_summary` and `get_category_breakdown`.

`month` and `days` must be mutually exclusive at the handler level, and the
precedence rule has to be stated precisely against `query_transactions`'s
actual code shape to be implementable, not just asserted in prose. The
handler builds `where`/`params` today as a flat sequence of FOUR independent
`if args.get(...)` blocks — `category`, `merchant`, `days`, and
`min_amount_dollars`, in that order — each appending its own clause, all
ANDed together in the final `"WHERE " + " AND ".join(where)`
(`tools.py:187-204`) — the `days` clause (`t.posted_date >= ?`,
`tools.py:194-196`) is just one of those four appends, with nothing today
that would make it aware of a sibling `month` filter. A naive same-style
addition — a fifth independent `if args.get("month"): where.append(...)`
block appended alongside the existing four — would silently AND the month
`LIKE` filter together with the `days` `>=` filter whenever a caller supplied
both, instead of month winning. **Concrete precedence rule (needs a
conditional branch, not a fifth independent `if` appended alongside the
existing four):** `month` and `days`
share one `if`/`elif` branch — `if args.get("month"):` appends only the
`t.posted_date LIKE ?` clause (and nothing else runs for `days` in that
branch); `elif args.get("days"):` keeps the existing lower-bound clause
unchanged. When `month` is provided, ONLY the month filter is ever appended
to `where`/`params` — the `days` filter is skipped entirely, regardless of
what `days` value the caller also passed. When `month` is absent, behavior
falls back to the existing `days` filter unchanged. Callers should still
only ever pass one of the two, but this branch makes month win even if a
caller sends both. This is one of TWO narrow, honest exceptions to "no MCP
tool schema changes" in this design (API Surface, corrected below) — the
other being `top_merchants`'s `data["month"]` addition, named immediately
below — both named explicitly here rather than silently listed as
scope-neutral, the same way the `txn_id` exception above is named rather
than assumed away. Nothing else about `query_transactions`'s own schema
changes: no new `data` fields on this tool, and `txn_id` remains out of
scope per the exception above.

**Named exception — `top_merchants`'s `data` payload gains a `month` key:**
`top_merchants` resolves its month via `_month_or_current(args.get("month"))`
(`tools.py`) — the same resolution already used to build the rendered
heading, `f"## Top merchants — {month}\n{rendered}"` (`tools.py`) — but
today only that `rendered` markdown carries the resolved value; `data` is
just `{"rows": rows}` (`tools.py`), with no `month` key at all. This matters
specifically when the user's original request had no explicit month:
`top_merchants(month=None)` resolves internally to the current month via
`_month_or_current(None)`, and per persona rule #6 a Level-2 drill-down must
source its `month=` argument from the parent's `data` payload only, never
reconstructed from the `rendered` heading — but for `top_merchants` today
that resolved value simply isn't present in `data` to be read. **Decision
for v1: add one new field, `data["month"]`, to `top_merchants`'s response —
the same resolved value already computed for the rendered heading, no new
computation** — matching the pattern `get_month_summary` and
`get_category_breakdown` already use (both already carry `"month"` in
`data`, `tools.py`). This is the second, and last, MCP schema exception in
this design, alongside the `query_transactions` `month` *parameter*
exception above: that one adds a field to a tool's INPUT schema; this one
adds a field to a tool's OUTPUT (`data`) schema. Both are named here rather
than silently folded into the general "no new fields" claim.

For a Level-2 drill-down whose parent list has an explicit month (
`get_month_summary`, `get_category_breakdown`, `top_merchants` — all three
take a `month` argument themselves, and per the two exceptions above all
three now carry that resolved value in `data["month"]`), the agent passes
`month=<data["month"]>` from the parent report uniformly — sourced from
`data` only, never reconstructed from the `rendered` markdown heading
(persona rule #6). `recurring_charges` has no `month` argument at all (it
scans all history, not one month), so its drill-down into
`query_transactions(merchant=X)` passes no `month` — there is no parent
month to inherit.

**Level-2 drill-down limit (fatal fix — silent truncation):**
`query_transactions`'s schema defaults `limit` to 50 and caps it at
`ROW_CAP` (500) (`_QUERY_SCHEMA`, `tools.py:179,200`), and hitting that
default silently truncates — there is no truncated/count-vs-returned
indicator anywhere in `query_transactions`'s response. This is not unique to
one tool: **three** of the v1 drill-downs already have the true parent-row
count in hand at zero extra query cost, and the same fix applies uniformly
to all three:

- `get_category_breakdown` → `query_transactions(category=X, month=Y)`: the
  parent row is `data["breakdown"][N-1]`, which carries `"n"`
  (`tools.py:163`, the same value rendered as the `#` column).
- `top_merchants` → `query_transactions(merchant=X, month=Y)`: the parent
  row is `data["rows"][N-1]`, whose SQL projects `COUNT(*) AS n`
  (`tools.py:213-216`), so `data["rows"][N-1]["n"]` is available the same
  way.
- `recurring_charges` → `query_transactions(merchant=X)`: the parent row is
  `data["recurring"][N-1]`, which carries `"occurrences": len(items)` —
  the total transaction count for that merchant across all history
  (`detect.py:65`, from `find_recurring()`) — distinct from that same dict's
  `"months"` field (`detect.py:66`, `len(months)`, a count of distinct
  calendar months, NOT a row count; using `"months"` here instead of
  `"occurrences"` would under-count and reintroduce silent truncation).

**Decision: all three of these drill-down calls MUST pass `limit=`
explicitly, set to the parent row's own count capped at `ROW_CAP`** —
`get_category_breakdown`'s drill passes `limit=min(row["n"], 500)`;
`top_merchants`'s drill passes `limit=min(row["n"], 500)`;
`recurring_charges`'s drill passes `limit=min(row["occurrences"], 500)` —
rather than leaving `limit` unset and falling back to the schema default of
50. This rule applies to exactly these three drill-downs, where the parent
row's count is already part of this design's `data` contract (Architecture
§4's rollout table, updated below).

**Corrected claim — this closes the gap for `get_category_breakdown` for any
realistic single-month category volume (bounded by `ROW_CAP`=500), not
unconditionally; `top_merchants`/`recurring_charges` get a narrower, named
guarantee still:** `get_category_breakdown`'s drill-down filters by
`t.category = ?` (`tools.py:189`) — an EXACT match — so sizing `limit` to
`min(row["n"], 500)` closes the truncation gap whenever that category has
500 or fewer transactions in the month: the query can never return more rows
than `n` when `n <= 500`, and `n` is exactly the count of rows that match. A
category with MORE than 500 transactions in a single month would still be
silently truncated at the `ROW_CAP` ceiling itself, with no
truncated/count-vs-returned indicator — an accepted, unlikely-in-practice v1
edge case, not a claim that truncation is impossible in every case. For
`top_merchants` and `recurring_charges`, the drill-down instead calls
`query_transactions(merchant=X, ...)`, whose merchant filter is
`t.merchant_norm LIKE '%X%'` (`tools.py:192-193`, uppercased) — a SUBSTRING
match, not exact — while the parent row's count (`"n"` from `top_merchants`'s
`GROUP BY merchant_norm`, or `"occurrences"` from `find_recurring()`'s exact
`merchant_norm` grouping, `detect.py:43`) is computed by EXACT `merchant_norm`
grouping. Those two counting methods only agree when the drilled-into
merchant string is not a substring of any OTHER distinct `merchant_norm` in
the data. `merchant_norm` values are truncated to roughly three words, so a
short/generic merchant string (e.g. "WALMART") can genuinely be a substring
of a different, unrelated `merchant_norm` (e.g. "WALMART SUPERCENTER"). When
that happens, `LIMIT` sized for the exact-match count can get filled by the
OTHER merchant's rows under `ORDER BY posted_date DESC`, silently pushing the
target merchant's own transactions out of the window — relocating, not
eliminating, the truncation risk for that drill-down.

**A second, independent cause — sign/category filtering, not just substring
collision:** even with no substring collision anywhere in the data, the same
truncation risk recurs for a different reason: the parent counts for these
two tools are computed over a SIGN/CATEGORY-FILTERED subset of a merchant's
rows, while the drill-down `query_transactions(merchant=X, ...)` call applies
no sign or category filter at all. `top_merchants`'s `"n"` is `COUNT(*)`
computed under a query that requires `amount_cents < 0` (`tools.py:213-216`).
`recurring_charges`'s `"occurrences"` is `len(items)` over `find_recurring()`'s
internal `_spend_rows` filter, which keeps only rows where
`categories.is_spend(r.get("category")) and r["amount_cents"] < 0`
(`detect.py:23-24,42-43,65`). But `query_transactions(merchant=X, ...)`'s
WHERE clause has no `amount_cents` sign check and no category filter tied to
the merchant match (`tools.py:187-206`) — it returns every row matching the
merchant substring regardless of sign or category. Concretely: a merchant
with 3 spend charges and 1 same-merchant refund/credit has a parent count of
3 (the refund is excluded from both `top_merchants`'s `COUNT(*)` and
`find_recurring`'s `_spend_rows`), so the drill-down is sized `limit=3` — but
the unfiltered `query_transactions` call matches all 4 rows, and
`ORDER BY posted_date DESC LIMIT 3` can silently drop one of the merchant's
own spend rows in favor of the refund row. No substring collision is needed
for this failure mode — it compounds with, rather than replaces, the
substring-collision cause above; either can independently produce the same
truncation symptom for these two tools.

**Accepted, named v1 limitation, not a new mechanism to build:** this fix
closes the truncation gap for `top_merchants`/`recurring_charges` only when
BOTH (a) the drilled-into merchant string is not a substring of another
distinct `merchant_norm` in the data, AND (b) the merchant's own transaction
history contains no refunds/credits or other non-spend rows that the parent
aggregation excluded but the unfiltered drill-down query would include; it
does not eliminate either risk in general for those two tools. A full fix
would require both an exact-match option on `query_transactions`'s `merchant`
filter (e.g. a `merchant_exact` parameter or an equality mode) AND a
sign/category-filter option matching the parent aggregation (e.g. a
`spend_only` flag) — both explicitly out of scope for v1, named here as
future work rather than silently assumed away. This is a claim-accuracy
correction only; the `limit=` fix itself ships as specified above.

**Why `get_month_summary` is the one legitimate exception, not an arbitrary
omission:** `get_month_summary`'s numbered section is `spend_by_category`, a
`{category: spent_cents}` dict (see the named exception above) — it carries
no per-category transaction count anywhere in its `data`. Adding one would
mean introducing a brand-new field to this tool's response, which is exactly
the kind of schema growth this design otherwise avoids as a general rule,
save for the two named schema exceptions already called out above
(`query_transactions`'s `month` parameter and `top_merchants`'s
`data["month"]` field) — neither of which is a new count field, so adding
one here would still be a THIRD, uncovered exception, not an instance of
either existing one (API Surface's corrected "two named exceptions"
framing). The three tools above get the fix
for free because the count was already present in their existing `data`;
`get_month_summary` would not get it for free, so it is deliberately left
out of this fatal-fix rule rather than silently forgotten. Its drill-down
(`query_transactions(category=X, month=<parent's month>)`) therefore still
relies on the schema default `limit` of 50 in v1, with the same silent-
truncation exposure this fix closes for the other three — an accepted,
named v1 gap for this one tool, not a general claim that the risk is
eliminated everywhere.

**Invalidation invariant (applies to every row above):** the row-N ⇔
`data[N-1]` correspondence is only guaranteed against the exact
`rendered`/`data` pair returned by the call that produced it. It is NOT
guaranteed to still hold after any write tool call (`set_merchant_category`,
`set_txn_category`, `remove_category`, `split_subscriptions`, or any other
write) that could have added, removed, merged, or reordered the underlying
rows of ANY numbered list, not only the list the write was resolved against
— see Decision 2 for why the scope is deliberately broad. Any such write
invalidates EVERY numbered list rendered before it, for further row
references — the agent must re-call the source tool and re-render before
honoring another bare-number reference against any of those lists. This is a
hard rule, stated identically here, in the persona rule (Architecture §3),
and in Error Handling — not a heuristic left to model judgment.

**Re-render is necessary but not sufficient — the confirmation step must
flag the switch.** Re-calling the source tool and re-printing its `rendered`
block is not, by itself, a guarantee that the user's original number gets
matched to the right row: the user typed that number while looking at the
OLD numbering, and if the invalidating write reordered, merged, or removed
rows, the FRESH list's row N can be a different underlying row than the one
the user meant. A generic write-confirmation prompt ("confirm?") is only a
coincidental backstop here, not a designed one — a user who doesn't
carefully read the resolved label could confirm the wrong write. **Decision:
when a numbered list is invalidated and must be re-rendered before resolving
a reference against it, the write-confirmation step (persona rule #4) must
EXPLICITLY FLAG that the list changed** — e.g. "the list changed since you
last saw it — I'm treating '3' as referring to `<resolved merchant/
category>`, from the refreshed list — confirm?" — rather than a generically-
worded confirmation that doesn't call out the reshuffle. This makes the
potential mismatch something the user is actively prompted to notice, not
something a generic confirmation might let slide past. This flagged wording
applies only to the confirmation that immediately follows a re-render
triggered by invalidation; it does not change the confirm-before-write rule
itself (persona rule #4 still applies to every write, flagged or not) — see
the worked example in Data Flow (example 6, the "3 → Groceries" step).

**Invalidation scope caveat (accepted v1 limitation):** invalidation as
specified only covers writes made THROUGH THE AGENT, via an MCP write tool
call, in the current conversation. It has no way to observe an out-of-band
write — e.g. the user running a CLI import or recategorize command mid-
conversation, which is a normal part of this app's documented workflow
(`budget-setup`: "load data via the CLI, then hand off to categorize"). If
that happens, numbered lists rendered before that point are NOT
automatically invalidated, because the agent has no signal that the
underlying data changed. Recommended mitigation: the agent should re-render
proactively whenever it has reason to believe data may have changed (e.g.,
the user mentions having just run a CLI command) — but this is best-effort,
not a hard guarantee, and is an accepted v1 limitation, not something this
design can close.

### 3. Persona rule (new — `budget-analyst` rule #6)

> When you print a numbered list, a follow-up reply that references a row —
> a bare number, "#2", "the second one", or a phrase matching a shown row's
> label (case-insensitive substring or exact match; no fuzzy matching in
> v1) — means: re-read the `data` payload from that list's actual tool
> response earlier in this conversation (never reconstruct the mapping from
> memory of the printed markdown table), look up row N there, and call the
> matching drill-down tool for it — unless that list is terminal/read-only
> (no drill-down tool; e.g. `query_transactions`), in which case say plainly
> that there's nothing further to drill into rather than silently doing
> nothing or erroring (see Error Handling). **A distinct, second case: the
> drill-down/write tool for this row EXISTS somewhere in the system, but is
> not in the CURRENT skill's own `tools:` frontmatter manifest** — e.g.
> `budget-setup` renders `review_queue`'s numbered merchant rows (its
> `tools:` are `[get_month_summary, review_queue]`), but `set_merchant_category`/
> `set_txn_category` are not among them, consistent with that skill's own
> documented "performs no writes of its own" design. This is NOT the same
> as the terminal-list case above — the tool is not absent from the system,
> it is absent from THIS skill's authorization — so do not say "there's
> nothing further to drill into." Instead say so and redirect: e.g. "I can
> show you what's uncategorized, but categorizing needs the budget-categorize
> skill — want me to hand off?" rather than attempting the write (which isn't
> even reachable — Claude Code's own per-skill tool-availability restriction
> limits a skill to only the tools declared in its own `tools:` frontmatter,
> making the call unavailable at runtime; this is not something the
> tools-exist lint enforces — see the corrected framing in Architecture §3)
> or silently doing nothing (see Error
> Handling). A row's "label field" is not
> always singular: when a table has more than one plausible text column
> (e.g. `query_transactions`'s rendered rows carry a Category, a Merchant,
> AND a Type column — `Type` is `t.txn_type`, e.g. "debit"/"check", a short
> text value like the other two, not excluded by the numeric-column
> carve-out below), phrase-matching checks ALL of that row's text columns — any
> match on any text column counts, not just one arbitrarily-picked column.
> Non-text columns (e.g. a formatted Amount figure) are excluded from
> phrase-matching on that basis alone — a dollar amount is a number, not a
> text label — consistent with this same general rule (see also the
> `review_queue` Amount carve-out in Architecture §4, which follows from
> this same principle, not a separate one). This also settles
> `query_transactions`'s two remaining rendered columns, `Date` and `Acct`
> (`_txn_table`, `tools.py:120-124`): both are technically text strings, not
> numbers, but neither is a plausible phrase-match target in practice — a
> bare date (e.g. "2026-06-14") or a 4-digit account suffix (e.g. "4521")
> essentially never satisfies "substantially just a row reference with no
> other plausible reading" any more than a dollar amount does (a date is
> usually just a date being discussed, an account digit string usually part
> of a longer number). They're excluded by the same false-positive guard
> that rules out dollar amounts, not by a separate text/non-text carve-out —
> no special-case code is needed for either column. Only
> apply this when the message is substantially just a row reference with no
> other plausible reading — e.g. "I paid $2 extra for shipping" or "it was
> about 50 bucks" are dollar amounts, not row references, even right after a
> numbered list was shown, and must NOT trigger this rule. Relative ordinals
> beyond "the second one" (e.g. "the last one") resolve against the list's
> actual current length at reference time, not a fixed position.
>
> If a message contains more than one row reference against the same list
> (e.g. "1 → Dining Out, 2 → Shopping"), resolve ALL of the references to
> their underlying identity (merchant string, `txn_id`, category, etc.) from
> the SAME `data` payload — captured before any write in the batch executes
> — then issue the write calls keyed by that identity, never by row
> position. This matters because a write can shift every later row's
> position; a second write keyed by position could silently hit the wrong
> row. **This identity-resolution step is orthogonal to, and does not
> override, the existing confirm-before-write rule** (persona rule #4;
> `budget-categorize` SKILL.md's "confirm each write"): it fixes WHAT gets
> written — the correct identity, not a stale row position — never WHETHER a
> confirmation is required. A terse row-reference message like "1 → Dining
> Out, 2 → Shopping" is the user's INTENT to categorize, not the
> confirmation itself — the agent still shows the resolved, human-readable
> proposed change(s) (e.g. "STARBUCKS #4521 → Dining Out, TARGET → Shopping
> — confirm?") and waits for an explicit "yes" before calling either write
> tool, exactly as rule #4 already requires for every other write in this
> app.
>
> Once ANY write call has been issued (in this message or an earlier one),
> EVERY numbered list rendered before it is invalidated for further row
> references — not only the list the write was resolved against (a write
> can change sort/filter values, like per-category totals, that a different
> earlier list was ordered by). Re-call the source tool and re-print its
> `rendered` block before honoring another bare-number reference against any
> list rendered before that write. Because the user's original number was
> read against the OLD numbering, and the write may have reshuffled rows,
> a generic "confirm?" is not enough here: the write-confirmation prompt
> that follows this re-render must EXPLICITLY FLAG that the list changed
> and name the resolved row — e.g. "the list changed since you last saw it
> — I'm treating '3' as referring to `<resolved merchant/category>`, from
> the refreshed list — confirm?" — rather than a generically-worded
> confirmation that doesn't call out the reshuffle, so the potential
> mismatch is something the user is actively prompted to notice rather than
> something a bare confirmation might let slide past. This is required
> specifically for the confirmation immediately following an invalidation
> re-render; it supplements, and does not replace, the ordinary
> confirm-before-write rule (persona rule #4).
>
> A back-reference ("go back", "show that again") means: re-call the same
> tool that produced the parent list and re-print its `rendered` block
> verbatim — never reconstruct it from memory. If there is no parent list to
> return to — the very first interaction, or "back" said again after already
> reaching the root of the drill-down chain — say so plainly (e.g. "there's
> nothing to go back to — you haven't drilled into anything yet") rather
> than erroring or guessing at some other list to show.
>
> If you cannot actually locate the cited tool response in your visible
> context — most likely because it has fallen out of context due to
> compaction in a long conversation — do NOT guess or reconstruct the row
> mapping from a summary or from memory of the printed table. Say plainly
> that you can no longer see the original data and offer to re-render the
> list, rather than silently proceeding on an unverifiable assumption. There
> is no fallback reconstruction path in v1; re-rendering is the only
> recovery.
>
> "Ambiguous" means precisely one of two things — a natural generalization
> of the same idea, not two separate mechanisms: (1) two or more numbered
> lists were rendered in the SAME response, and the reference has no
> natural-language disambiguator (e.g. no category/merchant language in the
> message that matches one list's row labels but not the other's); or (2) a
> phrase-match reference matches MORE THAN ONE row within the SAME numbered
> list (e.g. two `review_queue` checks-to-review rows sharing an identical
> `merchant_norm` label, since `manual.checks_to_review()` has no `GROUP BY`
> and lists one row per transaction — a phrase like "the landlord check" can
> match both). A bare number or ordinal reference is never ambiguous in
> sense (2): row positions are unique within a single list by construction,
> so only phrase-match references are ever at risk of a same-list
> collision. In either case, that is when you ask instead of guessing — the
> first of the two carve-outs to "most recent list always wins" (Decision
> 2); invalidation, above, is the second.

This rule is added to `.claude/skills/budget-analyst/SKILL.md` alongside the
existing five rules.

**Corrected claim — this design is NOT per-skill-change-free.** The persona
rule itself needs no per-skill copy (every `budget-*` skill already
references the `budget-analyst` persona), but the drill-down capability it
describes only works if a skill's own `tools:` frontmatter manifest actually
lists the drill-down target tool — otherwise the agent has no authorization
to call it. That authorization boundary is enforced at runtime by Claude
Code's own per-skill tool-availability restriction, which limits a skill to
only the tools declared in its own `tools:` frontmatter — NOT by this
project's tools-exist lint (`tests/test_skills_lint.py`). That lint is a
static, CI-time check with no visibility into live conversations: it only
verifies that each tool NAME listed in a SKILL.md's frontmatter actually
exists in the tool registry (catching typos/renames), and it does not check
the inverse — whether a skill's prose-described drill-down capability
actually matches what's declared in its own `tools:` list. That manual-sync
gap is exactly what checking the actual `tools:` manifests against
Architecture §4's rollout table surfaces: five of the eight v1-rollout
skills render a numbered list whose Level-2 drill-down target is
`query_transactions`, but do not declare `query_transactions` in `tools:`
today:

- `budget-budgets` (`tools: [budget_overview, get_category_breakdown,
  set_budget_limit, clear_budget_limit, set_expected_income]`)
- `budget-income` (`tools: [income_by_source, income_transactions,
  get_month_summary]`)
- `budget-setup` (`tools: [get_month_summary, review_queue]`)
- `budget-monthly-brief` (`tools: [get_month_summary, get_category_breakdown,
  insights, monthly_trend, find_anomalies, recurring_charges, save_brief]`)
- `budget-subscriptions` (`tools: [recurring_charges, subcategory_breakdown,
  get_category_breakdown, split_subscriptions, set_budget_limit]`)

Each of these five needs exactly one mechanical addition — `query_transactions`
appended to its `tools:` frontmatter list — to keep each skill's declared
tool authorization (and hence what Claude Code actually allows it to call)
in sync with the drill-down capability its own prose describes. The
tools-exist lint would already pass either way, since `query_transactions`
is a valid, registered tool name — this sync is a manual fix this design
calls for, not something the lint enforces or would catch if skipped. This
is a small, explicitly-scoped addition (one line in each of five files), not
a broader per-skill rewrite;
no other content in these five `SKILL.md` files changes. Only `budget-coach`
(`tools: [get_month_summary, get_category_breakdown, query_transactions,
compare_periods, top_merchants]`) and `budget-categorize` (`tools:
[review_queue, query_transactions, set_merchant_category, set_txn_category,
add_custom_category]`) already declare `query_transactions` and are
self-consistent as-is today. The eighth rollout skill, `budget-reconcile`
(`tools: [open_conflicts]`), renders no numbered list with a
`query_transactions` drill-down target in this design's rollout table, so it
needs no frontmatter change here. **Implementation checklist item:** add
`query_transactions` to the `tools:` frontmatter of `budget-budgets`,
`budget-income`, `budget-setup`, `budget-monthly-brief`, and
`budget-subscriptions` as part of landing this design — this fix does not
make that change itself, it only documents that the change is required.

### 4. Rollout scope (v1) — tools that opt into `numbered=True`

| Tool | Numbered list | Drill-down target |
|---|---|---|
| `get_month_summary` (bars section) | category rows (dict; see invariant exception) | `query_transactions(category=X, month=<parent's month>)` — **no `limit=` fix**: `spend_by_category` carries no per-category count to derive one from; the ONE accepted exception to the fatal-fix rule (Architecture §2) |
| `get_category_breakdown` | category rows | `query_transactions(category=X, month=<parent's month>, limit=min(<that row's "n">, 500))` — explicit `limit` derived from the parent row's own count, MUST be passed to avoid the schema-default-50 silent-truncation case (Architecture §2's fatal-fix rule) |
| `query_transactions` | transaction rows | terminal, read-only — no recategorize-by-row in v1 (`data["rows"]` has no `txn_id`; see invariant exception) |
| `review_queue` — uncategorized merchants | merchant rows | `set_merchant_category(merchant_norm=X)` — `X` is `data["merchants"][N-1]["merchant"]` (data key is `"merchant"`; that value is passed as the `merchant_norm=` tool argument), by row reference instead of retyped merchant string |
| `review_queue` — checks to review | check rows | `set_txn_category(txn_id=X)` (row's `data["checks"][N-1]["txn_id"]` — already present, no schema change) |
| `top_merchants` (bars section) | merchant rows (`data["rows"][N-1]["merchant_norm"]`) | `query_transactions(merchant=X, month=<data["month"]>, limit=min(<that row's "n">, 500))` — `month` sourced from `top_merchants`'s own `data["month"]` field, the named schema exception in Architecture §2 (added precisely so this value is recoverable from `data` without reading the `rendered` heading); explicit `limit` derived from the parent row's own `COUNT(*) AS n`, MUST be passed per Architecture §2's fatal-fix rule; closes the truncation gap only when `X` isn't a substring of another distinct `merchant_norm` AND the merchant has no refund/credit rows excluded from the parent count (Architecture §2's corrected claim — the drill-down filter is a substring `LIKE` with no sign/category filter, while the parent count is exact-match AND spend-only) |
| `recurring_charges` | recurring merchant rows (`data["recurring"][N-1]["merchant"]`) | `query_transactions(merchant=X, limit=min(<that row's "occurrences">, 500))` — no `month`: `recurring_charges` has no month argument of its own (scans all history), so there is no parent month to inherit; explicit `limit` derived from the parent row's own `"occurrences"`, MUST be passed per Architecture §2's fatal-fix rule; same substring-match AND sign/category-filter caveats as `top_merchants` above (Architecture §2's corrected claim) |

`review_queue` renders TWO separate numbered lists in one response
(merchants, then checks). Per Architecture §3, if the user's next message
references a row with no disambiguator, that's the same-turn multi-list
ambiguity case: the agent asks which table is meant ("merchant row or check
row?"). A message that names a merchant matching a row's label in the
merchants table (e.g. "Starbucks" matching `data["merchants"][N-1]
["merchant"]`) is itself a natural disambiguator and resolves without
asking — the same label-match rule as persona rule #6, applied here to
disambiguate which table rather than which row within it. Amount-based
disambiguation (e.g. "the check for $200") is NOT a supported
disambiguator in v1: checks-to-review rows have no dedicated label field
for amount (Amount is a separate rendered column, not a label field under
rule #6's definition), so a bare dollar amount does not resolve the table
ambiguity — the agent still asks.

Separately from which TABLE a reference belongs to, the checks-to-review
table itself is the one v1 rollout list built by `manual.checks_to_review()`
with no `GROUP BY` — one row per transaction, not per merchant — so two
checks written to the same payee produce two rows sharing an identical
`merchant_norm` label. A phrase-match reference (e.g. "the landlord check")
matching more than one row within that single table is the same-list,
multi-row ambiguity case (Decision 2, persona rule #6, Error Handling): the
agent asks which row is meant rather than guessing a `txn_id`. Bare
number/ordinal references into the checks table are unaffected, since row
position is unique within the list regardless of any label duplication.

**Accepted, named v1 limitation — composed-brief skills make bare-number
resolution the LESS likely outcome, not an edge case:** two of the eight
v1-rollout skills print more than one numbered list in a single response by
design:

- `budget-monthly-brief` renders THREE numbered lists in one turn, every
  turn: `get_month_summary`'s numbered category-spend bars,
  `get_category_breakdown`'s numbered category rows, and
  `recurring_charges`'s numbered recurring-merchant rows (`SKILL.md`'s fixed
  compose order: month summary → category breakdown → trend → insights →
  anomalies → recurring charges).
- `budget-setup` Step 2 renders TWO numbered lists in one turn:
  `get_month_summary`'s numbered category-spend bars and `review_queue`'s
  numbered merchant rows (`SKILL.md` Step 2: "call `get_month_summary`...
  then call `review_queue`").

Both skills' manifests gain `query_transactions` specifically to enable
Level-2 drill-down (Architecture §3's five-skill frontmatter fix) — but for
these two skills specifically, a BARE number or ordinal alone will usually
trigger the cross-list "which list?" clarifying question (Decision 2;
Architecture §3; Error Handling) rather than resolving directly, because
that carve-out fires whenever two or more numbered lists were rendered in
the SAME response with no natural-language disambiguator, and these two
skills structurally satisfy that condition on nearly every turn. The
existing natural-language-disambiguator mechanism — a phrase naming a
category/merchant/label unique to exactly one of the rendered lists, e.g.
"the Subscriptions breakdown" (uniquely matching `recurring_charges`'s
list) or "Home Improvement" (uniquely matching a `get_category_breakdown`
row) — still resolves cleanly without asking, exactly as it already does
for `review_queue`'s own dual-table case above; only a BARE number/ordinal
with no such phrase is affected. Practical guidance for users of these two
skills: name what you mean rather than typing a bare number, for a clean
single-turn resolution — a bare number will just prompt a quick clarifying
question instead of misfiring or guessing wrong. This is an accepted v1
trade-off of composing multiple reports in one response, not a bug: the
`query_transactions` wiring is still correct and still useful once the
user's phrasing (or the agent's clarifying question) narrows to one list —
it is simply not usually reachable via a bare number ALONE on the very
first follow-up turn for these two skills, unlike single-numbered-list
skills (e.g. `budget-budgets`, `budget-income`) where a bare number resolves
directly without any disambiguator needed.

`subcategory_breakdown` is out of scope for v1's drill-down chain entirely
(Decision 4) — it does not opt into `numbered=True` and has no trigger
condition anywhere in this design; nothing else in the tool surface
changes.

## Data Flow (example)

1. User: "spending report for June" → `get_category_breakdown(month="2026-06")`,
   numbered rows 1–15.
2. User: "2" → agent resolves row 2 from the last numbered list's `data`,
   where `data["breakdown"][1]` = `{"category": "Large Purchases", "spent":
   ..., "n": 87}` → `query_transactions(category="Large Purchases",
   month="2026-06", limit=min(87, 500))` — i.e. `limit=87` — (the parent
   list's own month, per the named `month` exception in Architecture §2,
   NOT `days`, which cannot correctly scope a past month; AND the parent
   row's own `"n"` count passed as an explicit `limit`, per Architecture
   §2's fatal-fix rule, so a Large Purchases month with more than 50
   transactions is never silently truncated by the schema default) →
   numbered transaction rows (terminal level; no further numbering needed
   since these are individual transactions).
3. User: "back" → agent re-calls `get_category_breakdown(month="2026-06")`
   (the parent list's exact original call) and re-prints it verbatim.
4. User: "1" → row 1 this time (Home Improvement) → drills again.
5. **Worked example — same tool, different args (no special-casing needed):**
   User: "categories for May" → `get_category_breakdown(month="2026-05")`,
   rows 1–12. User: "and April" → `get_category_breakdown(month="2026-04")`
   — a brand-new numbered list (fresh `data`/`rendered` pair), rows 1–10.
   User: "3" → resolves against the April list (the most recently rendered
   one), never May, even though both calls used the same tool. This is a
   plain instance of "most recent list wins" (Decision 2) — each tool call
   produces its own `data`/`rendered` pair regardless of whether the args
   changed.
6. **Worked example — write invalidates a list (the review_queue bonus case,
   Decision 5):** In this scenario, the checks-to-review table is EMPTY —
   `review_queue()` renders only the merchants table, so there is exactly
   one numbered list in play and no cross-list ambiguity question is
   triggered (Architecture §4's dual-table ambiguity rule applies only when
   BOTH tables have rows; stated explicitly here so this example doesn't
   appear to silently override that rule). User: "review queue" →
   `review_queue()`, merchants numbered 1–6. User: "1 → Dining Out, 2 →
   Shopping" → agent resolves BOTH rows against the one `data["merchants"]`
   snapshot from that call (row 1's `"merchant"` key = "STARBUCKS #4521",
   row 2's = "TARGET") — this resolves WHAT would be written (the correct
   identity, not a stale row position); it does not itself satisfy the
   confirm-before-write rule (persona rule #4, unchanged by this design).
   The agent proposes the resolved changes in plain language — "STARBUCKS
   #4521 → Dining Out, TARGET → Shopping — confirm?" — and waits. User:
   "yes" → ONLY THEN does the agent call
   `set_merchant_category(merchant_norm="STARBUCKS #4521",
   category="Dining Out")` and `set_merchant_category(merchant_norm="TARGET",
   category="Shopping")` — keyed by merchant string, not position, so the
   first write shifting the list can't corrupt the second. User then:
   "3 → Groceries" → every numbered list rendered before this point
   (here, just the one `review_queue` list) is now invalidated, because a
   write happened since it was rendered; the agent re-calls
   `review_queue()`, re-prints it, and resolves "3" against the FRESH list
   (never the original numbering) — but because the user typed "3" while
   looking at the OLD numbering, and the two writes just executed may have
   reordered or removed rows, the agent's write-confirmation prompt for
   this step must EXPLICITLY FLAG that the list changed and name the
   resolved row, rather than asking a generic confirmation: e.g. "the list
   changed since you last saw it — row 3 is now different than it was
   before; I'm treating '3' as referring to WELLS FARGO CHECK #204 →
   Groceries, from the refreshed list — confirm?" This flagged wording is
   required specifically because this reference follows an invalidating
   write (Architecture §2/§3, Error Handling); a bare "confirm?" that
   doesn't call out the reshuffle is not sufficient here. The agent then
   waits for an explicit "yes" before calling `set_merchant_category` —
   the confirmation gate applies identically to every write in this chain,
   not just the first, but this particular confirmation carries the extra
   invalidation flag the earlier ones didn't need.

## Error Handling

- **Ambiguous reference** (carve-out 1 of 2) — a natural generalization
  covering either of two situations, both resolved the same way: **(a)
  cross-list** — two or more numbered lists were rendered in the SAME
  response (e.g. `review_queue`'s merchants + checks tables, or two
  unrelated tools like `get_category_breakdown` and `top_merchants` both
  called in one turn), and the user's reference has no natural-language
  disambiguator that matches one list's row labels but not the other's; or
  **(b) same-list, multi-row** — a phrase-match reference matches MORE THAN
  ONE row within the SAME numbered list (e.g. `review_queue`'s
  checks-to-review table is built by `manual.checks_to_review()`, which has
  no `GROUP BY` and lists one row per transaction, so two checks written to
  the same payee produce two rows sharing an identical `merchant_norm`
  label — a phrase like "the landlord check" can match both, with no
  defined tie-break otherwise). A bare number or ordinal reference is never
  subject to case (b): row positions are unique within a single list by
  construction. In either case, ask which row/list is meant — e.g. for (b),
  ask which of the matching rows (and hence which `txn_id`) the user means —
  rather than guessing. This is not a general session-long fuzziness rule,
  and it does not mean references expire with time.
- **List invalidated by a write** (carve-out 2 of 2): once any write tool
  call has executed (`set_merchant_category`, `set_txn_category`,
  `remove_category`, `split_subscriptions`, or any other write), EVERY
  numbered list rendered before it — not only the list the write was
  resolved against — is invalid for further row references (Decision 2;
  Architecture §2's invalidation invariant is deliberately scoped this
  broadly). Re-call the source tool and re-render before honoring another
  bare-number reference against any of those lists — never reuse a row
  position from before the write. Re-rendering alone is not a sufficient
  guard, though: the user's original number was read against the list as it
  looked BEFORE the write, and the write may have reordered or removed rows,
  so the fresh list's row N can be a different underlying row than intended.
  The write-confirmation prompt for this specific step — the one that
  follows an invalidation re-render — must therefore EXPLICITLY FLAG the
  switch (e.g. "the list changed since you last saw it — I'm treating '3'
  as referring to `<resolved merchant/category>`, from the refreshed list —
  confirm?"), not a generically-worded "confirm?" that doesn't call out the
  reshuffle — see Architecture §2/§3 and Data Flow worked example 6. This
  tracking only covers writes made THROUGH THE AGENT in this conversation —
  an out-of-band CLI write (import, recategorize) mid-conversation is
  invisible to it; see the invalidation scope caveat in Architecture §2
  (accepted v1 limitation, best-effort proactive re-render only).
- **No parent list to go back to**: "back" on the very first interaction, or
  "back" said again after already reaching the root of the drill-down chain,
  has no parent list to return to. Say so plainly (e.g. "there's nothing to
  go back to — you haven't drilled into anything yet") rather than erroring
  or guessing at some other list to show (persona rule #6).
- **Terminal-list bare-number reference**: `query_transactions`'s numbered
  rows are read-only/terminal — there is no drill-down tool for them
  (Architecture §2 named exception). A bare-number reference against a
  terminal list has nothing further to drill into: say so plainly (e.g.
  "that row is already fully shown above — there's no additional detail to
  drill into") rather than silently doing nothing or erroring.
- **Bare-number reference whose drill-down/write tool exists in the system
  but isn't authorized for the CURRENT skill**: distinct from the
  terminal-list case above — here the tool exists, it just isn't in this
  skill's own `tools:` frontmatter manifest. `budget-setup` is a case where
  this applies today: it renders `review_queue`'s numbered rows (`tools:
  [get_month_summary, review_queue]`) but does not declare
  `set_merchant_category`/`set_txn_category`, consistent with its own
  documented "performs no writes of its own" design (`budget-setup`
  SKILL.md) — so "1 → Dining Out" typed after `budget-setup` shows a
  numbered `review_queue` cannot be honored there. Say so and redirect
  rather than attempting the write or silently doing nothing: e.g. "I can
  show you what's uncategorized, but categorizing needs the
  budget-categorize skill — want me to hand off?" This is NOT a case for
  adding the write tools to `budget-setup`'s manifest — that would
  contradict its explicit no-writes-of-its-own design intent; the fix is
  graceful redirect behavior, not a scope change to `budget-setup`
  (persona rule #6, Architecture §3).
- **Stale/out-of-range reference** ("12" when the last list only had 8 rows):
  say so, don't fabricate a row.
- **No hallucinated row→target mapping**: resolving a row reference always
  means re-reading the actual `data` payload of the relevant tool response
  from the conversation history — never reconstructing the mapping from
  memory of the printed markdown table. There is no separate session-side
  state to maintain; the tool call/response pair already in the
  conversation IS the state. If that tool response is no longer actually
  visible — e.g. it fell out of context due to compaction in a long
  conversation — do NOT guess or reconstruct the mapping from a summary or
  from memory of the printed table; say so plainly and offer to re-render
  the list instead of silently proceeding (persona rule #6). There is no
  fallback reconstruction path in v1.
- **Non-numbered-list bare number / false-positive guard**: the drill-down
  rule only fires when the message is substantially just a row reference
  with no other plausible reading. Concrete negative examples that must NOT
  trigger it, even though a numbered list was shown earlier in the
  conversation: "I paid $2 extra for shipping" (a dollar amount), "it was
  about 50 bucks" (a dollar amount). If no numbered list has ever been shown
  in the conversation, never apply this rule — treat the number literally.
  There is no time-based expiry (Decision 2): the most-recent list wins
  regardless of how many turns have elapsed, unless invalidated by a write
  (above).

## Testing Strategy

- **Unit** (`tests/test_render.py` or wherever render tests live):
  - Assert `table(rows, cols, numbered=True)` prepends a 1-indexed `Row`
    column correctly, and that `Row` does not collide with any existing
    header (in particular the `#` columns on `get_category_breakdown` and
    `review_queue`'s merchant table).
  - Assert `numbered=False` (default) output is byte-identical to current
    behavior on both `table()` and `bars()` — regression guard for the
    "zero blast radius on existing callers" claim.
  - Assert, for `get_month_summary`, that `list(data["spend_by_category"]
    .keys())` order matches the line order of the numbered `bars()` output —
    the dict/bars sub-invariant from Architecture §2 must be checkable, not
    assumed.
- **Skill eval reality check (read this before the two lists below):**
  `EvalSpec` (`tests/evals/specs.py`) has a single `prompt: str` field, and
  `scripts/eval.py` spawns exactly ONE `claude -p <prompt>` subprocess per
  spec (`build_command`) — there is no session-resume or multi-turn
  mechanism anywhere in the harness. Nearly all of the interesting drill-down
  scenarios are inherently multi-turn: a numbered list is rendered in one
  agent response, then a LATER, separate user message references it. None of
  the six existing `family_checks` (`tool_call`, `no_pii`, `confirm_gate`,
  `no_write`, `structure`, `invention`) can assert "the agent asked a
  clarifying question" either. The list below is split honestly along that
  line rather than assuming the harness already supports all of it.

  - **Testable today** (single `claude -p` invocation; no harness changes):
    a single prompt CAN legitimately exercise the resolution logic if it
    asks the agent to render a list and act on a reference to it within the
    same turn — the agent still makes its own real tool calls and reads its
    own real `data` payload from within that one turn (`--max-turns 12`
    already allows multiple internal tool calls per invocation); this is a
    weaker proxy than an organic later message (the script is spelled out
    up front rather than reacted to), but it is honestly testable now:
    - Does a numbered list get rendered correctly for a given tool call at
      all (e.g. "show me June's category breakdown" → asserts the call
      happened and the rendered `Row` column is present) — the closest
      today-testable approximation of the happy path's first hop.
    - Phrase-match resolution within one message that already contains both
      the render request and the reference (e.g. "show me June's category
      breakdown, then drill into whichever row is labeled 'Home
      Improvement'") — **what's actually checkable today, verified against
      `harness.py`:** `tool_call_ok`/`called_tools` only checks that bare
      tool NAMES are a subset of what was called — it discards all call
      arguments entirely. So this spec can only assert "`query_transactions`
      was called" (a name-presence check), NOT "`query_transactions` was
      called with `category="Home Improvement", month="2026-06"`" — asserting
      the specific arguments would need a NEW `family_check` that inspects
      `tool_calls[i]["input"]`, which does not exist today (same
      harness-extension follow-up named below, not a second separate ask).
    - Out-of-range handling within one message (e.g. "...then tell me
      about row 20 of that list") — **what's actually checkable today:** no
      existing `family_check` asserts an exact/bounded call set. `no_write`
      only checks the absence of WRITE-prefixed tool names (`_WRITE_PREFIXES`/
      `_WRITE_EXACT`), not the absence of extra READ tool calls, so "no
      further tool call fires" is NOT actually checkable today either. What
      IS checkable today: no WRITE tool fired (`no_write`), or that a
      specific named tool is absent from `called_tools` if the spec checks
      for that directly. Asserting a true exact/bounded call set (no
      additional tool call of ANY kind beyond a named set) would need a NEW
      `family_check`, not present today (same harness-extension follow-up
      named below).
  - **Requires harness extension** (genuine multi-turn: a separate LATER
    user message referencing a list rendered earlier; not authorable today
    with a single `prompt: str`):
    - **Happy path chain**: "show June categories" → "2" → "back" → "1",
      across four separate turns, asserting the agent calls the correct
      tool with the correct args at each step and never fabricates a number
      between tool calls.
    - **Same tool, different args, back to back**: two
      `get_category_breakdown` calls for different months in separate
      turns, then a bare number resolves against the second call (most
      recent), never the first (Data Flow worked example 5).
    - **Ambiguous same-turn multi-list — `review_queue`'s built-in case**:
      `review_queue()` renders both tables, then a LATER bare "2" with no
      disambiguator — asserts the agent asks instead of guessing. Also
      blocked on the missing "asked a clarifying question" family_check.
    - **Ambiguous same-turn multi-list — general case**: a turn that calls
      two unrelated tools each producing a numbered list (e.g.
      `get_category_breakdown` AND `top_merchants`), then a LATER bare "2"
      with no disambiguator — same family_check gap as above.
    - **Ambiguous same-list, multi-row — checks-to-review phrase collision**:
      `review_queue()`'s checks table contains two rows sharing the same
      `merchant_norm` (since `manual.checks_to_review()` has no `GROUP BY`),
      then a LATER phrase-match reference (e.g. "the landlord check")
      matching both rows — asserts the agent asks which row is meant rather
      than guessing a `txn_id` for `set_txn_category` (Decision 2, persona
      rule #6, Error Handling). Same family_check gap as the two cases
      above; a bare number/ordinal reference into the same table is NOT
      expected to trigger this ask, since row position is unique.
    - **Stale reference after an intervening write**: a numbered list is
      rendered, a write executes against one of its rows in a later turn,
      then a bare reference using the ORIGINAL numbering arrives in a
      further turn — asserts the agent re-renders the source list before
      resolving, rather than silently miscategorizing a shifted row (Data
      Flow worked example 6).
    - **`review_queue`'s dual-table numbering**: a LATER message with row
      references spanning BOTH tables, resolved by identity rather than
      position, asserting no cross-list bleed and correct write calls to
      each target tool (`set_merchant_category` vs. `set_txn_category`).

  **Prerequisite follow-up (named, not assumed away):** closing the
  "requires harness extension" bucket needs additions to the harness,
  tracked as a follow-up to this design, NOT part of it: (1) a
  `turns: list[str]` field on `EvalSpec` plus session-resume plumbing in
  `scripts/eval.py` so a spec can replay a sequence of user messages against
  one conversation; (2) new `family_check`s — for "asked a clarifying
  question / did not guess" (the multi-turn ambiguity cases above), for
  argument inspection (asserting a specific tool call's `input`, not just
  its bare name — needed to correctly check the "testable today" phrase-match
  claim above), and for an exact/bounded call-set assertion (asserting no
  additional tool call beyond a named set fired — needed to correctly check
  the "testable today" out-of-range claim above). These last two are not a
  second separate ask: they're the same accuracy fix as the "testable today"
  bucket's corrections above, just deferred to this one follow-up rather than
  claimed as already covered. **Until that follow-up lands, this design ships
  with unit-test coverage (render.py) + the single-turn skill evals above +
  manual verification of the multi-turn chain; full behavioral acceptance
  testing of the conversational drill-down is explicitly blocked on the
  harness extension, not silently claimed as already covered.**

  **Live-capture note (corrected against the actual harness code):**
  `scripts/eval.py`'s mock tier (`run_mock`/`discover_corpus`) only globs
  already-committed `*.jsonl` files under `tests/evals/transcripts/` — it
  makes no model calls and costs nothing, but it never reads anywhere else.
  `--capture` does NOT populate that directory: it only fires inside the
  `--live` branch and writes score fingerprints to `tests/evals/baseline.json`
  (`BASELINE_PATH`). The raw transcript itself is written unconditionally by
  every live run (`--live`, with or without `--capture`) to
  `tests/evals/.runs/<corpus_key>.jsonl` (`RUNS_DIR`) — a separate directory
  that `discover_corpus`/`run_mock` never globs. There is no code path
  anywhere in `scripts/eval.py` that copies a file from `tests/evals/.runs/`
  into `tests/evals/transcripts/`; promoting a captured run into the
  committed mock corpus is a manual step this script does not perform.
  This applies only to the "testable today" bucket above. The actual
  required steps to get a spec into the mock-tier corpus: (1) author the
  spec in `tests/evals/specs.py`; (2) run `scripts/eval.py --live --capture`
  once, which records the raw transcript to
  `tests/evals/.runs/<corpus_key>.jsonl` and the fingerprint to
  `tests/evals/baseline.json`; (3) manually copy/rename that file from
  `tests/evals/.runs/<corpus_key>.jsonl` to
  `tests/evals/transcripts/<corpus_key>.jsonl` and `git add` it — only once
  it lands there does the mock tier's CI replay (`run_mock`) actually find
  it. Step (3) is a required manual copy-and-commit, not an automatic
  side effect of `--capture`. Step (2) spends real API cost, bounded by
  `--max-runs`/`--max-cost` (default 30 runs / $15) — quote and confirm the
  expected spend before running it, consistent with this project's practice
  of quoting LLM cost up front. The "requires harness extension" bucket
  cannot be captured at all until the `turns` field and session-resume
  plumbing exist.

**Minor implementation note for `bars()` (not a test, a heads-up):** the
`N. ` numbering prefix is 3 characters for rows 1–9 and grows to 4+ for row
10 and beyond. `bars()` has no fixed-width columns to misalign, and the bar
length itself comes from `width`, not the prefix, so this is cosmetic-only.
`bars()`-rendered numbered lists also have no header row labeling what the
`N. ` prefix means (unlike `table()`'s `Row` header) — accepted cosmetic
debt alongside the prefix-width note, both for v1. This applies to both
v1-rollout tools that render via `bars()`: `get_month_summary` and
`top_merchants` (bars section) — neither gets a `Row`-style header, unlike
every `table()`-based tool in the rollout.

## API Surface

```python
# src/local_budget/agent/render.py
def table(rows: list[dict], cols: list[tuple[str, str]], *, numbered: bool = False) -> str: ...
def bars(items: list[tuple[str, int]], *, width: int = 20, numbered: bool = False) -> str: ...
```

```python
# src/local_budget/agent/tools.py — _QUERY_SCHEMA, the one MCP INPUT-schema change
# (Architecture §2 named exception): adds "month". Handler-side, "month" and
# "days" share one if/elif branch (NOT a fifth independent if appended
# alongside the existing four where.append() calls) so that when "month" is given,
# the "days" clause is skipped entirely rather than ANDed in; see Architecture
# §2 for the precise precedence rule against the handler's actual code shape.
"month": {"type": "string", "description": "YYYY-MM; when given, days is ignored entirely (not ANDed)"},
```

```python
# src/local_budget/agent/tools.py — top_merchants, the one MCP OUTPUT
# (data-schema) change (Architecture §2 named exception): data gains "month",
# the same resolved value already used to build the rendered heading — no new
# computation, just also surfacing it in data.
return {"data": {"rows": rows, "month": month}, "rendered": f"## Top merchants — {month}\n{rendered}"}
```

Corrected claim: this design is NOT schema-change-free. `numbered=True` at
existing render call sites in `agent/tools.py` needs no schema change, and no
new MCP tools are added anywhere — but there are exactly TWO named schema
exceptions: `query_transactions`'s input schema gains the one `month`
parameter above, and `top_merchants`'s `data` payload gains the one `month`
field above (both Architecture §2). These are narrow, named exceptions, not
a pattern that repeats elsewhere in v1.

## Invariants

**Checkable by inspection:**
- `numbered` defaults to `False` on both renderers; no existing call site's
  behavior changes unless explicitly updated to pass `numbered=True`.
- Numbering never alters a dollar figure, row order, or underlying `data`
  content — purely an added index.
- The numbered-column header is `Row`, never `#` — checked against every
  existing header on every `table()`-based v1-rollout tool (Architecture
  §1); `#` is already a live transaction-count header on
  `get_category_breakdown` and `review_queue`'s merchant table. (No header
  check applies to `get_month_summary` or `top_merchants` — both render via
  `bars()`, which has no column headers at all.)

**Testable:**
- Rendered row N ⇔ `data.<list>[N-1]` for every v1 tool EXCEPT
  `get_month_summary`, where row N ⇔ the Nth entry of
  `data["spend_by_category"]` in dict-insertion order (Architecture §2).
- `query_transactions`'s numbered rows carry no `txn_id` in `data` for v1;
  they are read-only/terminal, not a recategorize-by-row target
  (Architecture §2/§4).
- `review_queue`'s checks-to-review rows DO carry `txn_id` in `data`, so
  they ARE a valid recategorize-by-row target via `set_txn_category`
  (Architecture §4).
- Any write tool call invalidates every numbered list rendered before it;
  the "stale reference after an intervening write" eval must show a
  re-render, never a silent miscategorization (Testing Strategy) — and the
  re-render's own follow-up write-confirmation must explicitly flag the
  list-changed switch (Architecture §2/§3, Error Handling; Data Flow worked
  example 6), not rely on a generic "confirm?" as the only backstop — like
  the eval-transcript-chain invariant below, this is a multi-turn scenario (a
  list rendered in one turn, a write in a later turn, then a stale reference
  in a further turn — Testing Strategy's own "requires harness extension"
  bucket) and is genuinely testable only once that harness-extension work
  lands; until then it is verified by manual conversation testing, not by an
  automated mock-tier eval.
- A phrase-match reference uses case-insensitive substring/exact match
  against ANY of the row's text columns (not a single arbitrarily-picked
  one) — no fuzzy matching in v1, and non-text columns (e.g. a formatted
  Amount figure) never count (Architecture §3).
- The eval transcript chain (category → drill → back → drill) resolves each
  step to the correct tool call without the agent inventing a figure
  between tool calls (persona rule #1 held under the new rule #6) — this
  invariant is genuinely testable only once the "requires harness extension"
  work lands (Testing Strategy); until then it is verified by manual
  conversation testing, not by an automated mock-tier eval.
