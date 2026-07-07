---
ticket: "N/A"
title: "Floor-type budget goals (Investments) + Random-category categorization guardrail"
date: "2026-07-06"
source: "design"
---

# Floor-type budget goals + Random-category guardrail

## Context

Two follow-ups were flagged while splitting the "Utilities" category and
doing a full budget-limit review (same session):

1. **Investments** needs a $3,000/mo target where spending *more* is good —
   the opposite of every other category, where spending more is bad. The
   budget system only has ceiling semantics today (`over budget = bad`);
   there's no way to represent "under target = bad."
2. **"Random"** (the catch-all category) had drifted into overuse during past
   categorization passes. The user wants future categorization work to stop
   defaulting into it.

## Dimension 1: Floor-type budget goals

### Investigation summary

Investigated via two parallel agents (Domain Researcher + Impact Analyst)
and a Challenger pass. Initial hypothesis was that this needs a schema
change (a `direction` column on the `budgets` table) — that turned out to
be unnecessary. Two real surprises came out of investigation:

- **A live web UI exists** (`src/local_budget/web/` — FastAPI + a static
  HTML/JS frontend) with hardcoded ceiling-only red/green CSS/JS, reading
  the same `budget_overview()` data. Git history shows only the initial
  scaffold commit ever touched it (17 subsequent commits of budget-logic
  work never did) — confirmed with the user to be vestigial, so it's
  explicitly **out of scope** for this change (see Known Limitations).
- **"Over budget = bad" logic appears in `reports.py`** (`budget_status()`
  computes it; `insights()` and `budget_overview()` each consume/relabel
  that already-derived value rather than recomputing it) **plus two
  independently-computed CLI checks** — `cli.py`'s `report()` and
  `subscriptions()` commands, the latter bypassing `budget_status()`
  entirely and reading `subcategory_breakdown()`'s `limit_cents` directly.
  All active call sites must become direction-aware together.

The Challenger also caught that a hardcoded Python frozenset for marking a
category as floor-type would contradict this session's own precedent —
Phone/Electricity/Gas-Propane/Internet were all added *live*, via MCP calls,
with no code deploy. Direction should be mutable the same way.

### Decision

