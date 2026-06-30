# Phase 1 — De-wire agent.db; retarget the agent read layer onto budget.db — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use crucible:build to implement this plan task-by-task. This is a REFACTOR (preserve-behavior); existing tests stay green except the agent.db-projection tests, which are deliberately deleted/rewritten.

**Goal:** Remove the entire agent.db projection/staleness machinery and make the in-app agent read `budget.db` directly through the Phase-0 `db.agent_connect()` authorizer — one DB, one boundary.

**Architecture:** The two-DB design kept a sanitized `agent.db` rebuilt after every mutation. Phase 0 added `db.agent_connect()` (the column-level authorizer on `budget.db`). Phase 1 deletes the projection (`sync.py`, `agent_db.py`, the `db.py` projection/gen helpers, the `paths.py` agent.db helpers) and retargets `agent/tools.py` from `agent.db.txn` to `budget.db.transactions` via `agent_connect()`. This is ONE atomic migration: the deterministic write paths stop calling `bump_gen`/`refresh_agent_db`, the helpers are deleted, and the coupled tests are rewritten/deleted together so the suite is green at the phase boundary.

**Atomic:** true — intermediate commits may have a red suite; the phase-final commit MUST be green. **Restructuring-only:** false (removes the agent.db side-effect; changes the agent's data source). **Tests to verify:** the full suite.

**Out of scope (later phases):** removing `claude-agent-sdk` + `agent/chat.py` + CLI `ask`/`brief`/`chat` (Phase 2); building the new `{data,rendered}` tool surface + `render.py` + write tools + stdio MCP server (Phase 2–3); dropping `run_sql` (Phase 2). This phase KEEPS the SDK tool structure and only changes its data source.

**Design ref:** `docs/plans/2026-06-29-budget-true-agent-design.md` §1 (de-wiring table) + §6 Phase 1. Phase-0 landed `db.agent_connect()` (commit `3a6e143`).

**Behavior to preserve (the old projection):**
```sql
SELECT t.txn_id, t.account_id, t.posted_date, t.amount_cents, t.category,
       t.subcategory, t.category_source, t.merchant_norm,
       a.acct_last4 AS account_last4, t.txn_type
FROM transactions t JOIN accounts a ON a.account_id = t.account_id
WHERE t.status = 'posted'
```
Every retargeted tool query must keep `WHERE status='posted'` and join `accounts` for `account_last4`. Conflicts (old `conflicts_by_month` table) are now computed live via `reports.unresolved_conflicts(conn, month)` (status='conflict').

---

### Task 1: De-wire the deterministic write paths (mutating_connect → connect; drop bump/refresh)

- **Atomic:** true. **Restructuring-only:** true (no signature/control-flow change beyond removing the projection side-effect).
- **Tests to verify:** test_import, test_intake_dedup, test_normalize, test_manual_categorize, test_subcategories (these exercise the write paths).

**Files (per the §1 de-wiring table — convert `db.mutating_connect()`→`db.connect()`; delete every `db.bump_gen(...)` / `db.current_gen(...)` / `sync.refresh_agent_db()` line; remove the `sync` import):**
- `ingest/importer.py` — `bump_gen` ×4 (lines ~75,87,96,213) + `sync.refresh_agent_db()` (~230) + `sync` import.
- `intake.py` — `bump_gen` (~183) + `sync.refresh_agent_db()` (~208) + `sync` in the line-15 import.
- `normalize.py` — `mutating_connect` (~288) + `bump_gen` (~129) + `sync.refresh_agent_db()` (~131) + `sync` in line-19 import.
- `reconcile.py` — `bump_gen` (~55) + `sync.refresh_agent_db()` (~58) + `sync` in line-18 import.
- `categorize/manual.py` — `mutating_connect` ×5 (~28,52,85,156,172) + `bump_gen` (~230) + `sync.refresh_agent_db()` (~232) + `sync` import.
- `categorize/llm.py` — `mutating_connect` (~116) + `bump_gen` (~91) + `sync.refresh_agent_db()` (~97) + remove `sync` from its top-of-module import.
- `cli.py` — FOUR distinct commands are touched; do NOT conflate the `setup` and `verify` edits (the `sync.refresh_agent_db()` at cli.py:26 is inside `setup`, NOT `verify` — following the old "delete verify, drop sync" wording would leave `setup()` calling an unimported `sync` → `NameError` on `budget setup`, which ships silently since no test runs setup; only the Task-5 grep would catch it):
  - (a) **`setup` command:** delete the `sync.refresh_agent_db()` line at cli.py:26 — `db.init_schema()` (cli.py:25) alone now suffices.
  - (b) **`reset` command:** `mutating_connect` (~95) → `connect`.
  - (c) **`verify` command:** delete the entire `verify` function (cli.py ~414-420, which calls `sync.verify()`). It is registered via the `@main.command()` DECORATOR, so deleting the decorated function is sufficient — there is no `main.add_command` to remove for it (the only `main.add_command` is for `reconcile_cmd`).
  - (d) **`reconcile_cmd`:** remove the now-false `if res["rebuilt"]: click.echo("  ✓ agent.db rebuilt")` echo at cli.py ~407-408; stop depending on `reconcile.resolve`'s `rebuilt` key for that message (the key may remain in the returned dict — just don't print the stale agent.db line). NOTE: this string is `"agent.db"` (a DOT), so the underscore `agent_db` grep in Task 3/5 does NOT catch it — remove it by hand here.
  - Then remove `sync` from the line-12 import.

