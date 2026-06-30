---
ticket: "#TBD"
title: "Budget as a True Agent — deterministic MCP tools + no-code Claude skills"
date: "2026-06-29"
source: "design"
---

# Budget as a True Agent

Restructure `local-budget` toward the `local-fitness` architecture: a
**deterministic, unit-tested core** exposed over a single **MCP tool server**,
with all **non-deterministic (AI) capability moved out of the app into
human-readable, no-code Claude skills** that run in the user's Claude Code
session. The goal is an agent-first product whose session output is *clean and
beautiful*, whose deterministic code is fully tested, whose skills carry
behavioral evals, and which is safe to publish as a public GitHub repo with
personal data cleanly separated from the app layer.

## Decisions (resolved with the user)

1. **AI layer shape** — deterministic tools over one MCP server **plus literal
   `.claude/skills/budget-*/SKILL.md` wrappers** (persona + render contract),
   one eval per skill. Skills run in the Claude Code session on subscription
   auth (no API key in the app path).
2. **Dashboard** — its **deterministic/visual layer is untouched** (charts, data,
   budgets/reports views, and their `db.connect()`-backed routes stay intact); its
   **inference endpoints (`/api/chat`, `/api/categorize`, `/api/recategorize`,
   `/api/normalize`) are RETIRED** — that capability now lives in skills, since the
   modules they call (`agent/chat.py`, `categorize/llm.py`) are deleted (F1). No
   design effort spent on the deterministic web layer here.
3. **Repo strategy** — refactor the existing repo **in place**. The CLI becomes
   **deterministic-only**; every AI capability moves to a skill. Concretely the
   non-deterministic AI categorizer (`categorize/llm.py`, which imports
   `claude-agent-sdk`) is **deleted** — its capability moves to the `categorize`
   skill; `budget import` / `budget intake` apply **only** deterministic
   rule-based categorization (`categorize/rules.py`), landing everything else
   `Uncategorized` for the skill; and the `categorize` / `recategorize` CLI
   commands are removed (F1).
4. **Write boundary (scoped-full)** — skills read **real merchant text**
   (`payee`/`memo`/`incoming_payee`), but the text is **best-effort redacted of
   account numbers on read** (the columns stay raw at rest; the read tools run
   `sanitize.redact_account_numbers()` before returning — see F1). This redaction
   strips only **≥7-digit numeric runs**; it does **not** remove 5–6-digit
   fragments or Zelle/Venmo/wire **counterparty names** — that is the accepted
   `sanitize` I14 residual (the skill runs on the user's own subscription), so
   `payee` is *not* claimed PII-clean (M1). Writes hit **only the derived category
   columns + the app-config tables**; the **raw imported ledger — and now
   `status` and the conflict/import-state tables — is immutable to skills** (S4).
5. **Data model** — **collapse the two-DB split into a single `budget.db`**, with
   the boundary enforced by a **connection-scoped SQLite authorizer** rather
   than physical file separation.
6. **Skills are pure `.md`** — directions + guardrails only, **no code**.
   Self-contained: everything a skill needs is in its own file. All code lives
   in the unit-tested MCP tools.

## Non-goals

- Changing the dashboard's **deterministic/visual layer** (charts, data,
  budgets/reports views — left as-is). Its **inference endpoints** (`/api/chat`,
  `/api/categorize`, `/api/recategorize`, `/api/normalize`) are **RETIRED**, not
  preserved — their capability moves to skills (F1).
- Multi-tenant / multi-user. Single-user, self-hosted, one local DB.
- Plaid/aggregator/scraping. Manual file import only (unchanged trust boundary).
- Calling the Anthropic API directly from app code with an API key.

---

## Section 1 — Architecture & Data Model

### Collapse to one DB

`agent.db` existed **only** to keep `payee`/`memo` away from the model. Decision
#4 makes skills *read* real merchant text, so the second sanitized DB now buys
nothing and adds sync risk (the "agent.db is stale" guard exists only to manage
that risk). We collapse to a single `budget.db`.

The boundary becomes **explicit, testable authorizer rules** on the agent
connection instead of an implicit property of which file was opened:

The boundary is a **column-level SQLite authorizer** on the real schema
(`transactions` holds the financial facts; categories are *columns* on it, not a
separate table). "Immutable to skills" means the **imported-fact columns** are
immutable; the **derived columns** are writable:

```
db.connect()              -> full read/write   (importer, CLI, dashboard)
db.agent_connect(write=…) -> connection-scoped SQLite authorizer:

  WRITE side — the financial-fact firewall (DEFAULT-DENY allowlist — S1/S4):
    ALLOW UPDATE transactions.{category, subcategory, category_source}
                                              (the ONLY writable transactions
                                               columns — derived category only)
    ALLOW INSERT/UPDATE/DELETE on the writable-table allowlist:
          {category_rules, budgets, settings}  (app-config layer only)
                                              (settings KEY-whitelist is tool-
                                               enforced, NOT authorizer-enforced — S1)
    DENY  *everything else, by default* — this is an ALLOWLIST, not a denylist:
          • any transactions column not in the 3 above — incl. amount_cents,
            posted_date, payee, memo, raw_ofx, fitid, account_id, merchant_norm,
            canonical_merchant, status, txn_type, imported_at, import_run_id
            (imported facts + status are immutable to skills — S4)
          • INSERT/DELETE on transactions   (no skill adds/removes a ledger row)
          • any table not on the allowlist — incl. import_conflicts,
            merchant_aliases, normalize_changes, import_runs, inbox_files, accounts
            (conflict/import-state is immutable to skills — S4)

  READ side — explicit denylist (payee/memo ARE readable, decision #4):
    DENY  READ transactions.raw_ofx           (redacted raw export blob)
    DENY  READ accounts.acct_hash             (HMAC account-identity)
    DENY  READ inbox_files.filename, import_runs.source_name,
          import_runs.error_message           (raw filenames / paths / error text
                                               can carry PII — S3)
    ALLOW everything else (incl. transactions.payee / .memo,
          import_conflicts.incoming_payee, accounts.acct_last4)
          — but every read TOOL that surfaces payee / memo / incoming_payee
            passes the text through sanitize.redact_account_numbers() before
            returning it to the skill (redaction-on-read — see F1 / S6).

  Always:
    DENY  ATTACH / DETACH / PRAGMA writes / DDL / temp-table escapes
```

**The authorizer is NEW code, not a retarget of `agent_db.py` (M2).** The old
`agent_db.py` authorizer was *table-level*: a read-allowlist plus a blanket
write-deny on the sanitized projection DB. The new `db.agent_connect()` is a
**rewrite** — *column-level* read-denylist plus a *column/table write-allowlist*
on the real `budget.db` schema. None of the old authorizer code survives; only
the *idea* of a connection-scoped authorizer carries over.

