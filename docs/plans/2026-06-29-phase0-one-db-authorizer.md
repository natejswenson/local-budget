# Phase 0 — One budget.db + column-level `agent_connect` authorizer — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use crucible:build to implement this plan task-by-task.

**Goal:** Add a connection-scoped, column-level SQLite authorizer (`db.agent_connect`) on the single `budget.db`. **Phase 0 is purely ADDITIVE: it adds `db.agent_connect()` + its full unit-test matrix and DELETES NOTHING.** The deterministic core keeps `db.connect()`/`db.mutating_connect()` exactly as-is, `sync.py`/`agent_db.py` stay wired, and the existing suite stays green trivially because no consumer changes. The coupled de-wiring migration (removing the agent.db projection, deleting `sync.py`/`agent_db.py`, rewriting `llm.py`/`agent/tools.py`/the agent.db-dependent tests) is **Phase 1**, NOT this plan — see the Phase 1 scope note below.

**Architecture:** Model the new authorizer on the existing `agent_db._ro_authorizer` (table-level read-allowlist + blanket write-deny), but make it **column-level** and **write-capable** per design §1. The deterministic importer/CLI/dashboard are untouched and keep `db.connect()`/`db.mutating_connect()` (full RW + projection refresh); only the future MCP tool layer (Phase 1) will open `db.agent_connect()`. Adding `agent_connect` next to the existing connection helpers does not touch any current call site, so nothing breaks and the suite needs no edits.

**Why purely additive (the structural constraint):** the de-wiring cannot be split off ahead of its consumers. `categorize/llm.py` imports `sync` at module top and calls `mutating_connect`/`bump_gen`/`refresh` — deleting `sync.py` breaks `llm.py` at import. Removing the projection refresh makes `agent_db.assert_fresh()` raise across ~7 agent.db-dependent tests, so a "full suite green" gate is unreachable while those consumers still exist. De-wiring + projection removal + `agent_db.py` deletion + `llm.py`/`agent/tools.py` rewrites + test rewrites are ONE coupled migration and must land together in Phase 1.

**Tech Stack:** Python 3.12, sqlite3 authorizer API, pytest, uv. Work in worktree `.worktrees/budget-true-agent` on branch `feature/budget-true-agent`.

**Design ref:** `docs/plans/2026-06-29-budget-true-agent-design.md` §1, §6 Phase 0.

---

## Reference: the real `transactions` columns

`txn_id, account_id, fitid, posted_date, amount_cents, status, txn_type, payee, memo, merchant_norm, category, subcategory, category_source, raw_ofx, imported_at, import_run_id` (+ `canonical_merchant` added by `_migrate`). **Writable derived columns (allow UPDATE):** `category, subcategory, category_source`. Everything else is an imported fact (deny). Account numbers are NOT stored (only `accounts.acct_last4` + `accounts.acct_hash`).

## Authorizer rule matrix (the contract)

| sqlite action | callback args | rule |
|---|---|---|
| `SQLITE_SELECT`, `SQLITE_FUNCTION` | — | OK |
| `SQLITE_READ` | (table, column) | DENY if (table,column) ∈ read-denylist `{(transactions,raw_ofx),(accounts,acct_hash),(inbox_files,filename),(import_runs,source_name),(import_runs,error_message)}`; else OK |
| `SQLITE_UPDATE` | (table, column) | if `not write`: DENY. if `write`: OK iff `(table,column)` ∈ `{(transactions,category),(transactions,subcategory),(transactions,category_source)}` OR `table ∈ {category_rules, budgets, settings}`; else DENY |
| `SQLITE_INSERT`, `SQLITE_DELETE` | (table, None) | if `not write`: DENY. if `write`: OK iff `table ∈ {category_rules, budgets, settings}`; else DENY (incl. `transactions`) |
| `SQLITE_TRANSACTION`, `SQLITE_SAVEPOINT` | — | OK iff `write` (commits need it); else DENY |
| everything else (`ATTACH`/`DETACH`/`PRAGMA`/`CREATE_*`/`DROP_*`/`ALTER_*`/…) | — | DENY |

`SQLITE_READ` denylist returns `SQLITE_DENY` (aborts the statement) — so a `SELECT *` touching a denied column aborts; read tools must enumerate columns (design M1).

**Build constraint for the future Phase-1 MCP WRITE tool:** because `SQLITE_READ` fires for columns referenced inside an `UPDATE`'s `WHERE`/`SET` evaluation, the WRITE tool MUST filter its `WHERE` clause on NON-PII columns only (e.g. `txn_id`). A derived-column `UPDATE` whose `WHERE` reads a read-denied column (e.g. `raw_ofx`) will abort with `access to transactions.raw_ofx is prohibited`. This is correct/safe behavior — just a build constraint to design the tool's predicates around.