**Storage:** a `floor_categories` settings-blob, mirroring the existing
`hidden_categories`/`custom_categories` idiom in `categories.py` exactly
(JSON array under a `db.get_setting`/`set_setting` key). Direction is a
property of the *category*, not of an individual budget row or
`effective_from` history entry — Investments will always mean "more is
better." This requires **zero schema migration** to the `budgets` table and
**zero change** to `budgets.py` (`set_limit`'s `limit_cents <= 0` guard is
untouched; `active_limits()`'s return shape is untouched).

**Logic:** a centralized pair of helpers in `categories.py` —
`off_track_delta(category, actual_cents, limit_cents) -> int` (the
direction-aware, sign-normalized magnitude, positive always meaning "bad")
and `is_off_track(...) -> bool` (a thin wrapper: `off_track_delta(...) >
0`) — that every consumer calls instead of re-deriving `actual > limit`
independently. This collapses "5 places that must change in lockstep" into
"5 call sites that call one function pair" — the next goal-type (if one is
ever needed) becomes a change to one helper, not a five-file hunt.

**Ranking:** a floor-goal miss ranks in `insights()` at the **same tier** as
a ceiling breach (both are top-billed, most-urgent alerts) — just with a
distinct `kind` (`"under_target"` vs `"over_budget"`) and label wording, so
one isn't mistaken for the other.

**Web UI:** explicitly descoped (see Known Limitations).

### API Surface

```
categories.py:
  floor_categories(conn=None) -> frozenset[str]
  mark_floor_category(name: str, conn=None) -> None
  unmark_floor_category(name: str, conn=None) -> None
  is_floor(category: str, conn=None) -> bool
  off_track_delta(category: str, actual_cents: int, limit_cents: int, conn=None) -> int
      # Direction-aware, sign-normalized over_cents: for a ceiling category,
      # actual_cents - limit_cents (unchanged); for a floor category,
      # limit_cents - actual_cents (flipped). Positive always means "bad"
      # regardless of direction. This is the one place the sign gets
      # normalized — both the boolean gate (`> 0`) and any "$X over/under
      # target" display magnitude derive from this value.
  is_off_track(category: str, actual_cents: int, limit_cents: int, conn=None) -> bool
      # Convenience wrapper: return off_track_delta(...) > 0.

agent/tools.py (new MCP tools):
  mark_floor_category(name: str) -> {"ok": true, "rendered": str}
  unmark_floor_category(name: str) -> {"ok": true, "rendered": str}
  # existing tools updated (description text only, no signature change):
  budget_overview: description gains "(floor categories flip the comparison)"
  set_budget_limit: description gains a note that direction comes from the
      category's floor/ceiling marking, not from this call
```

### Consumers updated (all call `categories.off_track_delta`/`is_off_track`, no local `>` comparisons)

- `reports.py::budget_status()` — `over_cents` is **not** already
  sign-normalized (`actual - period_limit` is negative for a floor
  shortfall, which is the bad case). Fix: `over_cents` becomes
  `categories.off_track_delta(category, actual, period_limit)` — i.e.
  `actual - period_limit` for a ceiling category (unchanged) but
  `period_limit - actual` for a floor category (flipped), so
  `over_cents > 0` uniformly means "bad" regardless of direction.
- `reports.py::insights()` — the `if b["over_cents"] > 0` gate stays
  structurally the same (it now works correctly because `budget_status()`
  feeds it an already sign-normalized value), and the `amount_cents` field
  it emits (the "$X over/under target" display magnitude) is that same
  signed `off_track_delta` value, not just the boolean. The `kind`/label
  branches on `categories.is_floor(b["category"])` to emit `"under_target"`
  copy instead of `"over_budget"` copy.
- `reports.py::budget_overview()` — the `over` boolean (category level and
  per-subcategory) computed via `is_off_track` instead of a raw `spent >
  budget`. The `subs_exceed` field (`sub_total > monthly`, `reports.py:586`)
  is explicitly **not** a direction-relative comparison and is left
  completely unchanged: `sub_total` (`reports.py:569`) sums subcategory
  *budget limits*, and `monthly` (`reports.py:560`) is the parent's *budget
  limit* — both are configuration values, not actual spend, so there is no
  "actual vs. target" direction to flip. `subs_exceed` stays plain
  `sub_total > monthly` regardless of the category's floor/ceiling marking;
  it's a budget-allocation-consistency check ("do the subcategory
  allocations sum to more than the parent's total"), not a spend-vs-target
  check, so routing it through `off_track_delta`/`is_off_track` would be
  wrong — for a floor category it would silently hide genuine
  over-allocation (e.g. Investments with `monthly=$3000` and subcategory
  budgets summing to `$3500` would incorrectly report `subs_exceed: False`).
- `cli.py` — **both** the `report` command's `OVER`/`ok` flag AND the
  previously-independent `subscriptions()` command's flag now call
  `is_off_track` instead of their own local `>` comparisons. Beyond the
  boolean, the literal label text each command prints (`report` at line
  235, `subscriptions` at line 300) must also become direction-aware — a
  hardcoded `"OVER"` string reads as overspending even when `is_off_track`
  is true for the opposite reason (a floor category under its target).
  The two commands compute that label with **different mechanisms**,
  because their row shapes differ:
  - `report()` — each row dict already carries a `category` key (from
    `budget_status()`), so the label is computed **per-row**, inside the
    loop, off that row's own `category`: `categories.is_floor(r["category"])`.
  - `subscriptions()` (`cli.py:287-303`) — its rows come from
    `reports.subcategory_breakdown("Subscriptions", month)` (`cli.py:292`),
    and `"Subscriptions"` is a hardcoded string literal at the call site,
    not a field in the returned row dicts (`subcategory_breakdown()`'s
    dicts, built from `reports.py:646` onward, contain only
    `subcategory`, `spent_cents`, `monthly_avg_cents`, `count`, `months`,
    `limit_cents` — no `category` key). A
    per-row `r["category"]` lookup would `KeyError`. Instead, the fix must
    **hoist a single `is_floor("Subscriptions")` check outside the
    per-row loop** — computed once, since the category is fixed for the
    whole command — and apply the same resulting label
    (`"UNDER"`/`"OVER"`/`"ok"`) to every row using that one value.
  Both mechanisms land on the same label vocabulary — `"UNDER"` for an
  off-track floor category, `"OVER"` for an off-track ceiling category,
  `"ok"` otherwise — mirroring the same label-confusion fix already
  applied to `insights()`'s `kind` field above, rather than a
  boolean-gated static `"OVER"`/`"ok"`.
- `agent/tools.py`'s `budget_overview` render function — inherits
  correctness automatically once `reports.budget_overview()`'s `over`
  boolean is fixed upstream (no separate change needed there beyond the
  description-text update above).
- `categorize/manual.py`'s `remove_category` merge path — guard against
  merging a floor category's budget into a non-floor category (or vice
  versa): refuse the merge if `is_floor(name) != is_floor(merge_into)`
  rather than silently summing `limit_cents` across mismatched directions.
  This check must run before any mutation in `remove_category`'s sequence;
  relying on `db.writer`'s rollback-on-exception to undo a partial merge if
  the guard fired late is fine as a safety net, but the check itself is
  ordered first so no partial state is ever attempted.
