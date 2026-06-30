# Phase 3 — Write tools (the MCP write surface) — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use crucible:build to implement this plan task-by-task.

**Goal:** Add the deterministic MCP **write tools** so skills can *act* — set categories, set budgets, add/remove categories, split subscriptions, save a brief — each executing through `db.agent_connect(write=True)` so the column-level authorizer is genuinely in the write path (no skill can mutate an imported fact or an unlisted table).

**Architecture:** The write helpers (`manual.*`, `budgets.*`, `categories.*`) today open their OWN `db.connect()` (full RW, no authorizer). Phase 3 (a) refactors them to accept an OPTIONAL `conn` (default = open `db.connect()`, preserving every existing CLI/web caller + test); (b) adds write-tool handlers to the `agent/tools.py` registry that open `db.agent_connect(write=True)` and THREAD that conn through the helper — so the authorizer gates every write; (c) adds a file-backed `save_brief` tool (period-validated, path-confined). Confirm-gating is a SKILL responsibility (Phase 4); the authorizer is the safety net. Reconcile stays advisory — no `resolve_conflict` write tool; the read-only `open_conflicts` tool that backs the reconcile skill is DEFERRED to Phase 4 (where its `incoming_payee` redact-on-read is designed alongside the skill, and it must go through `agent_connect`). Phase 3 is purely the WRITE tools + `save_brief`.

**Design ref:** `docs/plans/2026-06-29-budget-true-agent-design.md` §1 (authorizer allowlist + the "thread agent_connect(write=True) through conn-accepting helpers" requirement, design-gate F1), §3 (write tools). Phases 0–2 shipped (`db.agent_connect()`, the de-wire, the stdio MCP server + render + ToolSpec registry).

**Atomic:** false — Task 1 (helper refactor) is preserve-behavior (existing tests stay green); Tasks 2–3 are additive. **Tests to verify:** the full suite + new write-tool tests.

**Out of scope (Phase 4+):** the 8 `.claude/skills/*` SKILL.md files + the shared persona (Phase 4); per-skill evals (Phase 5); CI/README/publish (Phase 6). `open_conflicts` (the read tool backing the advisory reconcile skill) also moves to Phase 4, where its `incoming_payee` redact-on-read is designed alongside the skill — and it must go through `agent_connect`, not `db.connect()`. This phase ships the deterministic write SURFACE the skills will call.