**Pattern per site:** `with db.mutating_connect() as conn: ...` → `with db.connect() as conn: ...` (drop the trailing `bump_gen(conn)` if any). A `bump_gen` that is the last statement inside the `with` is deleted. A standalone `sync.refresh_agent_db()` after the `with` block is deleted.

**Steps:** (1) grep `mutating_connect\|bump_gen\|current_gen\|refresh_agent_db\|import sync\|, sync\|sync,` in src; (2) convert each site; (3) `uv run python -c "import local_budget.cli"` → no ImportError; (4) `uv run pytest tests/test_import.py tests/test_normalize.py tests/test_manual_categorize.py tests/test_intake_dedup.py -q` — these may now FAIL only where they assert agent.db rebuild (handled in Task 4) — note which; the non-agent.db assertions must pass. (5) Commit `refactor(phase1): drop bump_gen/refresh wiring from write paths`.

---

### Task 2: Retarget `agent/tools.py` onto budget.db via `agent_connect`

- **Atomic:** true — interface of `_ro()`/`_with_ro_conn` changes; all tool handlers depend on it.
- **Tests to verify:** test_agent_tools.py (rewritten in Task 4).

**Files:** Modify `src/local_budget/agent/tools.py`; (its test is fixed in Task 4).

**Changes:**
1. Imports: drop `from .. import agent_db`; add `from .. import db, reports`. KEEP `from claude_agent_sdk import create_sdk_mcp_server, tool` (SDK removal is Phase 2).
2. `_ro()` → `db.agent_connect()` (read-only). Delete `agent_db.assert_fresh()` + the `StaleAgentDBError` handling in `_with_ro_conn` and `run_sql` (no staleness gate any more — the authorizer is the boundary).
3. Replace every `FROM txn` with `FROM transactions` **+ `WHERE status='posted'`** (AND-combine with existing WHEREs). Where a tool returns `account_last4`, JOIN accounts: `FROM transactions t JOIN accounts a ON a.account_id=t.account_id WHERE t.status='posted'` and select `a.acct_last4 AS account_last4` (alias columns with `t.` as needed). Tools touched: `get_month_summary`, `get_category_breakdown`, `query_transactions` (the one selecting `account_last4`), `top_merchants`, `compare_periods`, `recurring_charges`, `find_anomalies`, `_uncategorized_for`.
4. `_conflicts_for(conn, month)` → delegate to `reports.unresolved_conflicts(conn, month)` (returns `{count,total_cents}`) — the old `conflicts_by_month` table is gone. Verify `reports.unresolved_conflicts` accepts a `YYYY-MM` month (it calls `reports._scope`; pass the month string as the tools already format it).
5. `run_sql` (tools.py ~226-256): in the tool DESCRIPTION, replace the table name `txn` → `transactions` AND **drop `account_last4` from the column list** — `transactions` has no `account_last4` column (it lives on `accounts.acct_last4`), and run_sql does no JOIN, so an ad-hoc `SELECT account_last4` would error; either remove it from the listed columns or note it requires an explicit `accounts` JOIN. Document `status` as a filterable column, and explicitly acknowledge that run_sql now spans ALL statuses (conflict/non-posted rows included) — unlike the old posted-only `txn` projection. The posted-only parity rule in this plan applies to the STRUCTURED tools (item 3), NOT to run_sql (it is an ad-hoc escape hatch and cannot enforce a fixed WHERE). The authorizer (now `agent_connect`) still blocks raw_ofx/acct_hash reads and all writes — keep the SELECT/WITH keyword guard. (run_sql is dropped entirely in Phase 2, so this retarget is interim.)
6. `save_user_note`/`list_user_notes`/`delete_user_note` — unchanged (file-backed via `notes`).