- `.claude/skills/budget-visualizer/SKILL.md` — Recipe 2 prose updated:
  the `critical`/`good` tier keys off `budget_overview`'s `over` boolean as
  before (no visualizer-side logic change), with a new clause noting that
  for a floor category, `over == false` (i.e. spend at/above target) is
  `good`, and `over == true` (under target) is `critical` — same field,
  already-correct semantics, just documented explicitly so a future report
  session doesn't re-derive the wrong assumption from the word "over."
- `.claude/skills/budget-budgets/SKILL.md` — one-line note that "over"
  means direction-relative, not universally "spent more than."
- `off_track_delta`/`is_off_track` callers inside `budget_status`/
  `insights`/`budget_overview` should pass `conn=conn` — they already hold
  an open connection while looping — rather than opening a fresh one per
  call, matching the existing `hidden_categories`/`mark_hidden`
  conn-threading idiom in `categories.py`.

### Invariants

**Checkable by inspection:**
- No `budgets.py` function signature changes (`set_limit`, `clear_limit`,
  `active_limits`, `list_limits` are all untouched).
- No `ALTER TABLE` / schema migration in `db.py`.
- Every site that previously did a local `actual > limit`/`spent > budget`
  comparison for over-budget purposes now calls `categories.is_off_track`
  instead (grep for stray `> limit_cents`/`> budget` comparisons in
  `reports.py`/`cli.py` outside the helper as a regression check).

**Testable:**
- Marking "Investments" as a floor category, then recording spend below
  the target, produces `is_off_track() == True` and an `insights()` item
  with `kind == "under_target"`.
- Recording spend at/above the floor target produces `is_off_track() ==
  False` and no alert.
- All ~24 existing ceiling-type categories are completely unaffected
  (regression suite: existing `test_insights_over_budget_and_discretionary`
  and `test_budget_status_agrees_with_overview_total_for_period` must still
  pass unchanged).
- `cli.py subscriptions()`'s flag now agrees with `is_off_track` for a
  ceiling subscription (no regression) and is exercised at least once with
  a floor-marked category to confirm it isn't hardcoded ceiling-only —
  including that the printed label reads `"UNDER"` (not `"OVER"`) for that
  floor-marked category's shortfall.
- `remove_category` merge across mismatched directions raises rather than
  silently summing.

### Known Limitations

- **Web UI (`src/local_budget/web/`) is explicitly out of scope**, and this
  has two distinct breakages once `insights()` starts emitting
  `kind: "under_target"`, not just the one previously noted:
  - The budget-bar CSS/JS (`.over` classes) will continue to show a floor
    category's "under target" state with the same styling as a ceiling
    breach, and vice versa — i.e., it will render backwards for
    Investments specifically.
  - Worse, `static/index.html`'s `renderInsight(i)` (lines 500-504,
    served live via `web/routes.py`'s `/api/insights` endpoint) has no
    branch for `kind === "under_target"` — it has branches for
    `"over_budget"` and `"subscriptions"` only, so an `under_target` item
    falls through to the final catch-all branch written for `"reduce"`-kind
    items. That branch renders the wrong icon (💸 "cut" instead of ⚠
    "warn"), the wrong copy ("discretionary, easiest to trim" — the
    opposite of the intended meaning for an under-funded floor category
    like Investments), and a broken/undefined dollar amount (it reads
    `i.monthly_cents`, a field that only exists on `reduce`-kind items;
    `under_target`/`over_budget` items only carry `amount_cents`,
    `actual_cents`, `limit_cents`, per `reports.py:282-289`). So the
    insights-flags list on that page will misrepresent a floor-category
    shortfall on three axes at once (icon, copy, amount), not just show a
    backwards color.
  Both breakages are covered by the same "web UI is vestigial, explicitly
  out of scope" decision — confirmed with the user this page is unused
  since the initial scaffold commit. Revisit both if it's ever brought
  back into active use.