---

### Task 1: `db.agent_connect()` + column-level authorizer

**This is the entirety of Phase 0.** Purely additive: a new function + new test file. No existing file other than `db.py` is modified, nothing is deleted.

**Files:**
- Modify: `src/local_budget/db.py` (add `agent_connect` + `_agent_authorizer` + the allowlist constants near the other connection helpers, ~line 224). **Also update the module docstring at `db.py:1`** — it currently asserts "the agent layer NEVER opens this file," which `agent_connect()` makes false; reword to "the agent/skill layer opens it ONLY via `agent_connect()` behind the column-level authorizer."
- Test: `tests/test_agent_connect.py` (new)

**Step 1: Write the failing tests** — `tests/test_agent_connect.py`. The tests below depend on a `seeded_db` fixture (its concrete body is given at the end of this Step — add it to `tests/test_agent_connect.py` or `tests/conftest.py`).

```python
import sqlite3
import pytest
from local_budget import db

# --- read side ---
def test_agent_connect_reads_payee_memo(seeded_db):           # decision #4
    with db.agent_connect(seeded_db) as c:
        row = c.execute("SELECT payee, memo, category FROM transactions LIMIT 1").fetchone()
    assert row is not None  # payee/memo readable

def test_agent_connect_denies_raw_ofx_read(seeded_db):
    with db.agent_connect(seeded_db) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("SELECT raw_ofx FROM transactions LIMIT 1").fetchall()

def test_agent_connect_denies_acct_hash_read(seeded_db):
    with db.agent_connect(seeded_db) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("SELECT acct_hash FROM accounts LIMIT 1").fetchall()

def test_select_star_on_transactions_aborts(seeded_db):       # design M1
    with db.agent_connect(seeded_db) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("SELECT * FROM transactions LIMIT 1").fetchall()

# --- write side: read-only connection denies ALL writes ---
def test_readonly_conn_denies_category_update(seeded_db):
    with db.agent_connect(seeded_db) as c:                     # write=False default
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("UPDATE transactions SET category='X' WHERE txn_id=1")

# --- write side: write connection allows derived, denies facts/status/tables ---
def test_write_conn_allows_category_update(seeded_db):
    with db.agent_connect(seeded_db, write=True) as c:
        c.execute("UPDATE transactions SET category='Groceries' WHERE txn_id=1")
    with db.connect(seeded_db) as c:
        assert c.execute("SELECT category FROM transactions WHERE txn_id=1").fetchone()[0] == "Groceries"

@pytest.mark.parametrize("col", ["amount_cents", "posted_date", "payee", "memo", "status", "txn_type", "merchant_norm"])
def test_write_conn_denies_fact_update(seeded_db, col):
    with db.agent_connect(seeded_db, write=True) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(f"UPDATE transactions SET {col}='x' WHERE txn_id=1")

def test_write_conn_denies_transactions_insert_delete(seeded_db):
    with db.agent_connect(seeded_db, write=True) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("DELETE FROM transactions WHERE txn_id=1")

@pytest.mark.parametrize("col", ["subcategory", "category_source"])
def test_write_conn_allows_other_derived_cols(seeded_db, col):   # cover all 3 derived allows
    with db.agent_connect(seeded_db, write=True) as c:
        c.execute(f"UPDATE transactions SET {col}='x' WHERE txn_id=1")

def test_write_conn_allows_category_rules_budgets_settings(seeded_db):  # all 3 write tables
    with db.agent_connect(seeded_db, write=True) as c:
        # real columns: category_rules(pattern,category,subcategory,priority,source); budgets(...); settings(key,value)
        c.execute("INSERT INTO category_rules (pattern, category, source, priority) VALUES ('Z','Y','manual',5)")
        c.execute("INSERT INTO settings (key, value) VALUES ('k','v') ON CONFLICT(key) DO UPDATE SET value='v'")
        # budgets: insert one limit using the real columns (read db.SCHEMA for budgets' column list)

@pytest.mark.parametrize("tbl,col", [("inbox_files","filename"), ("import_runs","source_name"), ("import_runs","error_message")])
def test_read_deny_covers_all_pii_columns(seeded_db, tbl, col):  # cover remaining read-deny rows
    with db.agent_connect(seeded_db) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(f"SELECT {col} FROM {tbl} LIMIT 1").fetchall()

def test_write_conn_denies_unlisted_table(seeded_db):         # default-deny, design S1
    with db.agent_connect(seeded_db, write=True) as c:
        with pytest.raises(sqlite3.DatabaseError):
            # real merchant_aliases columns: pattern, canonical, source
            c.execute("INSERT INTO merchant_aliases (pattern, canonical, source) VALUES ('a','b','manual')")

def test_agent_connect_denies_attach(seeded_db):
    with db.agent_connect(seeded_db) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("ATTACH DATABASE ':memory:' AS x")
```