**Steps:** (1) make the edits; (2) `uv run python -c "import local_budget.agent.tools"` → clean; (3) defer test run to Task 4; (4) Commit `refactor(phase1): agent tools read budget.db via agent_connect (txn->transactions)`.

---

### Task 3: Delete the projection/staleness machinery

- **Atomic:** true — these symbols must be gone only AFTER Tasks 1–2 stop referencing them.
- **Depends on:** Task 1, Task 2.

**Delete:**
- File `src/local_budget/sync.py`.
- File `src/local_budget/agent_db.py`.
- From `src/local_budget/db.py`: `mutating_connect`, `bump_gen`, `current_gen`, `sanitized_projection`, `conflict_aggregates`, `SANITIZED_PROJECTION_SQL`, and the module-docstring sentence about the sanitized projection feeding agent.db.
- From `src/local_budget/paths.py`: `agent_db_path`, `agent_db_tmp_path`, `expected_gen_path`, `write_expected_gen` (and any `harden`/`.expected_gen` references). Leave `budget_db_path`, `data_dir`, `local_key_path`, `harden_db_files` intact.

**Steps:** (1) `git rm src/local_budget/sync.py src/local_budget/agent_db.py`; (2) delete the named helpers from db.py + paths.py; (3) `grep -rnE 'mutating_connect|bump_gen|current_gen|sanitized_projection|conflict_aggregates|SANITIZED_PROJECTION|agent_db|connect_readonly|assert_fresh|agent_gen|expected_gen|write_expected_gen|refresh_agent_db' src/local_budget` → MUST be empty (no dangling refs); (4) `uv run python -c "import local_budget.cli, local_budget.web.routes, local_budget.agent.tools, local_budget.categorize.llm"` → clean; (5) Commit `refactor(phase1): delete sync.py, agent_db.py, and db/paths projection helpers`.

---

### Task 4: Migrate the coupled tests

- **Tests to verify:** the FULL suite green at the end.

**Delete outright (assert the removed projection/gen machinery):**
- `tests/test_projection_refresh.py` (whole file — ~15 agent.db refs).
- In `tests/test_db.py`: the `current_gen`/`bump_gen` test and the `sanitized_projection` test (the 6 projection/gen refs). Keep the rest of test_db.py.
- In `tests/test_import.py`: `test_agentdb_rebuilt_after_import` and `test_quarantined_not_in_agentdb` (agent.db-rebuild assertions). Also remove `agent_db` (and `sync`, if present) from the module-level `from local_budget import ...` line (test_import.py:4) — it ImportErrors at collection once agent_db.py is deleted. Keep the import/dedup tests.
- In `tests/test_categorize_llm.py`: `test_rebuilds_agent_db_with_new_categories` (lines 113-124) — its ENTIRE body asserts the agent.db rebuild (`assert_fresh` + `connect_readonly` + `SELECT ... FROM txn`), so it is a projection test, not removable categorization scaffolding. Delete it outright; OR, if categorization-after-LLM still needs coverage, rewrite it to read the categories back from `budget.db` via `db.connect()`/`db.agent_connect()` (`SELECT DISTINCT category FROM transactions`).