- Direction cascades to *all* subcategories under a floor-marked category —
  a floor subcategory and ceiling subcategory can't coexist under the same
  parent. Nothing today needs that split; if it's ever needed, revisit
  storage grain (would need to move from category-level to per-row).
- Toggling `mark_floor_category`/`unmark_floor_category` retroactively
  changes the *meaning* of all past months' alerts for that category (no
  point-in-time history) — consistent with how `limit_cents` already
  behaves, but worth flagging explicitly since it's easy to assume
  direction is time-scoped the way an individual budget row is.
- `_pct()` (`spent / budget * 100`) is left unchanged for floor categories.
  It pairs correctly with the flipped `over` boolean, but is easy to
  misread in isolation — e.g. a floor category showing "67%, over: true"
  actually means the category is *under* its target (bad), not over it.
- `mark_floor_category` has no validation that `name` is an existing real
  category (unlike `budgets.set_limit`'s existing `category not in
  categories.all_categories()` guard) — a typo would silently no-op.

## Dimension 2: "Random"-category categorization guardrail

### Investigation summary (quick scan)

Expected a code-level auto-assignment path defaulting low-confidence
transactions to `Random`. There isn't one: `categorize/rules.py`'s
deterministic categorizer never assigns `Random` — an unmatched charge
becomes `Uncategorized` and waits for an agent-driven categorization
session (the `budget-categorize` skill) to decide. Past overuse of
`Random` was a judgment-call pattern by that agent-driven process, not a
code bug — so the guardrail has to live at both the prompt layer (where
the judgment is made) and a code-level backstop (for any path that
bypasses the skill).

### Decision

Both, per user's explicit choice:

1. **Skill-prompt fix** — `.claude/skills/budget-categorize/SKILL.md` gets
   an explicit instruction: never propose `Random` as a category; if
   nothing fits, leave the item in the review queue for the user to decide
   rather than defaulting.
2. **Code-level backstop** — `categorize/manual.set_merchant_category()`
   and `set_transaction_category()` refuse to write `category == "Random"`
   unless an explicit `confirm_random: bool = False` override is passed.
   `Random` remains in `categories.PROTECTED` (unremovable — the
   review-queue's `only_uncertain` filter still keys off it); this change
   only affects *new* writes, never historical `Random` transactions.

### API Surface

```
categorize/manual.py:
  set_merchant_category(merchant_norm: str, category: str,
                         subcategory: str | None = None,
                         confirm_random: bool = False, conn=None) -> int
      # raises CategorizeError("Random is discouraged — pick a real
      # category, or leave it in the review queue") if category == "Random"
      # and not confirm_random.
  set_transaction_category(txn_id: int, category: str,
                            subcategory: str | None = None,
                            confirm_random: bool = False, conn=None) -> None
      # same guard.

agent/tools.py:
  set_merchant_category / set_txn_category ToolSpec schemas gain an
  optional confirm_random boolean field (default false) — present for a
  deliberate override, but never set by the budget-categorize skill's
  normal flow.
```

### Consumers updated

- `categorize/manual.py`'s `set_merchant_category()` / `set_transaction_category()`
  — the guard itself, per API Surface above.
- `web/routes.py:291`'s `POST /api/merchant-category` endpoint calls
  `set_merchant_category` with no `confirm_random`, and — unlike the
  vestigial web UI from Dimension 1 — has live test coverage
  (`tests/test_web.py`). Decision: the endpoint **permanently loses the
  ability to set `Random`** (accepted, mirroring Dimension 1's own
  precedent of treating this web surface as low-priority) rather than
  growing a request-body passthrough for `confirm_random`. See Known
  Limitations. (Note: this is a different route than the one Dimension 1
  descopes wholesale — Dimension 1 treats the entire `web/` UI as
  vestigial/out-of-scope, while this is one specific, actively-tested
  route in the same file getting an explicit accepted-limitation
  writeup; different routes, different exposure and test coverage, not a
  contradiction between the two dimensions.)
- `cli.py:181` (the interactive `review()` loop) and `cli.py:197` (the
  `set-category` command) both call `set_merchant_category` with no way to
  pass `confirm_random`. Decision: **no CLI override in v1** — this matches
  the "discourage Random by default" intent, and the MCP `confirm_random`
  param already provides an escape hatch for agent-driven overrides. See
  Known Limitations.
- `agent/tools.py`'s `set_merchant_category` handler (lines 340-344) and
  `set_txn_category` handler (lines 347-351) — the ToolSpec schema change
  alone (API Surface above) is not sufficient; these handler bodies
  currently call `manual.set_merchant_category(args["merchant_norm"],
  args["category"], args.get("subcategory"), conn=conn)` and
  `manual.set_transaction_category(int(args["txn_id"]), args["category"],
  args.get("subcategory"), conn=conn)` with no `confirm_random` argument
  at all. Both handler bodies must be changed to read
  `args.get("confirm_random", False)` and pass it through as the
  `confirm_random` argument to their respective `manual.*` calls — without
  this, the MCP escape hatch described in Known Limitations (below) would
  not actually exist at runtime, even though the schema advertises it.

### Migration note

`tests/test_manual_categorize.py:70` calls `manual.set_merchant_category("CHECK",
"Random")` with no `confirm_random` as test setup (not testing the guard
itself) — this will raise `CategorizeError` under the new default-`False`
guard. That line needs updating to pass `confirm_random=True` as part of
implementation.

### Invariants

**Checkable by inspection:**
- `Random` stays in `categories.PROTECTED` — no removal of the category
  itself.
- Historical transactions already categorized `Random` are never touched
  by this change (no migration, no bulk recategorization).

**Testable:**
- `set_merchant_category(m, "Random")` without `confirm_random` raises
  `CategorizeError`.
- `set_merchant_category(m, "Random", confirm_random=True)` succeeds.
- `set_transaction_category(txn_id, "Random")` without `confirm_random`
  raises `CategorizeError`, and `set_transaction_category(txn_id,
  "Random", confirm_random=True)` succeeds — same guard, exercised
  explicitly for the transaction-level function rather than assumed from
  the merchant-level case.
- Every other category is completely unaffected (no guard fires).

### Known Limitations

- **`web/routes.py:291`'s `POST /api/merchant-category` endpoint permanently
  loses the ability to set a merchant's category to `Random`** — it has no
  `confirm_random` passthrough and none is planned. Accepted, since
  Dimension 1 already treats this web UI as a low-priority surface.
- **CLI users have no override path.** Neither `cli.py`'s interactive
  `review()` loop nor its `set-category` command can pass `confirm_random`,
  so a CLI user can never deliberately categorize as `Random` through the
  CLI after this change. Accepted as matching the "discourage by default"
  intent — the MCP `confirm_random` param remains available for
  agent-driven overrides.

## Testing Strategy

Unit tests in `tests/test_reports.py` (floor-direction `budget_status`/
`insights` cases, plus a case asserting `budget_overview()`'s `over` field
is correctly direction-aware for a floor-marked category at both the
category level and the subcategory level, and that `subs_exceed` is
unaffected by direction — still plain limit-vs-limit), `tests/
test_write_tools.py` (floor-marking MCP tools + `confirm_random` guard),
and CLI-level tests for both commands' label fix: `subscriptions()`'s
flag with a floor-marked category, and `report()`'s per-row label with a
mix of floor- and ceiling-marked categories in the same output. The
`remove_category` mismatched-direction guard is tested alongside the
existing `remove_category` coverage in `tests/test_manual_categorize.py`.
No new web/JS test harness — that surface is explicitly descoped.

## Integration Impact

No schema migration. No changes to `budgets.py`. Touches: `categories.py`
(new settings-blob functions + `off_track_delta`/`is_off_track` helpers),
`reports.py` (3 functions), `cli.py` (2 commands), `agent/tools.py` (2 new tools + 2
description-text updates + 2 guarded write functions), `categorize/
manual.py` (merge guard + `confirm_random` guard), 3 skill `.md` files
(`budget-visualizer`, `budget-budgets`, `budget-categorize`).