**What the authorizer enforces vs. what the tool layer enforces.** The
authorizer is a *column/operation* gate — it can ALLOW/DENY a table+column for an
operation, but it cannot inspect row values or string keys. So two properties are
**tool-enforced**, not authorizer-enforced: (a) the `settings` KEY-whitelist (the
UPDATE/INSERT callback sees `settings.value`, never the row's key — S1); and (b)
account-number redaction of `payee`/`memo`/`incoming_payee` on read (F1/S6).
Since `run_sql` is dropped (§3), every skill read/write goes through a structured
tool, so the tool layer is a sufficient place for both. (`status` integrity is
*no longer* a tool-layer concern — the authorizer denies `status` UPDATE outright,
S4.)

**Read-side authorizer is defense-in-depth, not the primary read guard.** Many
read backing functions (`reports.budget_overview`/`income_by_source`/`insights`/
`subcategory_breakdown`, `manual.needs_review`/`checks_to_review`,
`detect.recurring`/`anomalies`, `reconcile.list_open`, `notes.read_notes`) open
their own connection and take no `conn`, so the guarded read connection is
opened-but-discarded for those tools and the READ denylist is genuinely exercised
only for `query_transactions` and the `conn`-accepting `reports` calls. Read
safety therefore rests on two TOOL-layer properties — (a) redaction-on-read of
`payee`/`memo`/`incoming_payee`, and (b) read tools enumerating columns and never
selecting a denied column (`raw_ofx`/`acct_hash`/`inbox_files.filename`/
`import_runs.source_name`/`error_message`) — with the read-side authorizer as a
backstop. The build must keep these tool-layer guards as the load-bearing read
defense; refactoring the backing functions to thread the guarded `conn` (so the
authorizer covers every read) is a recommended hardening, not a v1 blocker.

**The authorizer is connection-scoped, not DB-global.** The deterministic
importer *must* INSERT/DELETE rows in `transactions`; it uses `db.connect()`.
Only the MCP tool layer opens `db.agent_connect()`. The core guarantee:
**a skill can never alter or fabricate an imported financial fact
(amount / date / payee / memo / status) — it can write ONLY the three derived
category columns (`category`, `subcategory`, `category_source`) and the
app-config tables.** Because the write side is a default-DENY allowlist, anything
not explicitly listed — `status`, `txn_type`, any other column, and every
non-app-config table (`import_conflicts`, `merchant_aliases`, …) — is denied.

**The authorizer is only real if it is in the write PATH — every write tool
THREADS one `agent_connect(write=True)` connection (F1).** A write tool that
called today's helpers as-is would *bypass* the authorizer: the existing write
helpers each open their **own** connection internally (`set_merchant_category` /
`set_transaction_category` / `remove_category` via `db.mutating_connect()`;
`split_subscriptions` via `db.connect()`; `set_limit` / `clear_limit` /
`set_expected_income` / `add_custom_category` / `unhide_category` via
`db.connect()` / a no-`conn` `db.set_setting()`), so wrapping them would run the
write on an unguarded full-RW connection while an *isolated* authorizer test still
passed — false assurance. The fix: **every write tool opens exactly one
`db.agent_connect(write=True)` connection and passes it down as an injected
`conn`**, and the write helpers are **refactored to accept and use that `conn`**
rather than opening their own. The helpers that currently lack a `conn` parameter
and so MUST be refactored to thread it are: `manual.set_merchant_category`,
`manual.set_transaction_category`, `manual.remove_category`,
`manual.split_subscriptions`, `budgets.set_limit`, `budgets.clear_limit`,
`budgets.set_expected_income`, `categories.add_custom_category`, and
`categories.unhide_category`. (`db.set_setting`, `categories.mark_hidden`,
`categories.remove_custom`, `categories.custom_categories`,
`categories.hidden_categories`, and `budgets.active_limits` already take `conn` —
no change.) This is why the de-wiring table below converts these helpers to
**thread `conn`**, not merely to `db.connect()` — repointing to `db.connect()`
would re-open an unguarded connection and re-introduce the bypass.

**Reconcile is advisory only — EVERY resolution is a CLI handoff (S4).** Every
`reconcile.resolve` action mutates ledger or conflict state that the authorizer
now denies to skills: `(near_duplicate, keep_one)`/`(near_duplicate, merge)`
`DELETE` a row; `(fitid_collision, accept_incoming)` rewrites
`amount_cents`/`posted_date`; `(near_duplicate, mark_distinct)` writes
`transactions.status='posted'`; and all of them `UPDATE import_conflicts.resolved`.
Under `db.agent_connect()` the writable set is `{category, subcategory,
category_source}` on `transactions` plus `{category_rules, budgets, settings}` —
**so `status`, `amount_cents`/`posted_date`, `transactions` INSERT/DELETE, and the
entire `import_conflicts` table are all denied.** There is therefore **no skill
write path to any reconcile resolution**. The `reconcile` skill is **read-only
(advisory)**: it reads/explains the open conflict queue and, for whichever action
the user chooses, emits the exact deterministic CLI command
(`budget reconcile resolve <id> <action>`) for the user to run under
`db.connect()`. This makes the authorizer a *truly uniform* net: skills write
ONLY `{category, subcategory, category_source}` on transactions plus the
app-config tables, and nothing else — every ledger/conflict fact is unreachable
by every skill path without exception.

### Account numbers (two distinct surfaces — `accounts` by construction, `payee`/`memo` by redaction-on-read)

The **`accounts` identity** has no full number at rest. `ingest/importer.py`
masks at import: it stores only `accounts.acct_last4` (e.g. `4444`) plus an HMAC
`accounts.acct_hash` (`db.acct_hash`, keyed by the local `local_key`); the full
account number is never written to `budget.db`. So for the `accounts` table
"account numbers never reach the model" holds **by construction**, not by
redaction — there is nothing to redact. The masked label a skill shows is
`accounts.acct_last4`; the READ authorizer additionally **denies
`accounts.acct_hash`** so the identity HMAC never reaches a skill either.

**`transactions.payee`/`.memo` and `import_conflicts.incoming_payee` are a
different surface.** The importer stores these **RAW** (`_insert_txn` writes
`ptxn.payee`/`ptxn.memo` verbatim; `redact_account_numbers` is applied **only**
to the `raw_ofx` blob, not to `payee`/`memo`). Decision #4 exposes `payee`/`memo`
to skills — and bank-transfer / Zelle / wire descriptions routinely carry a full
account number in that text — so "nothing to leak" is **false** for this surface.
Keeping the column raw is correct (the deterministic layer must stay faithful to
the bank's text), so the fix lives in the **read-TOOL layer**: every MCP read
tool that surfaces `payee`, `memo`, or `import_conflicts.incoming_payee` passes
the string through the existing `sanitize.redact_account_numbers()` before
returning it to the skill. A SQLite authorizer cannot *transform* a value (it can
only allow/deny a column), and `run_sql` is dropped (§3) so every skill read goes
through a structured tool — which makes tool-layer redaction the right and
sufficient place. **Honest scope of this redaction (M1, sanitize I14):** it
strips only **≥7-digit numeric runs** (after separator-collapse). It does **not**
remove 5–6-digit fragments, nor does it strip Zelle/Venmo/wire **counterparty
names** — `redact_account_numbers()` is *not* `strip_p2p_names()`. So a skill that
reads `payee` can still see a person's name on a P2P transfer; that is the
accepted, bounded I14 residual (the skill runs on the user's own subscription),
not a claim that `payee` is PII-clean. The deterministic merchant key
(`merchant_norm`) is already redacted **and** P2P-stripped at import, so
grouping/rules are unaffected.

`accounts` grain: **one row = one bank account the user imports from** (existing
table — unchanged).

### Migration of existing safety code (de-wiring)

The two-DB model (`agent_db.py` + `sync.py` + the sanitized `txn` projection +
the generation/staleness machinery) exists ONLY to keep `payee`/`memo`
physically out of a second file. Decision #4 lets skills read that text, so the
projection is deleted and the authorizer becomes the boundary.

**Ordering invariant (S1): the package must import cleanly after EVERY phase.**
`agent/tools.py` imports `claude_agent_sdk` + `agent_db`'s read path
(`connect_readonly`/`assert_fresh`/`StaleAgentDBError`), so deleting that read
path **before** `agent/tools.py` is rewritten would break the import and falsify
the per-phase "tests green / committable" gate. Therefore the de-wiring splits
across two phases:

- **Phase 0** adds the **bare `db.agent_connect()` authorizer** (new, additive)
  and de-wires everything that does **not** depend on the agent read path:
  collapse to one `budget.db`; thread `conn`/repoint the write+importer call
  sites (`ingest/importer.py`, `intake.py`, `normalize.py`, `reconcile.py`,
  `cli.py`, `categorize/manual.py`, `budgets.py`, `categories.py`); delete
  `sync.py`, the `db.py` projection/staleness helpers, the `paths.py` `agent.db`
  machinery, and the `budget verify` command; rewrite `test_security.py` against
  `agent_connect()`. `agent_db.py` is left **importable** here (still satisfies
  `agent/tools.py`'s import) — only its now-orphaned rebuild path is dead.
- **Phase 1** deletes `agent_db.py` **in full** and, **in the same phase**,
  rewrites `agent/tools.py` (retarget reads to `agent_connect()`, drop the
  `claude_agent_sdk`/`agent_db` imports) and rewrites `test_agent_tools.py`.
  Because the deletion and its only consumer move together, the package imports
  cleanly at the end of Phase 0 **and** Phase 1.

**Delete (split across Phases 0/1 per the ordering invariant above):** `sync.py`
(`refresh_agent_db`/`verify`) — Phase 0; `db.SANITIZED_PROJECTION_SQL`,
`db.sanitized_projection`, `db.conflict_aggregates`, `db.mutating_connect`,
`db.bump_gen`, `db.current_gen` — Phase 0; `paths.py`'s orphaned `agent.db`
machinery (`agent_db_path`, `agent_db_tmp_path`, `expected_gen_path`,
`write_expected_gen` and the `.expected_gen` marker — all dead once the
projection is gone, M2) — Phase 0; `test_projection_refresh.py` — Phase 0;
`agent_db.py`'s rebuild + read path (`rebuild_from`, `connect_readonly`,
`_checksum`, `agent_gen`/`expected_gen`/`assert_fresh`, `StaleAgentDBError`) —
**Phase 1, with the `agent/tools.py` + `test_agent_tools.py` rewrite**.
`agent_db.py` is ultimately deleted **in full**: `db.agent_connect()` is **NEW
code, not a retarget** of its authorizer — the old one was table-level
(read-allowlist + blanket write-deny on the projection DB), the new one is
column-level (read-denylist + write-allowlist) on the real `budget.db` schema
(M2). Skills now read `budget.db` directly.

Separately, the **AI categorizer `categorize/llm.py` is DELETED** (not de-wired):
it imports `claude-agent-sdk`, and its categorization capability moves to the
`categorize` skill (F1). `budget import` / `budget intake` keep only the
deterministic `categorize/rules.py` pass; the `categorize` / `recategorize` CLI
commands are removed (see §3 + Acceptance #4).

**De-wiring checklist** — every call site of the deleted helpers and its conversion:

| File | Today | Convert to |
|---|---|---|
| `ingest/importer.py` | `db.connect()` + `db.bump_gen` (×4) + `sync.refresh_agent_db()` | drop `bump_gen` + `refresh_agent_db`; keep `db.connect()` |
| `intake.py` | `db.bump_gen` + `sync.refresh_agent_db()` | drop both |
| `normalize.py` | `db.mutating_connect()` + `db.bump_gen` + `sync.refresh_agent_db()`; also a try/except import of `cluster_merchants` from `categorize/llm.py` (~165) | `db.connect()`; drop bump + refresh; **drop the `cluster_merchants` import + its LLM merchant-clustering step** — a deliberate decision, not an accident: `categorize/llm.py` is deleted (F1), so the optional LLM cluster pass goes away and `budget normalize` runs deterministic-only. The LLM-clustering capability, if wanted, belongs to a future skill (**deferred**, M1) |
| `reconcile.py` | `db.connect()` + `db.bump_gen` + `sync.refresh_agent_db()` | drop bump + refresh (deterministic/CLI path; see §2) |
| `cli.py` | `db.mutating_connect()` + `sync.refresh_agent_db()` | `db.connect()`; drop refresh |
| `cli.py` `verify()` | `verify` command calls `sync.verify()` ("check agent.db consistency vs budget.db; rebuild if stale") | **remove the command** (there is no second DB to verify); the agent.db doctor has no meaning post-collapse |
| `categorize/manual.py` | `db.mutating_connect()` (×4) + `db.bump_gen` + `sync.refresh_agent_db()`; `split_subscriptions` uses `db.connect()` + `seed_builtin_aliases` (F1) | **Refactor the write-tool helpers `set_merchant_category` / `set_transaction_category` / `remove_category` / `split_subscriptions` to accept an injected `conn`** (so the write tool's single `db.agent_connect(write=True)` is THREADED through them — F1, not a fresh `db.connect()`); drop `seed_builtin_aliases` from `split_subscriptions` (S2). The non-tool mutators (`set_merchant_subcategory`, `rename_subcategory`) just repoint `db.mutating_connect()`→`db.connect()`. Drop bump + refresh everywhere |
| `budgets.py` (write helpers) | `set_limit` / `clear_limit` / `set_expected_income` open their OWN `db.connect()` / no-`conn` `db.set_setting()` (F1) | **Refactor to accept an injected `conn`** so the `set_budget_limit` / `clear_budget_limit` / `set_expected_income` write tools THREAD `db.agent_connect(write=True)` through them; `set_setting(..., conn=conn)` already supports this |
| `categories.py` (write helpers) | `add_custom_category` / `unhide_category` call no-`conn` `db.set_setting()` (F1) | **Refactor to accept an injected `conn`** so the `add_custom_category` write tool THREADS `db.agent_connect(write=True)`; `mark_hidden` / `remove_custom` / `custom_categories` / `hidden_categories` already take `conn` |
| `agent/tools.py` (read path) | imports `from claude_agent_sdk import …` + `from .. import agent_db` and uses `agent_db.connect_readonly` / `assert_fresh` / `StaleAgentDBError` (the largest consumer of the deleted read path) | **Rewrite (Phase 1, S1): retarget every read to `db.agent_connect()`** on the single `budget.db`, drop the `claude_agent_sdk` + `agent_db` read-path imports + the `_ro()` / `assert_fresh` staleness boilerplate, and route reads through the deterministic backing functions (§3 backing map). Done **in the same phase** that deletes `agent_db.py` + rewrites `test_agent_tools.py`, so the package imports cleanly after every phase |
| `categorize/llm.py` | the AI categorizer (imports `claude-agent-sdk`); called by import/intake/categorize/recategorize | **DELETE the whole file** — its AI capability moves to the `categorize` skill (F1); not de-wired, removed |
| `cli.py` import/intake + `categorize`/`recategorize` cmds | `import`/`intake` call `llm.categorize_uncategorized`; `categorize`/`recategorize` commands call `categorize/llm.py` | strip the auto-AI step from `import`/`intake` (deterministic `categorize/rules.py` only; rest lands `Uncategorized`); **remove the `categorize` and `recategorize` CLI commands** (AI → skill, F1) |
| `web/routes.py` (inference endpoints) | `/api/categorize`→`llm.categorize_uncategorized` (~382); `/api/recategorize`→`llm.recategorize` (~387); `/api/chat`→`agent.chat._ask_once` (~495); `/api/normalize`→`normalize.py` (which imports `cluster_merchants` from `categorize/llm.py`) | **RETIRE these four routes and their now-dead imports of `llm` / `agent.chat`** — their capability moves to skills (F1). The deterministic/visual routes (charts, data, budgets/reports views) are **untouched** and keep `db.connect()`. This is a decision, not breakage: the doc no longer claims "dashboard untouched" wholesale — only the deterministic layer is |
| `paths.py` | `agent_db_path` / `agent_db_tmp_path` / `expected_gen_path` / `write_expected_gen` + `.expected_gen` marker | remove all four helpers + the marker (orphaned post-collapse, M2) |
| `db.py` | defines `mutating_connect` / `bump_gen` / `current_gen` / `sanitized_projection` / `conflict_aggregates` + `SANITIZED_PROJECTION_SQL` | remove all five helpers + the projection SQL |
| **tests importing the deleted read-path** (grep `connect_readonly\|assert_fresh\|agent_gen\|refresh_agent_db\|mutating_connect\|bump_gen`) — `test_subcategories.py`, `test_reports.py`, `test_import.py`, `test_manual_categorize.py`, `test_db.py` | call `db.mutating_connect()`/`db.bump_gen`/`sync.refresh_agent_db()` as part of setup/assertions | **repoint** to `db.connect()`; drop bump/refresh assertions (these are not "stay green unchanged" — they must be rewritten). `test_security.py` is rewritten against `agent_connect()` (Phase-0 gate); **`test_agent_tools.py` is rewritten against `agent_connect()` in Phase 1, together with the `agent/tools.py` rewrite + `agent_db.py` deletion, so the package imports cleanly after every phase (S1)**; `test_projection_refresh.py` is deleted; **`test_categorize_llm.py` is DELETED with `categorize/llm.py`** (F1 — the AI categorizer is gone); **`test_web.py`'s retired-endpoint tests are removed/rewritten** — `test_chat_*` (import `agent.chat`, patch `_ask_once` ~469–519) and `test_normalize_*` (import `categorize.llm` ~674) are **deleted** with the retired `/api/chat` + `/api/normalize` routes (F1/M2); the deterministic dashboard route tests in `test_web.py` stay green |

---

## Section 2 — Skill Catalog

All skills share **one persona file** (`budget-analyst.md`: grounded,
never-invent-a-number, clean-render discipline) plus a per-skill task brief.
Each is single-sentence-describable, self-contained, no overlap; every write
skill is **confirm-gated** (proposes → shows diff → writes only on "yes").

| # | Skill | One-sentence job | R/W |
|---|-------|------------------|-----|
| 1 | **setup** | Guide first run: hand off to the deterministic `budget import`/`intake` CLI, then drive `categorize` + `budgets`. | R+W† |
| 2 | **budget-coach** | Answer any money question, grounded, with drill-downs. | R |
| 3 | **monthly-brief** | Compose the structured period brief + flags. | R (+save artifact) |
| 4 | **categorize** | Work the review queue; assign categories, pin rules. | R+W |
| 5 | **budgets** | Review spend-vs-limits; set limits & expected income. | R+W |
| 6 | **income** | Analyze income by source — expected vs actual, paycheck cadence, missing/odd deposits. | R |
| 7 | **subscriptions** | Audit recurring charges; price-creep, dormant subs, split sub-budgets. | R+W |
| 8 | **reconcile** | Explain the duplicate/conflict queue and hand off EVERY resolution as a `budget reconcile resolve` CLI command. | R (advisory) |

† **setup is a CLI handoff for import (S5/F1).** `setup` has no MCP import tool —
import/intake remain **deterministic CLI**. The skill instructs the user to run
`budget import <file>` / `budget intake` (a handoff, exactly like `reconcile`'s),
then drives the `categorize` skill for post-import categorization and `budgets`
for limits & income. Its own writes are only those it makes *through* `categorize`
/ `budgets` (the derived-column + app-config allowlist).

\* **reconcile is READ-ONLY / advisory (S4).** EVERY `reconcile.py` action mutates
ledger or `import_conflicts` state that `db.agent_connect()` denies to skills —
including `(near_duplicate, mark_distinct)`, which writes `transactions.status`
(now denied), and the `UPDATE import_conflicts.resolved` every resolution issues
(table not on the write allowlist). There is **no** in-skill resolution, destructive
or otherwise (M3): the skill reads/explains the open conflict queue and emits the
exact `budget reconcile resolve <id> <action>` command for the user to run under
`db.connect()`. The `resolve_conflict` write tool is **dropped**; a read-only
`open_conflicts` tool surfaces the queue instead (§3).

Anomaly detection is **folded into `budget-coach` + `monthly-brief`** (no
standalone skill). Skills may *name* another skill as a handoff (e.g. coach →
"want me to run `/categorize`?") but never depend on reading another skill's
body.

---

## Section 3 — MCP Tool Surface & Render Contract

### One source of truth

Tools are defined once in `agent/tools.py` and exposed by a single **stdio MCP
server** (`web/mcp_server.py`, built on the `mcp` package), registered in the
committed `.mcp.json` so any Claude Code session in this directory auto-connects.
**`claude-agent-sdk` is removed** and the in-app chat loop (`agent/chat.py`) +
CLI `ask`/`brief`/`chat` are deleted; the AI categorizer `categorize/llm.py` and
the `categorize`/`recategorize` CLI commands are deleted too (F1) — `import`/
`intake` keep only deterministic `categorize/rules.py`. The dashboard's
**inference endpoints** (`/api/chat`, `/api/categorize`, `/api/recategorize`,
`/api/normalize`) are **RETIRED** with their backing modules (their capability
moves to skills, F1); the dashboard's deterministic/visual routes are untouched.
Skills run in the user's session; the server is only a tool provider.

### Render contract (where "clean & beautiful" lives)

Every **read** tool returns both a structured payload and a deterministic,
pre-rendered markdown block:

```json
{ "data": { ... }, "rendered": "<markdown table/bars/kv>" }
```

A unit-tested `agent/render.py` owns all formatting:
- `money(cents: int) -> str` — signed integer cents → `"$1,234.56"`, **never
  float**; negative as `-$X` or `($X)` (chosen once, tested).
- `table(rows, cols)` — right-aligned amounts, optional `%` column, totals row,
  `—` for null.
- `bars(items)` — category-share horizontal bars with `%`.

The skill instruction is: *"Call the tool, print the `rendered` block verbatim,
then add ≤3 sentences of synthesis. Never state a number you did not read from a
tool."* → beauty is **deterministic and regression-tested**, not model-dependent.

### Read tools (guarded read-only connection)

`month_summary`, `category_breakdown`, `top_merchants`, `compare_periods`,
`monthly_trend`, `recurring_charges`, `anomalies`, `budget_overview`,
`income_by_source`, `income_transactions`, `subcategory_breakdown`,
`review_queue`, `insights`, `query_transactions`, `list_notes`, `open_conflicts`.

**Backing-function map (M3) — every read tool is backed by an existing
deterministic function, not asserted.** The tool handler opens
`db.agent_connect()`, calls the named function (passing the guarded `conn` where
the signature accepts one), redacts `payee`/`memo`/`incoming_payee` on the way
out, and wraps the result as `{data, rendered}`:

| Tool | Backing function (verified) |
|---|---|
| `month_summary` | `reports.month_summary(month, conn)` |
| `category_breakdown` | `reports.month_summary(...)` → its `spend_by_category` (no separate fn; ranked + `%` in render) |
| `top_merchants` | `reports.top_merchants(conn, month, limit)` |
| `compare_periods` | two `reports.month_summary(...)` calls; delta composed in the handler |
| `monthly_trend` | `reports.monthly_trend(conn, limit)` |
| `recurring_charges` | `detect.recurring()` (→ `detect.find_recurring`) |
| `anomalies` | `detect.anomalies(sd_threshold)` (→ `detect.find_anomalies`) |
| `budget_overview` | `reports.budget_overview(month)` |
| `income_by_source` | `reports.income_by_source(month)` |
| `income_transactions` | `reports.income_transactions(source, month)` |
| `subcategory_breakdown` | `reports.subcategory_breakdown(category, month)` |
| `review_queue` | `manual.needs_review()` + `manual.checks_to_review()` |
| `insights` | `reports.insights(month)` |
| `query_transactions` | handler-level filtered SELECT (enumerated columns, not `SELECT *` — M1), redacted |
| `list_notes` | `notes.read_notes()` |
| `open_conflicts` | `reconcile.list_open()` (rewritten to an explicit column list, `incoming_payee` redacted — see build constraints) |

(`open_conflicts` is the **read-only** advisory tool the `reconcile` skill uses —
it lists the open `import_conflicts` queue (redacting `incoming_payee`) and the
exact `budget reconcile resolve <id> <action>` CLI command per conflict; it
**writes nothing** (S4, replacing the dropped `resolve_conflict` write tool).)

(`run_sql` is **dropped from v1** — the structured read tools cover the skills,
and a free-form SQL surface is the largest read-side risk. **Deferred:** if
reintroduced, it must run against a PII-free projected read view that excludes
`raw_ofx` / `acct_hash` / `inbox_files.filename` / `import_runs.source_name` /
`import_runs.error_message` (the READ denylist — S3), applies the same
`payee`/`memo`/`incoming_payee` redaction, and never issues a raw `transactions`
SELECT.)

**Build constraints on every read tool:**
- **Enumerate columns — never `SELECT *` (M1).** A `SELECT *` on `transactions`
  (which has `raw_ofx`) or `import_conflicts` (which has nothing denied now, but
  the rule holds uniformly) aborts under the READ authorizer the moment a denied
  column is in the result set. The existing `reconcile.list_open()` uses
  `SELECT * FROM import_conflicts` and must be rewritten to an explicit column
  list when surfaced through a tool. Tools select only the columns they render.
- **Redact `payee` / `memo` / `incoming_payee` on read (F1 / S6).** Any tool that
  returns one of these strings (e.g. `top_merchants`, `query_transactions`,
  `review_queue`, and `open_conflicts`) runs it through
  `sanitize.redact_account_numbers()` before placing it in either `data` or
  `rendered`. The redaction invariant is scoped to **these text-field values
  only** — `has_long_digit_run()` is asserted over the returned `payee`/`memo`/
  `incoming_payee` strings, **not** over the serialized tool output (which also
  contains money amounts; an amount ≥ $10,000 is a legitimate 7-digit run like
  `1000000` cents and must not be flagged — S2). `merchant_norm` is already
  redacted at import and needs no further treatment. For `fitid_collision` the
  incoming charge's merchant text exists **only** in
  `import_conflicts.incoming_payee`, so the reconcile view surfaces that column
  (redacted), not a denied one.

### Write tools (guarded write connection; derived-columns + app-config only)

`set_merchant_category`, `set_txn_category`, `add_custom_category`,
`remove_category`, `set_budget_limit`, `clear_budget_limit`,
`set_expected_income`, `split_subscriptions`, `save_note`, `delete_note`,
`save_brief`. (There is **no** `resolve_conflict` write tool — reconcile is
advisory; the read-only `open_conflicts` tool replaces it — S4.)

**Every write tool opens exactly one `db.agent_connect(write=True)` connection and
THREADS it as an injected `conn` through the refactored backing helper (F1)** —
`set_merchant_category`/`set_txn_category`/`remove_category`/`split_subscriptions`
(manual.py), `set_budget_limit`/`clear_budget_limit`/`set_expected_income`
(budgets.py), and `add_custom_category` (categories.py) are all refactored to
accept and use that `conn` instead of opening their own (see §1 + the de-wiring
table). A write tool therefore executes *inside* the authorizer; it never falls
back to `db.connect()`, which would bypass it.

These write **only** the derived `transactions` columns
(`category`/`subcategory`/`category_source`) and the app-config tables
(`category_rules`, `budgets`, `settings`). Confirm-gating is a **skill**
responsibility (and an eval); the authorizer is the safety net — a write tool
*cannot* `INSERT`/`DELETE` a `transactions` row, `UPDATE` an imported-fact column
(`amount_cents`/`posted_date`/`payee`/`memo`/`status`/`txn_type`/…), or touch any
table outside the `{category_rules, budgets, settings}` allowlist (incl.
`import_conflicts`) regardless of skill behavior. The one write property the
**authorizer cannot** enforce is pinned at the tool layer: the `settings`
**KEY-whitelist** — only the writer set `{add_custom_category, remove_category,
set_expected_income}` writes `settings`, and only the fixed keys
`{custom_categories, hidden_categories, expected_monthly_income_cents}`
(`add_custom_category` writes `custom_categories` and, via unhide,
`hidden_categories`; `remove_category` writes `hidden_categories` +
`custom_categories`; `set_expected_income` writes
`expected_monthly_income_cents` — the real key, `budgets.EXPECTED_INCOME_KEY` —
S1/S2). `status` is no longer a tool concern — the authorizer denies it
outright (S4). **Like every write tool, `split_subscriptions` opens ONE
`db.agent_connect(write=True)` and threads it into the refactored
`manual.split_subscriptions(conn)` (F1)** — so it runs *inside* the authorizer,
not on a fresh `db.connect()`. Under that guarded connection the ported tool
**drops** the in-tool `merchants.seed_builtin_aliases()` call (S2): that INSERT
targets `merchant_aliases`, a non-allowlisted write table, so under
`db.agent_connect(write=True)` the seed **would abort the tool** — that is exactly
the authorizer doing its job, and exactly why it must go. The seed is also
**redundant** — `init_schema` already seeds built-in aliases — so the tool reads
aliases via the read-only `active_aliases(conn)` (a read on `merchant_aliases`,
which the READ side allows) and writes only `transactions.subcategory` +
`category_rules` (both allowlisted).
`save_note`/`save_brief` are **file-backed**, not DB writes (see API surface).

---

## Section 4 — Eval Strategy

Skills are no-code markdown, so they are evaluated **behaviorally, by structural
parity** (mirrors fitness's shadow-run/fingerprint approach) — never by
asserting verbatim model prose. The harness drives each skill against a
**fabricated fixture `budget.db`** (shared with unit tests, zero real PII) and
asserts properties of the transcript + tool-call log.

Six assertion families cover every skill:

| Family | Catches | Example |
|---|---|---|
| **Grounding / invention-rate** | hallucinated numbers | every figure traces to a recorded tool result; `invention_rate == 0` (defined below) |
| **Tool-call correctness** | skipping required reads | `categorize` calls `review_queue` before proposing |
| **Confirm-gate** | writing without consent | no write-tool call before user "yes"; write fires after |
| **Render fidelity** | "beautiful" regressing | output contains the tool's `rendered` block verbatim |
| **Structure** | missing brief sections | `monthly-brief` emits spent/income/net → where-it-goes → ways-to-save → flags |
| **Safety** | PII / ledger leaks | only `acct_last4` ever shown; no `transactions` INSERT/DELETE or imported-fact UPDATE attempted |

**Invention-rate, defined precisely (M3).** For a skill turn, let `F` be every
numeric/`$` figure in the rendered output and `T` the set of numeric leaves in
that turn's recorded tool-result JSON. Normalize each figure to **integer cents**:
strip `$`, thousands separators, and the sign wrapper (leading `-` or `(...)`);
parse `D.CC`/`D` to cents. A figure is *grounded* iff its cents value equals some
leaf in `T` **or** a value the deterministic render contract itself produced from
`T` (a totals-row sum or a `%` share). `invention_rate = |ungrounded figures| /
|F|`. The metric's **hard bound `invention_rate == 0`** is a gate **only when
computed over real model output** — i.e. on the deferred LIVE tier (over the
fabricated fixture, not "≈ 0"). In v1 only the *normalizer/checker* is unit-tested
(S2 below); the bound is not a v1 gate.

**What v1 can and cannot test — be honest (S2).** The six families above are
*behavioral* properties of the running skill. A deterministic tier makes **no
model calls**, so it cannot exercise any of them against the actual `.md` skill:
with no model in the loop every figure in a "transcript" comes from
hand-authored / recorded tool JSON, which makes `invention_rate == 0`
**tautological** (it tests that the fixture is internally consistent, not that
the model invented nothing); likewise confirm-gate ordering, tool-call
correctness, structure, and safety-on-real-output, asserted against a
hand-authored transcript, test the **fixture, not the skill**. Catching
model-side hallucination or a skill that writes before "yes" **requires the
deferred LIVE tier**.

**v1 tier — deterministic, in CI (the only tier built in v1):** v1 ships exactly
the determinism it can actually prove without a model: (a) `render.py`
snapshots (money/table/bars), (b) tool-handler output snapshots and the
account-number **redaction-on-read** snapshots on the fabricated `budget.db`,
(c) the `agent_connect()` authorizer allow/deny tests, and (d) the
**eval-harness LOGIC tests** — unit tests of the `invention_rate` normalizer,
the confirm-gate/tool-call/structure checkers, and the safety scanner, run
against tiny synthetic inputs to prove the *checkers themselves* are correct.
The behavioral skill gates (invention-rate on real output, confirm-gate
ordering, tool-call correctness, structure, safety-on-real-output) are **NOT v1
gates** — they are deferred to the live tier below and, until then, a documented
manual check. Runs in CI always; costs nothing.

**Deferred — live model-driven tier (documented, NOT built in v1):** a
`budget eval <skill>` runner that drives the actual model, fingerprints each run
to `tests/evals/baseline.json`, and gates on drift. Because `claude-agent-sdk` is
removed and no API key is allowed, the runner would be a `claude -p` headless
subprocess on the committed `.mcp.json` (subscription auth), with cost
**estimated up front, a hard `--max-spend` gate in code, a mock mode, and an
explicit `--capture` re-baseline**. This tier — and `baseline.json` — are
deferred to a later version per the lean-v1 scope (M4); v1 ships the harness
checker-logic tests + render/redaction snapshots, and the live behavioral gates
(`invention_rate == 0`, confirm-gate ordering, …) are a **documented manual
check** until then.

---

## Section 5 — Public-Repo Hygiene

Committed code = the **app layer**; personal data = gitignored `data/`. The
"personal vs app separation" goal is a *publishing* property, satisfied by never
committing personal data — independent of the runtime read-boundary.

**Audit result (2026-06-29):** repo already clean — no tracked DB/key/env, no
hardcoded personal paths, no inline secrets, gitignore thorough, fixtures
fabricated, `scripts/secret-scan.sh` present.

**Carries over unchanged:** `data/` (now the single `budget.db` + `local_key` +
inbox; **no `agent.db`**), `logs/`, `backups/`, `.env`, raw-export globs stay
gitignored; `secret-scan.sh` stays in the pre-commit path.

**New for this redesign:**
- **Briefs & notes are file-backed** (the fitness approach): `notes` stay in the
  existing plain-text `data/user_notes.md` (already the agent's only filesystem
  write path), and `save_brief` writes a markdown file under a **gitignored
  `data/briefings/` dir** — so `save_note`/`save_brief` need **no DB tables**
  (there is no `user_notes`/`briefs` table, and none is added). These writes are
  **outside the authorizer** (filesystem, not SQLite), so `save_brief` must
  self-guard against path traversal: `period` is validated/slugified against
  `^[0-9]{4}-[0-9]{2}$|^all$|^last\d+$` and the resolved output path is confined
  under `data/briefings/` — anything escaping it is rejected (S7).
- **Env knobs** (project-relative defaults that work in a fresh clone, documented
  in `.env.example`): `LOCAL_BUDGET_DATA_DIR`, `LOCAL_BUDGET_MODEL`. The stdio MCP
  server needs no host/token (loopback only). (`LOCAL_BUDGET_EVAL_MAX_SPEND` ships
  only with the deferred live-eval tier.)
- **`.mcp.json` is committed** with only a relative invocation
  (`uv run budget-mcp`), never an absolute path.
- **CI** (`.github/workflows/ci.yml`): ruff + unit tests + coverage gate +
  `secret-scan.sh` on every push. The deterministic eval tier runs in CI.
- **README rewrite** for the agent-first story + a fresh-clone quickstart.
- **Deferred (with the live-eval tier):** a fingerprints-only `baseline.json`
  (tool names, section counts, `invention_rate`, n-figures — **no `$` amounts or
  real merchant strings**, holding by construction from the fabricated fixture).
  Not committed in v1 since the live tier is not built.

---

## Section 6 — Migration Phasing

Each phase is independently committable **and the package imports cleanly after
every phase (the S1 ordering invariant)** — the `agent_db.py` read-path deletion
and the `agent/tools.py` rewrite that depends on it land in the **same** phase
(Phase 1). The dashboard's **deterministic/visual layer is untouched throughout**
(its **AI routes** `/api/chat`, `/api/categorize`, `/api/recategorize`,
`/api/normalize` are retired as their backing modules are deleted — F1).

| Phase | Work | Gate |
|---|---|---|
| **0 · Data model** | one `budget.db`; the **bare** column-level `agent_connect()` authorizer (additive — `agent_db.py` is left importable so `agent/tools.py` still imports, S1); run the §1 de-wiring for everything **independent of the agent read path** — thread `conn`/repoint the write+importer call sites (importer, intake, normalize, reconcile, cli, `categorize/manual.py`, `budgets.py`, `categories.py`), drop the `paths.py` `agent.db` helpers + the `budget verify` command, delete `sync.py` + projection/staleness machinery + the `db.py` helpers; rewrite `test_security.py` against `agent_connect()` | authorizer + rewritten security tests green; **package still imports** (`agent/tools.py` untouched, `agent_db.py` still present) |
| **1 · Tools + render** | **delete `agent_db.py` in full and rewrite `agent/tools.py` against `agent_connect()` + rewrite `test_agent_tools.py` — together, so imports never break (S1)**; `render.py` (money/table/bars, snapshot-tested); read tools return `{data, rendered}` over the §3 backing map with payee/memo/incoming_payee redaction-on-read; **write tools each open one `agent_connect(write=True)` threaded through the refactored `conn`-accepting helpers (F1)** | handler + render + redaction snapshots tested; **authorizer exercised through the write-tool entry points** (F1); rewritten `test_agent_tools.py` green |
| **2 · MCP server** | `web/mcp_server.py` (stdio) + committed `.mcp.json`; drop `claude-agent-sdk`; delete `agent/chat.py` + CLI `ask`/`brief`/`chat`; **retire `web/routes.py`'s AI routes** (`/api/chat`, `/api/categorize`, `/api/recategorize`, `/api/normalize`) + their dead `llm`/`agent.chat` imports, and remove/rewrite their `test_web.py` tests (`test_chat_*`, `test_normalize_*`) — F1 | **automatable:** server process starts + registers all tools; deterministic dashboard route tests green, retired-route tests removed; **manual:** live session calls each tool (M3) |
| **3 · Skills** | 8 self-contained `SKILL.md` + shared persona | inspection: no executable code |
| **4 · Evals** | deterministic harness LOGIC tests in CI (checker unit tests + render/redaction snapshots); 1 spec/skill; behavioral skill gates + live tier **deferred** | harness-checker logic + render/redaction snapshots green (behavioral `invention_rate == 0` is a deferred live gate — S2) |
| **5 · Publish** | CI workflow, `.env.example`, README rewrite, coverage gate, secret-scan | fresh-clone invariant holds |

---

## API Surface

Tool signatures (Python handler shapes; period = `"YYYY-MM" | "all" | "lastN"`).
Read tools return `{ data, rendered }`; write tools return `{ ok, summary }`.

```python
# --- read (db.agent_connect(), read-only) ---
month_summary(period: str | None) -> dict          # spent/income/net + counts
category_breakdown(period) -> dict                 # ranked categories + %
top_merchants(period, limit: int = 8) -> dict
compare_periods(a: str, b: str) -> dict
monthly_trend(limit: int = 24) -> dict
recurring_charges() -> dict
anomalies(sd: float = 2.0) -> dict
budget_overview(period) -> dict                    # spend vs limits
income_by_source(period) -> dict
income_transactions(source: str, period) -> dict
subcategory_breakdown(category: str, period) -> dict
review_queue() -> dict                             # needs_review + checks_to_review
insights(period) -> dict                           # deterministic ways-to-save
query_transactions(filters: dict) -> dict
list_notes() -> dict
open_conflicts() -> dict                           # advisory: open import_conflicts
                                                   # (incoming_payee redacted) + the exact
                                                   # `budget reconcile resolve <id> <action>`
                                                   # CLI command per conflict. READ-ONLY (S4).
# (run_sql dropped from v1 — see §3; deferred behind a PII-free projected view)

# --- write (db.agent_connect(write=True): {category,subcategory,category_source}
#     on transactions + {category_rules,budgets,settings} ONLY — default-DENY, S1/S4) ---
set_merchant_category(merchant_norm: str, category: str, subcategory: str | None) -> dict
                                                   # UPDATE transactions.category/subcategory + category_rules
set_txn_category(txn_id: int, category: str, subcategory: str | None) -> dict
                                                   # tool name `set_txn_category`; backing helper is
                                                   # manual.set_transaction_category(conn, …) — alias/rename,
                                                   # no `set_txn_category` exists in the core (M1).
                                                   # UPDATE transactions.category/subcategory (one row)
add_custom_category(name: str) -> dict             # settings: custom_categories
                                                   #   (+ hidden_categories via unhide) — S1
remove_category(name: str, merge_into: str) -> dict  # re-point transactions/category_rules/budgets +
                                                   # settings: hidden_categories + custom_categories — S1
set_budget_limit(category: str, amount_cents: int, subcategory: str | None) -> dict   # budgets
clear_budget_limit(category: str, subcategory: str | None) -> dict                    # budgets
set_expected_income(cents: int) -> dict            # settings
# (no resolve_conflict write tool — reconcile is advisory; ALL resolution is the
#  `budget reconcile resolve <id> <action>` CLI under db.connect(). The authorizer
#  denies transactions.status, transactions INSERT/DELETE, amount/date UPDATE, and
#  the entire import_conflicts table to skills — S4. Read via open_conflicts.)
split_subscriptions() -> dict                      # UPDATE transactions.subcategory + category_rules ONLY.
                                                   # DROPS the in-tool seed_builtin_aliases() INSERT into
                                                   # merchant_aliases (non-allowlisted table → would abort
                                                   # under agent_connect(write=True)); that seed is redundant
                                                   # (init_schema already seeds aliases). Relies on the
                                                   # READ-ONLY active_aliases() — S2.
save_note(text: str) -> dict                       # FILE-backed: appends to data/user_notes.md (no DB)
delete_note(line: int) -> dict                     # FILE-backed: data/user_notes.md
save_brief(period: str, payload: dict) -> dict     # FILE-backed: writes data/briefings/<period>.md (no DB).
                                                   # `period` is FILE-PATH-SENSITIVE and OUTSIDE the authorizer,
                                                   # so the tool SELF-GUARDS (S7): validate/slugify against
                                                   # ^[0-9]{4}-[0-9]{2}$|^all$|^last\d+$, reject otherwise, then
                                                   # resolve the output path and confirm it stays CONFINED under
                                                   # data/briefings/ (reject any path that escapes, e.g.
                                                   # period="../../x"). Never interpolate `period` into a path raw.

# --- connection factory ---
db.connect() -> Connection            # full RW: importer, CLI, dashboard
db.agent_connect(write: bool=False) -> Connection   # column-level authorizer-guarded

# --- render (pure, unit-tested) ---
render.money(cents: int) -> str
render.table(rows: list[dict], cols: list[Col]) -> str
render.bars(items: list[dict]) -> str
```

---

## Invariants

### Checkable by inspection
- No `claude-agent-sdk` import anywhere in `src/`; absent from `pyproject.toml`.
- No `ANTHROPIC_API_KEY` referenced in app code.
- `.claude/skills/budget-*/SKILL.md` contain **no executable code blocks** (only
  directions/guardrails); each names its tools, its confirm-gate, and the
  "print `rendered` verbatim" instruction.
- `.mcp.json` invocation is relative (no absolute/personal path).
- `data/` (incl. `data/briefings/`), `logs/`, `backups/`, `.env`, raw-export
  globs remain gitignored; nothing personal is tracked.
- No `sync.py`, `mutating_connect`, `bump_gen`, `current_gen`,
  `sanitized_projection`, or the `paths.py` `agent.db` helpers (`agent_db_path`,
  `agent_db_tmp_path`, `expected_gen_path`, `write_expected_gen`) remain in
  `src/`; no `agent.db` or `.expected_gen` marker is created; the `budget verify`
  CLI command is removed.
- Every read tool's return path includes a `rendered` field.

### Testable (require tests)
- **Authorizer is exercised through the TOOL entry points (F1), not just in
  isolation.** A raw `agent_connect()` allow/deny suite proves the authorizer
  *rules*; but because a write tool that opened its own connection would silently
  bypass the authorizer, the binding test calls the **actual write tools** and
  asserts the gate fires *through them*: a write tool is **denied** when its
  refactored helper is pointed at a non-allowlisted target — e.g. a tool path that
  attempts `transactions.status`, `merchant_aliases`, or any non-allowlisted
  table/column aborts under the tool's own `db.agent_connect(write=True)`. (This is
  what catches the "isolated authorizer passes but the tool bypasses it" failure —
  the helpers thread the tool's guarded `conn`, they never re-open `db.connect()`.)
- `agent_connect()` **raises** on `INSERT`/`DELETE` of a `transactions` row.
- `agent_connect()` **raises** on `UPDATE` of any non-allowlisted `transactions`
  column — the imported facts (`amount_cents`, `posted_date`, `payee`, `memo`,
  `raw_ofx`, `fitid`, `account_id`, `merchant_norm`) **and**
  `status`, `txn_type`, `imported_at`, `import_run_id` (S4).
- **Default-DENY allowlist (S1):** `agent_connect()` **raises** on `UPDATE` of an
  *unlisted* fact column — e.g. `txn_type` — and on **any** write (INSERT/UPDATE/
  DELETE) to an *unlisted* table — e.g. `merchant_aliases` (and likewise
  `normalize_changes`, `import_runs`, `inbox_files`, `accounts`,
  `import_conflicts`). The rule is an allowlist: anything not explicitly allowed
  is denied.
- `agent_connect()` **allows** `UPDATE` of the derived columns ONLY
  (`category`, `subcategory`, `category_source`) — `status` is **not** allowed.
- READ denylist: `agent_connect()` **denies** reading `transactions.raw_ofx`,
  `accounts.acct_hash`, `inbox_files.filename`, `import_runs.source_name`, and
  `import_runs.error_message` (the statement aborts — S3);
  `transactions.payee`/`.memo`, `import_conflicts.incoming_payee`, and
  `accounts.acct_last4` **are** readable (decision #4 / S6).
- **Redaction-on-read (F1/S6/S2):** for a fabricated fixture whose `payee` /
  `memo` / `incoming_payee` carry an account-number-bearing description
  (Zelle/wire), for **every** read tool that surfaces those columns,
  `sanitize.has_long_digit_run()` is False over the returned **`payee`/`memo`/
  `incoming_payee` string values themselves** — NOT over the full serialized tool
  output (which also contains money amounts; a ≥ $10,000 amount is a legitimate
  7-digit cents run and must not be flagged — S2). Asserts the tool-layer
  redaction, since the column stays raw at rest.
- `accounts` account numbers hold **by construction**: no full account number
  exists at rest (masked at import → `acct_last4` + HMAC `acct_hash`); the only
  account label a skill can read is `acct_last4`.
- `agent_connect()` **allows** writes ONLY to the table allowlist
  `{category_rules, budgets, settings}`. (The authorizer allows the `settings`
  *table*; it does **not** enforce the key-whitelist — S1.) Every other table,
  incl. `import_conflicts`, is denied (S4).
- **Settings key-whitelist (tool-enforced, S1):** a tool-level test asserts that
  only the writer set `{add_custom_category, remove_category, set_expected_income}`
  writes `settings`, and only over the key set `{custom_categories,
  hidden_categories, expected_monthly_income_cents}` (`add_custom_category` →
  `custom_categories` + `hidden_categories` via unhide; `remove_category` →
  `hidden_categories` + `custom_categories`; `set_expected_income` →
  `expected_monthly_income_cents` (= `budgets.EXPECTED_INCOME_KEY`, S2)). No
  settings write occurs outside that writer/key set; an
  attempt to write any other settings key via the tool layer is rejected. (The
  authorizer cannot see the row key, so this is not an `agent_connect()`
  assertion.)
- **split_subscriptions writes only allowlisted targets (S2):** the ported tool
  drops the `seed_builtin_aliases()` INSERT into `merchant_aliases`; a test asserts
  it completes under `db.agent_connect(write=True)` and writes only
  `transactions.subcategory` + `category_rules` (no `merchant_aliases` write), and
  that it reads aliases via read-only `active_aliases()`.
- **Reconcile is advisory — no skill write path (S4):** there is no
  `resolve_conflict` write tool; `agent_connect()` denies `transactions.status`,
  `transactions` INSERT/DELETE, `amount_cents`/`posted_date` UPDATE, and the whole
  `import_conflicts` table. The read-only `open_conflicts` tool returns each open
  conflict with the exact `budget reconcile resolve <id> <action>` CLI string and
  performs no write — a test asserts it opens a read-only connection.
- **save_brief path confinement (tool-enforced, S7):** `period` values outside
  `^[0-9]{4}-[0-9]{2}$|^all$|^last\d+$` (e.g. `"../../x"`) are rejected, and the
  resolved write path is asserted to stay under `data/briefings/`.
- `agent_connect()` denies `ATTACH` and `PRAGMA` writes.
- `db.connect()` (importer path) can still `INSERT`/`DELETE` `transactions` rows
  (immutability is skill-scoped, not global).
- `render.money()` never uses float arithmetic; exact-string snapshots locked
  for positive, negative, zero, and large values.
- Read-tool `rendered` blocks match snapshot fixtures.
- **Eval-harness checker LOGIC (v1, S2):** unit tests of the harness primitives on
  tiny synthetic inputs — the `invention_rate` normalizer maps `$1,234.56`/`-$X`/
  `($X)` to the right cents and flags an injected ungrounded figure; the
  confirm-gate, tool-call, structure, and safety checkers each fire on a crafted
  bad transcript and pass on a good one. These prove the *checkers*, not any
  skill's behavior.
- **Behavioral skill gates are DEFERRED to the live tier (S2):** `invention_rate
  == 0` on real model output, confirm-gate ordering, tool-call correctness,
  structure, and safety-on-real-output are **not** v1 gates (a no-model harness
  cannot exercise them — it would only re-assert hand-authored fixtures). Until
  the live tier exists they are a documented manual check.
- `tests/test_security.py` and `tests/test_agent_tools.py` are **rewritten** to
  exercise `db.agent_connect()` on the single `budget.db` (column-level
  allow/deny), replacing every file-absence assertion of the old two-DB model.
- Coverage gate (~85%) on the deterministic core passes in CI.
- **Fresh-clone invariant:** unit tests + lint + MCP server start succeed with
  zero env setup and zero personal data.
- Regression: the **deterministic dashboard routes** (charts, data,
  budgets/reports views) and their `test_web.py` tests stay green. The **retired
  AI routes** (`/api/chat`, `/api/categorize`, `/api/recategorize`,
  `/api/normalize`) and their tests (`test_chat_*`, `test_normalize_*` in
  `test_web.py`) are **removed/rewritten**, not kept green (F1).

---

## Acceptance Criteria

1. `grep -r agent.db src/` is clean; `sync.py` + `test_projection_refresh.py`
   deleted; `mutating_connect`/`bump_gen`/`current_gen`/`sanitized_projection`
   removed from `db.py`; the `paths.py` `agent.db` helpers (`agent_db_path`,
   `agent_db_tmp_path`, `expected_gen_path`, `write_expected_gen`) and the
   `budget verify` CLI command removed; single `budget.db` in use.
   **`tests/test_security.py` and `tests/test_agent_tools.py` are rewritten
   against `db.agent_connect()` (column-level deny on the one DB); and the test
   files that import the deleted read-path — `test_categorize_llm.py`,
   `test_subcategories.py`, `test_reports.py`, `test_import.py`,
   `test_manual_categorize.py`, `test_db.py` — are repointed to `db.connect()`
   (bump/refresh assertions dropped). `test_categorize_llm.py` is deleted with
   `categorize/llm.py`; and `test_web.py`'s retired-endpoint tests (`test_chat_*`,
   `test_normalize_*`, which import `agent.chat` / `categorize.llm`) are
   removed/rewritten with the retired `/api/chat` + `/api/normalize` routes (F1/M2).
   None of these "stay green unchanged."**
2. Authorizer unit tests cover all rules above (allow-derived-UPDATE /
   deny-fact-UPDATE / deny-INSERT-DELETE / READ-denylist / allow-config-write /
   ATTACH+PRAGMA-deny).
3. Every read tool returns `data` + `rendered`; `render.py` snapshots locked;
   `money()` float-free.
4. `claude-agent-sdk` removed from `pyproject.toml`; `agent/chat.py` + CLI
   `ask`/`brief`/`chat` removed; **`categorize/llm.py` deleted and the
   `categorize`/`recategorize` CLI commands removed (F1)**; `budget import` /
   `budget intake` apply **only** deterministic `categorize/rules.py` (no AI step;
   everything else lands `Uncategorized` for the `categorize` skill); no
   `claude-agent-sdk` import remains in `src/`; no API key in code.
5. **Automatable (CI):** the stdio MCP server **process starts and registers all
   tools** (a headless start + list-tools assertion, no model session); read-tool
   output shows only `acct_last4` and is account-number-clean (redaction-on-read
   snapshots). **Manual (not CI-automatable, M3):** connecting from a live Claude
   Code session in the repo dir and calling each tool end-to-end.
6. 8 self-contained `SKILL.md` files, no executable code, each with tools +
   confirm-gate + render instruction.
7. One eval spec per skill; **deterministic tier in CI**; the live model-driven
   tier (`--max-spend`, mock mode, `baseline.json`) is **deferred and documented,
   not built in v1**.
8. Coverage gate (~85%) on deterministic core; CI green (ruff + tests +
   secret-scan).
9. Fresh clone runs unit tests + lint + MCP server start with zero env, zero
   personal data. **v1 gate = deterministic harness LOGIC tests + render /
   redaction snapshots** (the `invention_rate` normalizer and the
   confirm-gate/tool-call/structure/safety checkers are unit-tested on synthetic
   inputs — §4/S2). The **behavioral** `invention_rate == 0` over real model
   output (and confirm-gate ordering, etc.) is a **deferred live gate + documented
   manual check**, NOT a v1 gate.
10. The **deterministic dashboard routes** (charts, data, budgets/reports views)
    and their `test_web.py` tests still green (no regression). The dashboard's
    **AI routes** (`/api/chat`, `/api/categorize`, `/api/recategorize`,
    `/api/normalize`) are **retired** and their tests (`test_chat_*`,
    `test_normalize_*`) removed/rewritten (F1). The two security/agent-tool test
    files are **rewritten** for the one-DB column model.

## Failure modes considered

- **Hallucinated numbers** → render-from-tool contract (skills print tool output,
  don't compose figures) + render snapshots in v1; the behavioral
  `invention_rate == 0` check is a **deferred live gate** (a no-model harness
  can't catch model-side invention — S2). v1 only unit-tests the invention-rate
  *checker* logic.
- **Skill corrupts the ledger** → default-DENY authorizer: skills write ONLY
  `{category, subcategory, category_source}` on `transactions` plus
  `{category_rules, budgets, settings}`. `transactions` INSERT/DELETE, every other
  column (incl. `status`, `txn_type`), and every other table (incl.
  `import_conflicts`) are denied; allowlist unit tests cover an unlisted column
  (`txn_type`) and an unlisted table (`merchant_aliases`) — S1. **Reconcile is
  fully advisory (S4):** EVERY resolution — including the formerly-"non-destructive"
  `mark_distinct` (which writes `status`) — is a `budget reconcile resolve` CLI
  handoff under `db.connect()`; there is no `resolve_conflict` write tool.
- **Import silently stops AI-categorizing (F1)** → by design, `budget import` /
  `budget intake` now apply ONLY deterministic `categorize/rules.py`; merchants
  with no rule land `Uncategorized`. This is surfaced (the import summary reports
  the uncategorized count) and the `categorize` skill — which inherited the AI
  capability from the deleted `categorize/llm.py` — works that queue. No app-path
  AI categorization remains.
- **Account number leak via merchant text** → `payee`/`memo`/`incoming_payee` are
  stored RAW and CAN carry an account number (Zelle/wire); every read tool runs
  them through `sanitize.redact_account_numbers()` before returning
  (redaction-on-read, F1/S6); a fixture-based invariant asserts
  `has_long_digit_run()` is False over the returned `payee`/`memo`/`incoming_payee`
  **string values** (not the full output, which legitimately carries ≥7-digit
  money amounts — S2). **Residual (M1):** this strips only ≥7-digit runs, not
  5–6-digit fragments or P2P counterparty NAMES — the accepted sanitize I14
  residual; `payee` is not claimed PII-clean.
- **Account identity leak (`accounts`)** → no full number exists at rest (masked
  at import); the READ authorizer also denies `accounts.acct_hash`; only
  `acct_last4` is shown.
- **Brief path traversal** → `save_brief` is outside the authorizer (filesystem),
  so it self-guards: `period` validated/slugified and the output path confined
  under `data/briefings/` (S7).
- **Eval spend blowout** → the v1 deterministic tier makes no model calls and
  costs nothing; the deferred live tier carries the `--max-spend` + mock-mode +
  estimate-first design before it is built.
- **Fresh clone broken by a personal assumption** → fresh-clone invariant test;
  env-driven defaults; relative `.mcp.json`.
- **Dashboard regression from the DB collapse** → the deterministic/visual layer
  keeps `db.connect()` full access; its `test_web.py` route tests are the
  regression gate. The AI endpoints (`/api/chat`, `/api/categorize`,
  `/api/recategorize`, `/api/normalize`) are **retired by design** (their modules
  `agent/chat.py` + `categorize/llm.py` are deleted, F1), so `test_chat_*` /
  `test_normalize_*` are removed/rewritten rather than treated as a green gate.
