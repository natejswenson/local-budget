# Phase 4 — The skills (no-code) + their backing read tools — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use crucible:build to implement this plan task-by-task.

**Goal:** Ship the 8 no-code `.claude/skills/budget-*/SKILL.md` skills + a shared `budget-analyst` persona — the agent-first interface that was the whole point — backed by the read tools each needs for clean, dedicated output (not hand-rolled `run_sql`).

**Architecture:** The MCP read+write tool surface is complete except for **8 dedicated read tools** the skills want (all backed by existing `reports.py`/`manual.py`/`reconcile.py` functions). Phase 4 (a) adds those 8 read tools to the `agent/tools.py` registry with `{data, rendered}` (deterministic, unit-tested), incl. the deferred `open_conflicts` (with `incoming_payee` redact-on-read), and hardens the already-registered `run_sql` to redact on read; (b) un-ignores `.claude/skills/` so skills are committable; (c) writes a shared persona + 8 self-contained, **no-code** `SKILL.md` files that orchestrate the tools with a confirm-gate (for writes) and the "print `rendered` verbatim" discipline.

**Design ref:** `docs/plans/2026-06-29-budget-true-agent-design.md` §2 (skill catalog), §3 (read tools + render contract). Phases 0–3 shipped (one DB + authorizer; de-wire; stdio MCP server + render + SDK-free registry; 9 write tools).

**Atomic:** false — Tasks 1–2 are additive (tools + gitignore); Tasks 3–5 are new files. **Tests to verify:** the full suite + the new read-tool tests; a skill-lint (no executable code in any SKILL.md).

**Out of scope (Phase 5+):** the eval harness (deterministic tier) + per-skill behavioral evals (Phase 5); CI/README/publish (Phase 6). This phase ships the skills + their tools.

---

### Task 1: Make `.claude/skills/` committable

**Files:** `.gitignore`.

`.claude/` is currently fully ignored (Claude Code runtime state). The budget skills live in `.claude/skills/` and MUST ship with the repo. Change:
```
.claude/
```
→
```
.claude/*
!.claude/skills/
```
(`.claude/*` ignores the runtime files; `!.claude/skills/` re-includes the skills dir, so `git add .claude/skills/budget-*/SKILL.md` tracks them while `scheduled_tasks.lock` etc. stay ignored.)

**Steps:** edit `.gitignore`; `git check-ignore .claude/scheduled_tasks.lock` → still ignored; `git check-ignore .claude/skills/x` → NOT ignored (un-ignored). Commit `chore(phase4): un-ignore .claude/skills (ship the budget skills)`.

---

### Task 2: The backing read tools

**Files:** `src/local_budget/agent/tools.py` (extend `TOOL_SPECS`); Test `tests/test_read_tools_phase4.py`.