**`seeded_db` fixture (concrete body — verified against `db.SCHEMA`).** `init_schema`/`connect` both accept an explicit `db_path`. Required NOT-NULL columns: `accounts` has none mandatory beyond defaults (insert `account_id` + the PII cols the tests read); `transactions` requires `account_id, fitid, posted_date, amount_cents, status('posted' default), imported_at`.

```python
import pytest
from pathlib import Path
from local_budget import db

@pytest.fixture
def seeded_db(tmp_path) -> Path:
    path = tmp_path / "budget.db"
    db.init_schema(path)
    with db.connect(path) as c:
        c.execute(
            "INSERT INTO accounts (account_id, acct_last4, acct_hash, own_account) "
            "VALUES (1, '1234', 'deadbeefhash', 1)"
        )
        c.execute(
            "INSERT INTO transactions "
            "(txn_id, account_id, fitid, posted_date, amount_cents, status, "
            " txn_type, payee, memo, merchant_norm, raw_ofx, imported_at) "
            "VALUES (1, 1, 'FIT-1', '2026-06-01', -1299, 'posted', "
            " 'DEBIT', 'WHOLEFDS', 'WHOLE FOODS #123', 'WHOLEFOODS', "
            " '<OFX-RAW-PII>', '2026-06-02T00:00:00')"
        )
    return path
```

**Step 2: Run to verify they fail** — `uv run pytest tests/test_agent_connect.py -q` → FAIL (`agent_connect` undefined).

**Step 3: Implement** in `src/local_budget/db.py`:

```python
# ── agent connection: connection-scoped column-level authorizer (design §1) ──
_AGENT_WRITE_COLS = {("transactions", "category"),
                     ("transactions", "subcategory"),
                     ("transactions", "category_source")}
_AGENT_WRITE_TABLES = {"category_rules", "budgets", "settings"}
_AGENT_READ_DENY = {("transactions", "raw_ofx"), ("accounts", "acct_hash"),
                    ("inbox_files", "filename"), ("import_runs", "source_name"),
                    ("import_runs", "error_message")}


def _agent_authorizer(write: bool):
    def auth(action, arg1, arg2, dbname, trigger):  # noqa: ANN001
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_DENY if (arg1, arg2) in _AGENT_READ_DENY else sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_UPDATE:
            if not write:
                return sqlite3.SQLITE_DENY
            if (arg1, arg2) in _AGENT_WRITE_COLS or arg1 in _AGENT_WRITE_TABLES:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE):
            return sqlite3.SQLITE_OK if (write and arg1 in _AGENT_WRITE_TABLES) else sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            return sqlite3.SQLITE_OK if write else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY
    return auth


@contextmanager
def agent_connect(db_path: Path | None = None, write: bool = False) -> Iterator[sqlite3.Connection]:
    """budget.db opened for the AGENT/skill layer behind the column-level
    authorizer (design §1). write=False → all writes denied (PRAGMA query_only).
    write=True → only {category,subcategory,category_source} on transactions +
    {category_rules,budgets,settings} writable; facts/status/INSERT-DELETE on
    transactions + every unlisted table denied. raw_ofx/acct_hash/inbox_files/
    import_runs PII columns are read-denied. ATTACH/PRAGMA/DDL always denied.
    Set PRAGMAs BEFORE the authorizer (which denies PRAGMA).

    NOTE for the Phase-1 MCP WRITE tool: filter UPDATE WHERE clauses on NON-PII
    columns only (e.g. txn_id). SQLITE_READ fires inside UPDATE, so a derived-column
    UPDATE whose WHERE reads a read-denied column (e.g. raw_ofx) aborts with
    'access to transactions.raw_ofx is prohibited' — correct/safe, but a build
    constraint to design predicates around."""
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    # Read protection relies on PRAGMA query_only + the authorizer rather than a
    # mode=ro handle: write=True needs a writable connection, so we cannot open
    # read-only. This is a deliberate, acceptable defense-in-depth difference from
    # the old connect_readonly (authorizer is the real read-deny enforcement).
    if not write:
        conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_agent_authorizer(write))
    try:
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.set_authorizer(None)
        conn.close()
        if write:
            paths.harden_db_files(path)
```