**Rewrite against `db.agent_connect()` / one-DB reality:**
- `tests/test_security.py` (~46 refs — includes `_build_agent_db`/`connect_readonly`/`conflicts_by_month`/`StaleAgentDBError`/`write_expected_gen`) — the heaviest. Re-express each guarantee for the one-DB column model: payee/memo are now READABLE via `agent_connect()` (decision #4) but `raw_ofx`/`acct_hash`/`inbox_files.filename`/`import_runs.source_name`/`error_message` are read-DENIED; writes to fact columns/status/unlisted tables are denied; account numbers are still absent at rest (only `acct_last4`). Drop all `agent_db`/`connect_readonly`/`assert_fresh`/`_build_agent_db` scaffolding. Much of this overlaps `tests/test_agent_connect.py` (Phase 0) — keep test_security.py focused on the redaction-at-import + no-account-number-at-rest guarantees that test_agent_connect doesn't cover; delete duplicated authorizer rows.
- `tests/test_agent_tools.py` (1 ref) — point its fixture at a seeded `budget.db` (reuse the Phase-0 `seeded_db` pattern; insert posted txns) and assert the retargeted tools return correct figures from `transactions WHERE status='posted'`. Delete `test_tool_refuses_when_agentdb_stale` (no staleness any more).
- `tests/test_reports.py`, `tests/test_subcategories.py`, `tests/test_manual_categorize.py`, `tests/test_categorize_llm.py` — these do NOT call `mutating_connect`; their real coupling is a MODULE-LEVEL `from local_budget import ... agent_db ...` line (test_reports.py:6, test_subcategories.py:4, test_manual_categorize.py:6, test_categorize_llm.py:5) that becomes a collection-time `ImportError` the moment `agent_db.py` is deleted. For each file: remove `agent_db` (and `sync`, if present) from that module-level import line, and delete any test body that calls `agent_db.*`/`assert_fresh`/`connect_readonly` (for test_categorize_llm.py the rebuild test is already handled in the Delete-outright list above). Their real subject (reports math, categorization) is unaffected. NOTE: the src-only greps (Task 3 step 3 and Task 5 step 3) do NOT scan `tests/`, so only `uv run pytest -q` catches a missed test import.

**Steps:** (1) delete the whole-file + in-file dead tests; (2) rewrite test_security.py + test_agent_tools.py; (3) repoint the setup-only references in the remaining 4 files; (4) `uv run pytest -q` → iterate to green; (5) Commit `test(phase1): migrate tests to one-DB agent_connect; drop agent.db projection tests`.

---

### Task 5: Phase-1 gate verification

**Steps:**
1. `uv run pytest -q` → full suite green.
2. `uv run ruff check src tests` → clean.
3. `grep -rnE 'agent_db|sync\.|mutating_connect|bump_gen|current_gen|sanitized_projection|conflict_aggregates|refresh_agent_db|connect_readonly|assert_fresh|\.expected_gen' src/local_budget` → **empty** (no dangling references to deleted machinery; `agent_connect` is the only "agent" symbol left).
4. `uv run python -c "import local_budget.cli, local_budget.web.routes, local_budget.agent.tools, local_budget.categorize.llm; from local_budget import db; print(hasattr(db,'mutating_connect'), hasattr(db,'agent_connect'))"` → `False True`.
5. Smoke the in-app agent path is still wired: the SDK tools import and `agent/tools.make_server()` builds (no agent.db access). `uv run python -c "from local_budget.agent import tools; tools.make_server(); print('tools ok')"`.
6. `bash scripts/secret-scan.sh` (if present) → clean.

---

## Acceptance (Phase 1)
- `sync.py` and `agent_db.py` deleted; `db.mutating_connect`/`bump_gen`/`current_gen`/`sanitized_projection`/`conflict_aggregates`/`SANITIZED_PROJECTION_SQL` and the `paths.py` agent.db helpers removed.
- `agent/tools.py` reads `budget.db` via `db.agent_connect()` (`transactions WHERE status='posted'`, accounts JOIN for `acct_last4`, conflicts via `reports.unresolved_conflicts`); no `agent_db`/staleness references.
- `cli.py verify` command removed; no `sync` imports anywhere.
- `test_projection_refresh.py` deleted; the projection/gen/agent.db-rebuild tests removed; `test_security.py` + `test_agent_tools.py` rewritten against one DB; the 4 setup-only test files repointed.
- Grep for the deleted machinery is empty; package imports cleanly; full suite + ruff green.

## Invariants
- **Checkable:** no `agent_db`/`sync`/`mutating_connect`/projection symbols remain in `src/`; the agent reads only `budget.db` via `agent_connect()`.
- **Testable:** the retargeted tools return the same figures the agent.db projection did (posted-only, accounts-joined `acct_last4`, live conflicts); the full suite is green; the in-app agent never opens an agent.db file (none is created).