Each is a thin `@_with_ro_conn` (or self-contained) handler over an existing function, returning `{data, rendered}` via `render.py`. **8 read tools** (the lint in Task 5 parses each skill's `tools:` manifest against `SPEC_BY_NAME`, so every tool a skill names must be registered here or already exist). Add:

| Tool | Backing fn | rendered | REAL data keys (read these, NOT the labels) |
|---|---|---|---|
| `budget_overview` | `reports.budget_overview(month)` | table: category · spent · limit · % used (over-budget flagged) | per-category dicts in `["categories"]`: `category`, `spent_cents`, period limit `budget_cents` (monthly is `monthly_budget_cents`), `pct`, over-budget flag `over` |
| `income_by_source` | `reports.income_by_source(month)` | table: source · amount · # | `source`, `total_cents` (NOT "amount"), `count` (NOT "#"), `other` |
| `income_transactions` | `reports.income_transactions(source, month)` | txn table | sanitized income rows (`income_source_key`-matched) |
| `review_queue` | `manual.needs_review()` + `manual.checks_to_review()` | TWO sections — see note below | needs_review: `merchant`, `count`, `spent_cents` · checks_to_review: `txn_id`, `posted_date`, `amount_cents`, `merchant_norm` |
| `subcategory_breakdown` | `reports.subcategory_breakdown(category, month)` | table: subcategory · spent | `subcategory`, `spent_cents` (+ budget) |
| `insights` | `reports.insights(month)` | bulleted "ways to save" | list-of-dicts: `kind`, `label`, `amount_cents` (+ `actual_cents`/`limit_cents`/`monthly_cents`/`count` by kind) |
| `monthly_trend` | `reports.monthly_trend(conn, limit)` (reports.py:186; design §3) | compact spend-by-month trend table | oldest-first rows: `month`, `spend_cents`, `income_cents` |
| `open_conflicts` | `reconcile.list_open()` (deferred from Phase 3) | table: id · kind · existing amount/date vs incoming amount/date + redacted incoming merchant | see `open_conflicts` note below — explicit projection, no existing-merchant column exists |

**`review_queue` — TWO sections (the two backing fns have DIFFERENT shapes; a single table KeyErrors):**
- (a) **"Uncategorized merchants"** from `manual.needs_review()` → table `merchant` · `count` · spent via `render.money(spent_cents)`.
- (b) **"Checks to review"** from `manual.checks_to_review()` → table date `posted_date` · amount `render.money(amount_cents)` · `merchant_norm`.
The handler renders both sections in one `rendered`; the builder must read each section's distinct keys (above) — do NOT fuse them.

**`open_conflicts` — the redact-on-read detail (design S6):** `reconcile.list_open()` does `SELECT * FROM import_conflicts` over `db.connect()`. The tool MUST instead read through `db.agent_connect()` with an **explicit column projection** (NOT `SELECT *`) and pass `incoming_payee` through `sanitize.redact_account_numbers()` before returning. The `import_conflicts` schema (db.py:60-73) has **NO existing-merchant text column** — `incoming_payee` is the only payee/merchant text (and the only column that needs redaction). Explicit projection: `conflict_id, kind, existing_amount_cents, existing_posted_date, incoming_amount_cents, incoming_posted_date, incoming_payee` (`incoming_payee` redacted on read). The render shows, per row: `conflict_id` · `kind` · existing `existing_amount_cents`/`existing_posted_date` **vs** incoming `incoming_amount_cents`/`incoming_posted_date` + the redacted incoming merchant text — NO existing-merchant column. (`reports.*`/`manual.*` functions that open their own `db.connect()` are read-only and select non-PII columns, so they're fine through `_with_ro_conn`'s discarded-conn path — but `open_conflicts` reads raw `incoming_payee`, so it must go through `agent_connect` + redact.)

**`run_sql` hardening — close the read-side leak (design §3 "largest read-side risk"):** `run_sql` (tools.py:265-285) `str()`s every returned column with NO redaction, so `SELECT payee` leaks raw account numbers — defeating the redaction-on-read invariant. It stays registered (power-user drill-down) but no v1 skill names it (see Task 4). HARDEN it: pass every returned string cell through `sanitize.redact_account_numbers()` before returning (both the `data["rows"]` values and the `rendered` table). Add a test that a `SELECT payee` carrying an account number comes back redacted through `run_sql`.

**`budget_overview` None-guard:** `budget_cents`/`monthly_budget_cents`/`pct` are `None` for any spend category with no active limit (`reports.py` `_pct` returns None), and `render.money(None)` raises `TypeError`. The render MUST guard: show `—` for a None limit/pct (pass the raw value to `render.table` whose `_cell` maps `None → "—"`, or guard before `render.money`). The Task-2 test seed MUST include an UN-budgeted spend category so this path is exercised (otherwise the crash hides until a real over-/under-budget mix).

**Schemas:** real JSON-Schema objects (month optional; `income_transactions {source, month?}` req source; `subcategory_breakdown {category, month?}` req category; `monthly_trend {limit?}`).

**Steps:** add the 8 handlers + specs; harden `run_sql` (redact); tests assert each returns `data`+`rendered`, figures are correct on a seeded budget.db, `open_conflicts` redacts an account-number-bearing `incoming_payee` AND never emits a denied column, and `run_sql` redacts an account-number-bearing `payee`. `uv run pytest -q` green; ruff clean. Commit `feat(phase4): 8 backing read tools + run_sql redact (budget_overview/income/review_queue/monthly_trend/insights/open_conflicts)`.

---

### Task 3: The shared persona — `.claude/skills/budget-analyst/SKILL.md`

A small **no-code** skill the others reference. Content (markdown only):
- **Frontmatter:** `name: budget-analyst`, `description: shared persona/discipline for the budget skills`, and the machine-readable manifest `tools: []` (persona calls no tools — but the key is present so the Task-5 lint's frontmatter parse is uniform across every SKILL.md).
- **Body — the discipline:** (1) **Never invent a number** — every figure comes from a tool result; if a tool wasn't called, don't state the number. (2) **Print the tool's `rendered` block verbatim**, then add ≤3 sentences of synthesis. (3) **Money** is always the tool's formatted string. (4) **Confirm before any write** — show the proposed change and get a "yes" before calling a write tool. (5) **Account numbers / raw text never restated** — only what tools return.

**Steps:** write it; commit `feat(phase4): budget-analyst shared persona`.

---

### Task 4: The 8 skills — `.claude/skills/budget-<name>/SKILL.md`

Each is **self-contained, no-code markdown** with frontmatter (`name`, `description`, and the machine-readable **`tools: [tool_a, tool_b, ...]` manifest** — the YAML list of EVERY tool the skill calls, which the Task-5 lint parses against `agent_tools.SPEC_BY_NAME`) + a body that: names the persona discipline, lists the exact tools it calls, the order, the confirm-gate (writes only), and the "print `rendered` verbatim" instruction. The body's tool mentions and the `tools:` manifest must agree. One file per skill:

| Skill | Job | Tools (read → write) — also the `tools:` manifest | Confirm-gate |
|---|---|---|---|
| `budget-setup` | First run: import → categorize → budgets/income | (CLI handoff: `budget import`/`intake`) → then `categorize`/`budgets` skills | n/a (orchestration) |
| `budget-coach` | Answer any money question, grounded | get_month_summary, get_category_breakdown, query_transactions, compare_periods, top_merchants | read-only |
| `budget-monthly-brief` | Compose the period brief | get_month_summary, insights, monthly_trend, find_anomalies → **save_brief** (confirm) | confirm save_brief |
| `budget-categorize` | Work the review queue | **review_queue** → set_merchant_category / set_txn_category / add_custom_category (confirm each) | confirm each write |
| `budget-budgets` | Review spend-vs-limits; set limits | **budget_overview** → set_budget_limit / clear_budget_limit / set_expected_income (confirm) | confirm each write |
| `budget-income` | Income by source; expected vs actual | **income_by_source**, income_transactions, get_month_summary | read-only |
| `budget-subscriptions` | Audit recurring; price-creep; split | recurring_charges, **subcategory_breakdown** → split_subscriptions / set_budget_limit (confirm) | confirm writes |
| `budget-reconcile` | Explain the conflict queue (advisory) | **open_conflicts** → emit the exact `budget reconcile <id> <action>` CLI command (action ∈ `keep_one`\|`mark_distinct`\|`merge`\|`accept_incoming`; there is NO `resolve` subcommand — `reconcile_cmd` at cli.py:325 takes positional `conflict_id` then `action`) | advisory (CLI handoff, no write tool) |

**No v1 skill names `run_sql`.** `budget-coach`'s five structured read tools (get_month_summary / get_category_breakdown / query_transactions / compare_periods / top_merchants) cover its drill-down needs; `run_sql` returns raw `payee`/`memo` and is the largest read-side risk (design §3), so it stays registered+hardened (Task 2) but is dropped from every skill's `tools:` manifest.

Each SKILL.md ends naming a handoff where relevant (coach → "run `/budget-categorize`?"). **No executable code blocks** — directions only.

**Steps:** write the 8 files; commit `feat(phase4): 8 budget skills (no-code SKILL.md)`.

---

### Task 5: Skill-lint + Phase-4 gate

**Files:** `tests/test_skills_lint.py` (a deterministic check on the SKILL.md files).

The lint asserts (no model calls):
- Every `.claude/skills/budget-*/SKILL.md` has YAML frontmatter with `name` + `description` + a `tools:` list (the machine-readable manifest; may be empty for the persona/orchestration skills).
- **No executable code block** that would run (no ```python / ```bash fenced blocks with executable intent) — skills are directions only. (A ```text/```markdown example is fine; the lint flags ```python```/```bash```/```sh```.)
- Each write-capable skill's body contains a confirm-gate phrase (e.g. "confirm"/"ask"/"before writing") and names its write tool(s).
- **Tools-exist (the extraction mechanism):** parse each SKILL.md's `tools:` frontmatter list and assert EVERY entry exists in `agent_tools.SPEC_BY_NAME` — no prose-scraping, no guessing a tool name from ordinary text. (This is what makes a missing-from-registry tool like F1's `monthly_trend` catchable.) No skill's manifest lists `run_sql`. **Parse mechanism:** `yaml` is NOT a project dependency — do NOT `import yaml`. The `tools:` line is a simple inline list (`tools: [a, b, c]`); regex/`split`-parse it (slice the frontmatter between the leading `---` fences, find the `tools:` line, strip `[]`, split on `,`), or add `pyyaml` to the dev group. A bare `import yaml` fails at collection. **Filter empty tokens** — the persona's `tools: []` naive-splits to `['']`, which would fail the `SPEC_BY_NAME` lookup as a phantom tool; drop blank entries after split.
- Each skill names the persona discipline (references `budget-analyst` or restates "print rendered verbatim / never invent a number").

**Gate:**
1. `uv run pytest -q` → green (suite + new read-tool tests + skill-lint).
2. `uv run ruff check src tests` → clean.
3. `.claude/skills/` committable (`git check-ignore .claude/skills/budget-coach/SKILL.md` → not ignored).
4. MCP server lists all tools (read + write + the 8 new); schemas serialize.
5. `open_conflicts` redacts `incoming_payee` (test) and emits no denied column; `run_sql` redacts an account-number `payee` (test).
6. `bash scripts/secret-scan.sh` → clean.

**Steps:** write the lint; iterate to green; commit `test(phase4): skill-lint (no-code, confirm-gate, tools-exist)`.

---

## Acceptance (Phase 4)
- 8 new read tools registered with `{data, rendered}`; `open_conflicts` redacts `incoming_payee` + reads via `agent_connect` (explicit columns, no `SELECT *`); `run_sql` hardened to redact on read.
- `.claude/skills/` is committable; runtime state stays ignored.
- A `budget-analyst` persona + 8 self-contained no-code `SKILL.md` skills; each carries a `tools:` manifest (every entry exists in `SPEC_BY_NAME`, none lists `run_sql`), its confirm-gate (writes), and the render-verbatim discipline.
- Skill-lint green (frontmatter incl. `tools:`, no executable code, confirm-gate, manifest tools-exist); full suite + ruff green; MCP server lists all tools.

## Invariants
- **Checkable:** no executable code block in any `SKILL.md`; every tool in a skill's `tools:` manifest exists in `SPEC_BY_NAME` (and no manifest lists `run_sql`); `open_conflicts` uses an explicit projection (no `SELECT *`) + `redact_account_numbers` on `incoming_payee`; `run_sql` redacts every returned string cell; `.claude/skills/` un-ignored while `.claude/*` ignored.
- **Testable:** each of the 8 new read tools returns correct `data`+`rendered` on a seeded DB; `open_conflicts` and `run_sql` both redact an account-number-bearing payee; the skill-lint passes; existing suite stays green.

## Failure modes considered
- **`.claude/skills/` still ignored** → skills silently untracked on a clone → skill-lint gate + the `git check-ignore` check.
- **A skill names a non-existent tool** (incl. an un-registered `monthly_trend`, F1) → the lint parses the `tools:` manifest against `SPEC_BY_NAME`.
- **A skill embeds executable code** (violating no-code) → the lint's fenced-block check.
- **`open_conflicts` leaks raw payee / aborts on a denied column** → explicit projection + redact + the redaction test.
- **A write skill writes without confirming** → the lint requires a confirm phrase; true behavioral enforcement is the deferred live eval (Phase 5).