**Step 4: Run** — `uv run pytest tests/test_agent_connect.py -q` → PASS. Iterate the rule matrix until green (watch for `SQLITE_TRANSACTION` on commit; if a write test errors on commit, that's the missing allow).

**Step 5: Commit**
```bash
git add src/local_budget/db.py tests/test_agent_connect.py
git commit -m "feat(phase0): db.agent_connect() column-level authorizer (design §1)"
```

---

### Task 2: Phase-0 gate verification

Phase 0 deleted nothing, so the gate is simply: the new tests pass, the **full existing suite still passes unchanged**, lint is clean, and the package imports.

**Step 1:** `uv run pytest tests/test_agent_connect.py -q` → all new allow/deny rows green.
**Step 2:** `uv run pytest -q` → FULL existing suite still green (nothing was removed or rewritten, so this should pass with zero changes to any other test).
**Step 3:** `uv run ruff check src tests` → clean.
**Step 4:** `uv run python -c "import local_budget.cli, local_budget.web.routes, local_budget.agent.tools, local_budget.categorize.llm, local_budget.sync, local_budget.agent_db"` → package imports cleanly (everything still wired this phase).
**Step 5:** `bash scripts/secret-scan.sh` (if present) → clean.

---

## Acceptance (Phase 0)
- `db.agent_connect(write=…)` exists with the rule matrix above; `tests/test_agent_connect.py` (incl. its `seeded_db` fixture) covers every allow + deny row.
- **Nothing is deleted or de-wired.** `mutating_connect`/`bump_gen`/`current_gen`/`sanitized_projection`/`conflict_aggregates`, `sync.py`, and `agent_db.py` all remain exactly as-is and stay wired.
- The full existing suite passes UNCHANGED (no test rewritten, none deleted); the new test file passes; ruff clean; package imports cleanly.

## Phase 1 (next phase, NOT this plan) — the coupled de-wiring migration
This is one phase that must land together so the package keeps importing and the suite stays green at the phase boundary — it CANNOT be split, because `categorize/llm.py` imports `sync` at module top and the agent.db-freshness consumers/tests fail the moment the projection refresh is removed.

- **De-wire `mutating_connect` → `connect`** across `ingest/importer.py`, `ingest/intake.py`, `ingest/normalize.py`, `ingest/reconcile.py`, `cli.py`, `categorize/manual.py`, AND **`categorize/llm.py`** (which imports `sync` at module top and calls `mutating_connect`/`bump_gen`/`refresh` — F1). Drop every `bump_gen`/`current_gen`/`refresh_agent_db`/`sync.*` call site.
- **Delete** `sync.py` and the projection helpers in `db.py` (`mutating_connect`, `bump_gen`, `current_gen`, `sanitized_projection`, `conflict_aggregates`, `SANITIZED_PROJECTION_SQL`).
- **Delete** `agent_db.py` and **rewrite** `agent/tools.py` against `db.agent_connect()`.
- **Remove** the `cli.py verify` command (its body + the top-level `sync` import it depends on — S4).
- **Handle EVERY agent.db-dependent test together** (F2/S3) — either delete or rewrite against `db.agent_connect()`:
  - Delete: `tests/test_projection_refresh.py`; the `current_gen`/`bump_gen`/`sanitized_projection` cases in `tests/test_db.py:36-40,54-71`; `test_agentdb_rebuilt_after_import` & `test_quarantined_not_in_agentdb` in `tests/test_import.py`.
  - Delete or rewrite the agent.db-freshness assertions in `tests/test_reports.py`, `tests/test_subcategories.py`, `tests/test_manual_categorize.py`, `tests/test_categorize_llm.py`, `tests/test_agent_tools.py`.
- **Rewrite `tests/test_security.py`** against `db.agent_connect()` on one `budget.db` (column-deny invariants: payee/memo READABLE per decision #4, raw_ofx/acct_hash read-DENIED, writes to facts/status/unlisted tables denied, account numbers still absent at rest) — replacing the old two-file `agent.db`-absence model.

## Out of scope (Phase 0 — deferred to later phases)
- The entire de-wiring migration above (Phase 1).
- Deleting `agent_db.py` + rewriting `agent/tools.py` + `test_agent_tools.py` (Phase 1).
- Read/write MCP tools, render.py, the stdio MCP server, skills, evals (Phases 1–4).
- Removing `claude-agent-sdk` / `categorize/llm.py` / dashboard AI endpoints (Phases 1–2).