**Authorizer allowlist recap (what a write tool MAY touch — design §1):** UPDATE `transactions.{category, subcategory, category_source}` ONLY; INSERT/UPDATE/DELETE on `{category_rules, budgets, settings}`. Everything else denied (status, all other txn columns, transactions INSERT/DELETE, every unlisted table incl. `merchant_aliases`). `settings` KEY-whitelist + `status` are TOOL-enforced (the authorizer can't see row keys/values) — the write tools simply never write `status` and only write the whitelisted settings keys.

**Write-tool → backing helper → tables touched (all within the allowlist):**
| Tool | Backing helper | Writes |
|---|---|---|
| `set_merchant_category` | `manual.set_merchant_category` | `category_rules` + UPDATE `transactions.category/subcategory` |
| `set_txn_category` | `manual.set_transaction_category` | UPDATE `transactions.category/subcategory` (by txn_id) |
| `add_custom_category` | `categories.add_custom_category` (+`unhide_category`) | `settings.{custom_categories, hidden_categories}` |
| `remove_category` | `manual.remove_category` | `settings` + UPDATE `transactions.category` (merge) |
| `set_budget_limit` | `budgets.set_limit` | `budgets` |
| `clear_budget_limit` | `budgets.clear_limit` | DELETE `budgets` |
| `set_expected_income` | `budgets.set_expected_income` | `settings.expected_monthly_income_cents` |
| `split_subscriptions` | `manual.split_subscriptions` (minus the alias seed) | UPDATE `transactions.subcategory` + `category_rules` |
| `save_brief` | NEW (file) | `data/briefings/<period>.md` (NOT the DB) |

---

### Task 1: Refactor the write helpers to accept an optional `conn` (preserve-behavior)

- **Restructuring-only:** true (signature gains an optional `conn=None`; default behavior unchanged). **Tests to verify:** test_manual_categorize, test_subcategories, test_reports, test_budgets (if present), test_web — all must stay green.

**Pattern for each helper** (so CLI/web callers are unchanged AND a write tool can thread the guarded conn):
```python
def helper(..., conn: sqlite3.Connection | None = None) -> ...:
    if conn is None:
        with db.connect() as c:
            return _impl(c, ...)
    return _impl(conn, ...)   # caller's agent_connect(write=True) CM commits
```
Extract the existing `with db.connect() as conn:` body into `_impl(conn, ...)` (or inline the `conn or open` branch). **Do NOT commit inside `_impl`** — when a `conn` is passed, the caller's context manager owns the commit (`agent_connect(write=True)` commits on exit).

**Helpers to refactor** (read each first; some already accept `conn`):
- `categorize/manual.py`: `set_merchant_category` (~19), `set_transaction_category` (~46), `remove_category` (~60), `split_subscriptions` (~203). **`split_subscriptions`: also DROP the `merchants.seed_builtin_aliases(conn)` call** — it INSERTs `merchant_aliases` (NOT allowlisted → aborts under `agent_connect(write=True)`); rely on read-only `merchants.active_aliases(conn)` (`init_schema` already seeds the builtins). (design-gate S2)
- `budgets.py`: `set_limit` (~26), `clear_limit` (~57), `set_expected_income` (~115). (`active_limits` already takes `conn`.)
- `categories.py`: `add_custom_category` (~120), `unhide_category` (~111). (`mark_hidden`/`remove_custom`/`custom_categories`/`hidden_categories` already take `conn`; `set_setting`/`get_setting` already take `conn`.)
  - **`add_custom_category` must thread `conn=conn` into ALL THREE of its conn-bearing calls:** `unhide_category(name)` (categories.py:135), `custom_categories()` (:137), and `db.set_setting("custom_categories", ...)` (:141). A PARTIAL thread is a self-deadlock: any one of these left un-threaded opens its OWN `db.connect()`, whose write then blocks on the single agent write connection's still-uncommitted write lock → `OperationalError: database is locked` (and if `unhide_category` specifically is left un-threaded, its write also *bypasses the authorizer*). `unhide_category` itself must in turn thread `conn` through to its own `hidden_categories`/`set_setting` calls.

**Steps:** refactor each (optional `conn`); `uv run pytest -q` → all existing tests still green (preserve-behavior for existing callers EXCEPT the deliberate, benign drop of the redundant alias re-seed in `split_subscriptions` — `init_schema` seeds builtins and every test/CLI path inits first, so the suite stays green); commit `refactor(phase3): write helpers accept an optional conn (thread the guarded write connection)`.

---

### Task 2: Add the DB write tools to the registry

**Files:** `src/local_budget/agent/tools.py` (extend `TOOL_SPECS`); Test `tests/test_write_tools.py`.

**Each write tool handler** opens the guarded write connection and threads it:
```python
async def set_merchant_category_tool(args: dict) -> dict:
    n = manual.set_merchant_category(args["merchant_norm"], args["category"],
                                     args.get("subcategory"), conn=...)
    # open inside: with db.agent_connect(write=True) as conn: n = manual.set_merchant_category(..., conn=conn)
    return {"ok": True, "rendered": f"✓ {args['category']} → {n} transaction(s) + a rule"}
```
Concretely each handler is:
```python
@_with_rw_conn                      # NEW decorator, mirrors _with_ro_conn but agent_connect(write=True)
async def fn(args, conn): ...       # calls the helper with conn=conn; returns {"ok": True, "rendered": "..."} or {"error": ...}
```
Add a `_with_rw_conn` decorator (opens `db.agent_connect(write=True)`, passes `conn`, the CM commits). Validate required args; return `{"error": msg}` on bad input (e.g. unknown category for `set_merchant_category` — `manual` already raises; catch and convert). Register all in `TOOL_SPECS` (so the stdio MCP server exposes them) with proper JSON-Schema `input_schema`:
- `set_merchant_category {merchant_norm, category, subcategory?}` (req merchant_norm, category)
- `set_txn_category {txn_id:int, category, subcategory?}` (req txn_id, category)
- `add_custom_category {name}` (req name)
- `remove_category {name, merge_into}` (req both)
- `set_budget_limit {category, amount_cents:int, subcategory?}` (req category, amount_cents)
- `clear_budget_limit {category, subcategory?}` (req category)
- `set_expected_income {cents:int}` (req cents)
- `split_subscriptions {}` (no args)

**Steps:** add the `_with_rw_conn` decorator + the handlers + the specs; `uv run python -c "import local_budget.agent.tools"` clean; tests in Task 4. Commit `feat(phase3): MCP write tools (categories/budgets/subscriptions) via agent_connect(write=True)`.

---

### Task 3: `save_brief` tool (file-backed, period-validated, path-confined)

**Files:** `src/local_budget/agent/tools.py` (the `save_brief` handler + spec); maybe a small `briefs.py` helper.

`save_brief {period:str, markdown:str}` — writes the brief markdown to `paths.briefings_dir() / f"{period}.md"`. **Safety (design S7):** validate `period` against `^[0-9]{4}-[0-9]{2}$|^all$|^last\d+$`; **resolve `briefings_dir()` itself first** (it can sit under a symlinked `data_dir`), then resolve-and-confine the output path under that resolved base (reject anything that escapes it). Returns `{"ok": True, "path": "<relative>"}` or `{"error": "invalid period"}`. This write is OUTSIDE the authorizer (a file, not the DB), so the tool MUST self-guard.

**Steps:** implement with the regex + `Path.resolve()` confinement check (resolving the `briefings_dir()` base too); register the spec; test in Task 4. Commit `feat(phase3): save_brief tool — period-validated, path-confined file write`.

---

### Task 4: Unit tests — exercise the TOOL ENTRY POINTS (design-gate requirement)

**Files:** `tests/test_write_tools.py`.

The authorizer test must call the **actual write tool** (not `agent_connect` in isolation) so a missed conn-threading is caught:
- **Persists:** `set_merchant_category` / `set_txn_category` set the category (read back via `db.connect()`); `set_budget_limit` creates the limit; `add_custom_category` adds it; `remove_category` merges; `split_subscriptions` runs; `set_expected_income` sets the whitelisted setting.
- **Authorizer-in-the-write-path (the load-bearing tests):** a write tool whose helper is (temporarily, in a test) pointed at a denied write MUST raise — concretely, assert that **no write tool can mutate `status` or a non-allowlisted table**: e.g. a crafted call that would touch `merchant_aliases`/`status` aborts; and assert `split_subscriptions` (which previously seed-wrote `merchant_aliases`) now succeeds *because* that write was dropped (proving it runs under the guarded conn).
- **save_brief:** valid period writes under `briefings_dir()`; `period="../../etc"` (or any escaping value) returns `{"error": ...}` and writes nothing outside `briefings_dir()`.
- **MCP exposure:** the new tools appear in `TOOL_SPECS`/`SPEC_BY_NAME` and their `input_schema` JSON-serializes (extend `test_mcp_server.py`).

**Steps:** write the tests; `uv run pytest -q` → green; `uv run ruff check src tests` → clean. Commit `test(phase3): write-tool entry-point tests (authorizer-in-write-path, save_brief confinement)`.

---

### Task 5: Phase-3 gate verification
1. `uv run pytest -q` → green (full suite + new write-tool/save_brief tests).
2. `uv run ruff check src tests` → clean.
3. Write tools registered: `uv run python -c "from local_budget.agent import tools as t; print(sorted(t.SPEC_BY_NAME))"` includes all 9 new tools; every `input_schema` JSON-serializes.
4. Authorizer-in-write-path: the Task-4 tests prove a write tool cannot mutate `status`/an unlisted table, and `split_subscriptions` no longer seed-writes `merchant_aliases`.
5. Existing CLI/web write paths unchanged: `budget set-category`/`set-limit`/the dashboard editors still work (their tests stay green — the helpers' default `conn=None` path is untouched).
6. `bash scripts/secret-scan.sh` → clean.

---

## Acceptance (Phase 3)
- The write helpers accept an optional `conn`; default behavior (CLI/web) unchanged; all prior tests green.
- 9 new tools in the registry: `set_merchant_category`, `set_txn_category`, `add_custom_category`, `remove_category`, `set_budget_limit`, `clear_budget_limit`, `set_expected_income`, `split_subscriptions`, `save_brief`; each DB write tool runs through `db.agent_connect(write=True)`.
- `split_subscriptions` no longer writes `merchant_aliases` (would abort under the authorizer).
- `save_brief` validates `period` + confines the path under `briefings_dir()`.
- Write-tool entry-point tests prove the authorizer is in the write path; full suite + ruff green; MCP server still builds and lists all tools.

## Invariants
- **Checkable:** every DB write tool opens `db.agent_connect(write=True)` (not `db.connect()`); no write tool references `status` or `merchant_aliases`; `save_brief` has a `period` regex + a path-confinement check; new specs have real JSON-Schema.
- **Testable:** each write tool persists its change; a write tool cannot mutate `status`/an unlisted table (entry-point test, not authorizer-in-isolation); `save_brief` rejects an escaping `period`; existing CLI/web write tests stay green; `input_schema`s JSON-serialize.

## Failure modes considered
- **Helper commits twice / not at all** — when `conn` is passed, `_impl` does NOT commit; the `agent_connect(write=True)` CM commits once on exit; the `conn=None` path keeps its own `with db.connect()` commit. Test both paths.
- **`split_subscriptions` aborts** under the authorizer (the `merchant_aliases` seed) — dropped in Task 1; the run-succeeds test is the guard.
- **`save_brief` path traversal** — regex + resolve-and-confine; the escaping-period test is the guard.
- **A write tool silently bypasses the authorizer** (calls the helper with `conn=None` → `db.connect()`) — the entry-point authorizer tests catch it (a denied write would succeed via db.connect()).
- **settings key-whitelist / status** — authorizer can't enforce; the tools simply never write `status` and only the whitelisted settings keys (checkable by inspection + the deny tests).
