# Phase 2 — Standalone stdio MCP server + render contract; drop the SDK & in-app AI — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use crucible:build to implement this plan task-by-task.

**Goal:** Expose the deterministic read tools over a standalone **stdio MCP server** (reachable from a Claude Code session), with every read tool returning a deterministic `rendered` markdown block ("clean & beautiful"); **remove `claude-agent-sdk`** and the entire in-app AI surface (the chat REPL, the LLM categorizer, the dashboard AI routes) so AI lives only in skills.

**Architecture:** Today `agent/tools.py` defines tools with the SDK's `@tool`/`create_sdk_mcp_server`, consumed by the in-app `agent/chat.py` REPL. Phase 2 (a) adds a pure, unit-tested `agent/render.py`; (b) restructures the tools into a **plain registry** (no SDK decorator) whose read handlers return `{data, rendered}`; (c) serves that registry over a raw-`mcp` low-level `Server` in `web/mcp_server.py` (stdio), wired in a committed `.mcp.json`; (d) deletes the in-app AI (`chat.py`, `prompts.py`, `categorize/llm.py`, CLI `ask`/`brief`/`chat`/`categorize`/`recategorize`, the dashboard AI routes) and drops `claude-agent-sdk` from `pyproject`. This is ONE coupled migration — `tools.py`, `chat.py`, and `llm.py` are the only SDK consumers, so they change together; the suite must be green at the phase boundary.

**Atomic:** true — intermediate commits may be red; the phase-final commit MUST be green. **Restructuring-only:** false. **Tests to verify:** the full suite.

**Decision (user, 2026-06-29):** DROP the SDK and reimplement on the raw `mcp` package (the design's Acceptance #4), NOT the fitness "reuse the SDK server" pattern. `mcp` is available (transitively today; becomes a direct dep).

**Out of scope (Phase 3+):** the WRITE tools (`set_merchant_category`/`set_txn_category`/`add_custom_category`/`remove_category`/`set_budget_limit`/`clear_budget_limit`/`set_expected_income`/`split_subscriptions`/`save_note`/`save_brief`) and the `.claude/skills/*` themselves; evals (Phase 4); CI/README/publish (Phase 5). This phase ships the READ surface + server + AI removal.

**Design ref:** `docs/plans/2026-06-29-budget-true-agent-design.md` §3 (tool surface + render contract), §6 Phase 2.

**Reference:** `~/localrepo/local-fitness/src/local_fitness/web/mcp_server.py::run_stdio` for the stdio serve pattern (`mcp.server.stdio.stdio_server` + `server.run(read, write, server.create_initialization_options())`). We use the raw `Server` directly (not the SDK reuse).

**SDK consumers to retire (grep-confirmed — the ONLY ones):** `agent/tools.py` (`@tool`, `create_sdk_mcp_server`), `agent/chat.py` (`ClaudeSDKClient`), `categorize/llm.py` (`claude_agent_sdk` at lines 154, 262).

---

### Task 1: `agent/render.py` — deterministic, unit-tested formatting

**Files:** Create `src/local_budget/agent/render.py`; Test `tests/test_render.py`.

**API (pure functions; `money` reuses the existing `money.dollars`):**
```python
def money(cents: int) -> str          # signed int cents -> "$1,234.56"; negative -> "-$1,234.56"; never float
def table(rows: list[dict], cols: list[tuple[str, str]]) -> str
    # cols = [(key, header), ...]; right-align any column whose every value is a money/number string;
    # totals row when a col is flagged; "—" for None. Returns a GitHub-flavored markdown table.
def bars(items: list[tuple[str, int]], *, width: int = 20) -> str
    # category-share horizontal bars: label, ▇-bar proportional to value/max, value via money(), and %.
```

**Steps (TDD):** write `tests/test_render.py` first with exact-string snapshots — `money(-123456) == "-$1,234.56"`, `money(0) == "$0.00"`, `money(1000000) == "$10,000.00"`; a `table` snapshot (right-aligned amounts, `—` for None, totals row); a `bars` snapshot. Run → fail. Implement `render.py` (reuse `money.dollars` for the dollar formatting; verify its exact output first). Run → pass. Commit `feat(phase2): agent/render.py deterministic money/table/bars`.

---

### Task 2: Restructure `agent/tools.py` into an SDK-free registry returning `{data, rendered}`

- **Atomic:** true — removes the SDK decorator; `make_server`/`allowed_tool_names`/`chat.py` all depend on the old shape (handled in Tasks 3–4).

**Changes:**
1. Drop `from claude_agent_sdk import create_sdk_mcp_server, tool`.
2. Define a plain **tool spec** registry. **ToolSpec contract (ONE shape, used by Tasks 2/3/6):** `ToolSpec` is a `@dataclass` (frozen) with ATTRIBUTE access — `name: str`, `description: str`, `input_schema: dict`, `handler` — NOT a plain dict literal, so Task 3's `s.name`/`s.description`/`s.input_schema` and `test_agent_tools.py`'s `t.handler(args)` resolve as attributes:
   ```python
   from dataclasses import dataclass
   from collections.abc import Awaitable, Callable

   @dataclass(frozen=True)
   class ToolSpec:
       name: str
       description: str
       input_schema: dict          # real JSON-Schema object (see step 4)
       handler: Callable[[dict], Awaitable[dict]]   # async; self-contained
   ```
   `handler` is a SELF-CONTAINED `async def handler(args: dict) -> dict` that owns its OWN connection + validation. `spec.handler` is the single entry point (drop any `(args, conn)` two-arg form / `spec.invoke` alias). Each read tool's handler returns `{"data": {...}, "rendered": "<markdown>"}`; the notes tools return their existing payloads (`{saved:...}` / `{notes:...}` / `{deleted:...}`). The read tools keep being wrapped by `_with_ro_conn` (it opens `db.agent_connect()` and passes `conn` to the inner body, exposing a single-arg `handler(args)` to the registry); `run_sql` keeps its validate-then-open self-contained body; the notes tools keep their filesystem-only bodies. For each read tool, build `rendered` via `render.py` (e.g. `get_month_summary` → a summary table + a `bars()` of `spend_by_category`; `query_transactions` → a `table`; `top_merchants`/`category_breakdown` → `table`/`bars`).
3. The note tools (`save_user_note`/`list_user_notes`/`delete_user_note`) stay (they back a future skill) but are READ/notes-only here; `run_sql` keeps its guard and returns `{data, rendered}` where `rendered` is a compact table of the rows. NOTE: `save_user_note`/`delete_user_note` WRITE to `user_notes.md` (a markdown file, NOT the financial DB) — defensibly in the read-surface server, distinct from the Phase-3 DB write tools; this is not a leak of the write surface.
4. **`input_schema` must be a real JSON-Schema dict** — `{"type": "object", "properties": {...}, "required": [...]}` — NOT the SDK shorthand. Today only `query_transactions` carries a real schema (`_QUERY_SCHEMA`); every other tool uses shorthand where the value is the Python `str`/`int`/`float` CLASS (e.g. `{"month": str}`) or a bare `{}` — neither is JSON-serializable / a valid object schema, so a raw-`mcp` `list_tools()` would fail to serialize over stdio. Convert ALL of them: `get_month_summary`, `get_category_breakdown`, `top_merchants`, `compare_periods`, `recurring_charges`, `find_anomalies`, `run_sql`, `save_user_note`, `list_user_notes`, `delete_user_note`. (`recurring_charges`/`list_user_notes` take no args → `{"type": "object", "properties": {}, "required": []}`.)
5. Export `TOOL_SPECS: list[ToolSpec]` AND a `SPEC_BY_NAME: dict[str, ToolSpec]` name→spec map (Task 3's `_call` consumes `SPEC_BY_NAME`). Replace `make_server()`/`allowed_tool_names()` — these SDK-coupled functions are deleted (the MCP server in Task 3 consumes `TOOL_SPECS`/`SPEC_BY_NAME`).
6. Update `agent/__init__.py` docstring (no more "claude_agent_sdk MCP tools over agent.db").

**Error-return contract (replaces the SDK `_err`/`is_error` content shape):** an error handler returns a plain `{"error": "<msg>"}` dict — NO `content`/`is_error` envelope (that was SDK-only). The error cases are `run_sql` (read-only/forbidden-keyword/invalid-query → `{"error": "..."}` instead of `_err(...)`) and `save_user_note`/`delete_user_note` (missing text / no note at line). The server (Task 3) handles it via the existing fallback: `result.get("rendered")` is `None`, so it `json.dumps` the dict → the error text reaches the caller as `TextContent`. `test_agent_tools.py`'s error cases (Task 6) assert on `result["error"]` (the message), not the dropped `is_error` flag.

**Render contract:** the handler returns BOTH `data` (structured) and `rendered` (markdown). Skills are instructed to print `rendered` verbatim — so the markdown is the deterministic, testable "beautiful" surface.

**Steps:** restructure; `uv run python -c "import local_budget.agent.tools"` clean; defer tool tests to Task 6. Commit `refactor(phase2): tools.py -> SDK-free registry returning {data, rendered}`.

---

### Task 3: `web/mcp_server.py` — raw-`mcp` stdio server + `.mcp.json` + entry point

**Files:** Create `src/local_budget/web/mcp_server.py`; modify `pyproject.toml` (`[project.scripts] budget-mcp = "local_budget.web.mcp_server:main"`); create `.mcp.json` (repo root, committed).

**Server (raw `mcp.server.lowlevel.Server`):**
```python
from mcp import types
from mcp.server.lowlevel import Server
from ..agent import tools as agent_tools

def build_server() -> Server:
    server = Server("budget")
    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return [types.Tool(name=s.name, description=s.description, inputSchema=s.input_schema)
                for s in agent_tools.TOOL_SPECS]
    @server.call_tool()
    async def _call(name: str, arguments: dict) -> list[types.TextContent]:
        spec = agent_tools.SPEC_BY_NAME.get(name)
        if spec is None:
            return [types.TextContent(type="text", text=f"unknown tool: {name}")]
        result = await spec.handler(arguments)   # self-contained; returns {data, rendered} (or {notes:...} etc.)
        text = result.get("rendered") or json.dumps(result.get("data", result), default=str)
        return [types.TextContent(type="text", text=text)]
    return server

async def run_stdio() -> None:
    from mcp.server.stdio import stdio_server
    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

def main() -> None:
    import asyncio; asyncio.run(run_stdio())
```
(`spec.handler(args)` is self-contained and owns its own connection lifecycle — read tools open `db.agent_connect()` via `_with_ro_conn`; `run_sql` validates then opens; notes tools touch only the filesystem. Single ToolSpec shape — see Task 2.)

**`.mcp.json` (committed, RELATIVE invocation — invariant: no absolute/personal path):**
```json
{ "mcpServers": { "budget": { "command": "uv", "args": ["run", "budget-mcp"] } } }
```

**Steps:** build server; `uv run budget-mcp` starts and (manually) `list_tools` returns the read tools; commit `.mcp.json`. Commit `feat(phase2): stdio MCP server (web/mcp_server.py) + .mcp.json + budget-mcp entrypoint`.

---

### Task 4: Delete the in-app AI surface

**Delete:**
- `src/local_budget/agent/chat.py` (the ClaudeSDKClient REPL) and `src/local_budget/agent/prompts.py` (its system prompt / DENIED_BUILTIN_TOOLS).
- `src/local_budget/categorize/llm.py` (the SDK LLM categorizer).
- **Normalize AI surface in `src/local_budget/normalize.py`** (THIS phase removes it — it was NOT dropped in Phase 1; `normalize.py` still imports `cluster_merchants` from `categorize/llm.py` and exposes the AI flow). Delete `propose_unknowns`, `normalize_now`, `_unaliased_merchants`, and the in-function `from .categorize.llm import cluster_merchants` import. KEEP the deterministic `apply_aliases`/`confirm`/`undo_last` (and the helpers `confirm`/`apply_aliases` depend on: `_next_batch_id`, `_reconcile_subscription_budgets`, `_alias_group`, `_record_added_patterns`). `uv run python -c "import local_budget.normalize"` must stay clean once `llm.py` is gone.
- CLI commands in `cli.py`: `ask` (~search `def ask`), `chat` (~search `def chat`), `brief` (~search `def brief`), `categorize`, `recategorize`; and strip the auto-AI-categorize calls from `import` and `intake` so import/intake apply ONLY deterministic `categorize/rules.py` (leftover rows stay `Uncategorized`). Remove now-dead `from .categorize import llm` imports. (Line numbers drift — locate by `def <name>`.)
- **CLI `normalize` command in `cli.py` (`def normalize`, ~181–197) — REWRITE to the deterministic path.** Its body currently calls `r = norm.normalize_now()` and consumes AI-only return keys (`r['applied_txns']`, `r['auto_merged_groups']`, `r['review']` + a `norm.confirm(...)` review loop). `normalize_now` is deleted in this task, so without a rewrite `budget normalize` raises `AttributeError` at runtime (it's a function-local `import`, there's no `test_cli`, so the suite stays green and a builder won't catch it). Swap to `r = norm.apply_aliases()` and rework the echo lines to its REAL return shape: `apply_aliases()` returns `{batch_id, txns_updated, budgets_merged}` — there is NO `review`, `applied_txns`, or `auto_merged_groups`. So: report `r['txns_updated']` (e.g. `✓ normalized {r['txns_updated']} transaction(s)`) and optionally `r['budgets_merged']` (merged sub-budgets), and **DROP the entire `review`/`norm.confirm(...)` interaction loop** (both `r['review']` and the unsure-group confirm prompt no longer exist — `confirm` itself stays in normalize.py but is no longer driven by this command). Also rewrite the docstring (no more "the AI groups the rest and asks about anything it's unsure of") — built-in/cached brand aliases apply deterministically, nothing is AI-clustered.
- **Remove the now-vestigial AI flags/options + AI-advertising help text on `import` and `intake`** (stripping the `llm.categorize_uncategorized` calls leaves them dead): on `import_cmd` (~37–46) drop the `--no-categorize` option (and its `no_categorize` param + the `if not no_categorize and r["inserted"]:` AI block) and rewrite the docstring `"…(auto-categorizes via AI)."` → rule-based only; on `intake` (~118–164) drop the `--no-ai` option (and its `no_ai` param) and the `if not no_ai and click.confirm("  categorize the unknowns with AI now?", …):` block, so the `needs_review` branch just reports the count (e.g. point the user at `budget review` / the dashboard) with no AI offer. After this, `budget import --help` / `budget intake --help` no longer advertise removed AI behavior.
- Dashboard AI routes in `web/routes.py`: DELETE `/api/chat` (~495), `/api/categorize` (~382), `/api/recategorize` (~387) + their now-dead `llm` import. **REWRITE the `/api/normalize` RUN route (~321) to the deterministic path** — it currently calls `normalize.normalize_now(on_progress=_cb)` (deleted in this task), publishes to `_tidy_progress`, and returns the AI-only `{applied_txns, auto_merged_groups, review}` shape. Swap the body to `return normalize.apply_aliases()` (its REAL return is `{batch_id, txns_updated, budgets_merged}` — NO `review`/`applied_txns`/`auto_merged_groups`), drop the `_cb`/`_tidy_lock` progress wiring, and rewrite the docstring (no LLM clustering — deterministic alias apply only). **DELETE the `/api/normalize/status` progress poller (~342)** and the now-orphaned `_tidy_progress`/`_tidy_lock` module-level plumbing (~29–30) it was the only consumer of. **KEEP the deterministic `/api/normalize/confirm` (~349, calls `normalize.confirm`) and `/api/normalize/undo` (~377, calls `normalize.undo_last`)** — neither depends on the removed clustering.
- **Dashboard frontend `web/static/index.html`** (mirrors the route changes — no test clicks these, so a stale call 404s only at runtime):
  - **"Tidy names" button (~352) handler (~1181–1198):** keep the button (deterministic alias apply still works), but (a) DELETE the `/api/normalize/status` poll block (`const poll=setInterval(...)`, the `clearInterval(poll)`, and the live `'tidying… N/M'` counter — there is no status endpoint any more); the button just shows a static `tidying…` while the POST is open. (b) The `/api/normalize` POST (~1193) stays but now returns `{batch_id, txns_updated, budgets_merged}` — there is NO `r.review`, so call `renderNormalizeReview([], r)` (no review cards) and update `renderNormalizeReview` (~1199) to read `r.txns_updated` (not `r.applied_txns`/`r.auto_merged_groups`) for its summary line, dropping the confirm-card rendering driven by `groups`. The `/api/normalize/confirm` and `/api/normalize/undo` handlers (~1220/1224) stay (deterministic) — the undo button keeps working; the per-group confirm cards no longer appear because the run returns no review groups.
  - **Chat box:** remove the chat UI and its `POST /api/chat` call (~609) — the chat panel/log (`#chat-log`, `#ask`, `#q`, `#new-chat`, `#ask-panel`), `sendChat`/`newChat`/`addBubble`, the `chatHistory` state, and the `launchPlan` "AI save-plan" helper (~618) + any "Plan to save" buttons that call it. The conversational agent moves to skills.
  - **AI categorize/recategorize:** `grep -n '/api/categorize\|/api/recategorize' index.html` is currently EMPTY (no such buttons in the dashboard — those routes had no frontend), so nothing to remove here; the Task 7 gate grep confirms it stays empty.

**Orphan cleanup (cosmetic — keeps the tree clean after the deletions above):**
- `web/routes.py`: with `/api/chat` gone, the chat-prompt machinery it was the only caller of is now dead — remove `_flatten_chat` (~77), `_coerce_history`/`_sanitize_turn_text`, the `_CHAT_*` constants + `_CHAT_PREAMBLE`, and the `_ROLE_MARKER`/`_FENCE_TOKEN` regexes. **Also remove the now-unused module imports these were the SOLE users of** (else ruff F401 blocks Task 7's "ruff clean"): `import re`, `import threading` (only `_tidy_lock`/`_tidy_progress`), `import secrets` (only `_flatten_chat`), and `import logging` + the `_log` module global (only `_flatten_chat`). Also correct the module docstring (~1–8) — drop the "The chat route runs the agent strictly over agent.db" sentence.
- `agent/tools.py`: if the SDK-free registry (Task 2) no longer uses the SDK-shaped `_text`/`_err` content wrappers (handlers now return `{data, rendered}` / the new error shape), DELETE the now-unused `_text`/`_err`. (If Task 2 chose to keep `_err` as the error-shape builder, keep only what the registry actually calls.)
- `cli.py::setup`: reword its stale user-facing strings — the `"Your name (used in chat/briefs)"` prompt and the `"• budget ask …"` next-steps echo reference commands removed this phase (`ask`/`chat`/`brief`); point them at the surviving CLI + skills.
- `web/static/index.html`: ensure the `launchPlan` helper AND the `.ai-plan` ("✨ Plan to save") buttons (the `renderInsight` emitter ~538 + the `querySelectorAll('#insights .ai-plan')` wiring ~520) are removed — a stray `.ai-plan` button whose `launchPlan` handler is deleted throws `ReferenceError` on click (lazy closure, so render won't catch it).
- `models.py` (~line 2): the docstring references `chat.py`/`llm.py` ("not a find-all across chat.py / llm.py") — both deleted this phase; reword to reference the surviving owners (e.g. just the MCP/render surface) so it isn't a stale pointer.

**Files touched:** `web/routes.py`, `web/static/index.html`, `cli.py`, `normalize.py`, `models.py`; deletes `agent/chat.py`, `agent/prompts.py`, `categorize/llm.py`.

**Steps:** delete; `grep -rn 'claude_agent_sdk\|agent.chat\|categorize.llm\|import llm' src` → empty; `grep -n '/api/chat\|/api/categorize\|/api/recategorize' src/local_budget/web/static/index.html` → only nothing (all three retired/absent); `uv run python -c "import local_budget.cli, local_budget.web.routes"` clean. Commit `refactor(phase2): remove in-app AI (chat REPL, llm categorizer, CLI/web AI routes + dashboard)`.

---

### Task 5: Drop `claude-agent-sdk`; add `mcp` as a direct dependency

**Files:** `pyproject.toml`.
- Remove `"claude-agent-sdk>=0.1.68"` from `dependencies`; add `"mcp>=1.0"` (it was transitive via the SDK and resolves as a normal PyPI dep — `uv lock` records the exact resolved version, e.g. 1.27.2, in `uv.lock`; no manual exact pin in `pyproject`).
- `uv lock` to update `uv.lock`.

**Steps:** edit; `uv lock`; `uv run python -c "import mcp; import local_budget.web.mcp_server"` clean; `grep -rn 'claude_agent_sdk' src tests` → empty. Commit `chore(phase2): drop claude-agent-sdk; add mcp as a direct dep`.

---

### Task 6: Test migration

**Add:**
- `tests/test_render.py` (Task 1).
- `tests/test_mcp_server.py` — build the server, assert `list_tools()` returns the expected read-tool names, and `call_tool("get_month_summary", {...})` against a seeded `budget.db` returns a TextContent whose text contains the `rendered` markdown (and that no PII/`raw_ofx` appears). Drive the async handlers with `asyncio.run`/`anyio`.

**Rewrite/keep:**
- `tests/test_agent_tools.py` — call the registry handlers directly (`await spec.handler(args)` or a thin helper) instead of the SDK `_call`; assert both `data` figures (as today) AND that `rendered` is present and correct. Keep the seeded-budget.db fixture from Phase 1. **Update the error-case assertions to the new error shape (Task 2):** the `run_sql` forbidden-keyword/invalid-query cases (and any notes error case) now return `{"error": "<msg>"}`, so assert on `result["error"]` (substring match on the message) instead of the dropped SDK `is_error` flag / `content` envelope.
- `tests/test_normalize.py` — the normalize AI flow is gone (Task 4), so DELETE its AI tests: `test_propose_unknowns_high_confident_low_review`, `test_normalize_now_auto_merges_high_only`, `test_normalize_progress_callback_forwarded_to_default_clusterer` (it monkeypatches `local_budget.categorize.llm.cluster_merchants`), `test_normalize_now_single_undo_reverts_builtin_and_llm`, and the `test_propose_survives_*` cases (`test_propose_survives_llm_error`, `test_propose_survives_non_dict_clusterer_and_confirm_caps_canonical` — re-add its canonical-64-char-cap assertion under a `confirm` test if you want to keep that coverage). KEEP every deterministic test: the `apply_aliases`/`confirm`/`undo_last` coverage (`test_apply_*`, `test_undo_*`, `test_confirm_*`, `test_corrected_*`, `test_canonical_scoped_*`, `test_multiword_*`, `test_singleword_*`, `test_user_renamed_*`, `test_fresh_alias_*`, the builtin-resolution tests).

**Delete:**
- `tests/test_categorize_llm.py` (the LLM categorizer is gone).
- Any `test_web.py` tests for the FULLY-retired routes (`/api/chat`, `/api/categorize`, `/api/recategorize`) — the deterministic dashboard route tests stay green.
- **UPDATE (do NOT delete)** `test_web.py::test_normalize_endpoints_and_no_pii` (~669): the `/api/normalize` run route is KEPT (rewritten to `apply_aliases()`), so this test must be rewritten — drop the `cluster_merchants` monkeypatch, re-point the run assertion to the `{txns_updated, budgets_merged}` shape (no `applied_txns`/`review`), and PRESERVE its deterministic two-spelling→one-Subscriptions collapse + `/api/normalize/undo` + no-PII-leak coverage. Wholesale-deleting it would leave the rewritten run route untested.
- Any chat-REPL test (search `test_*chat*`, `agent.chat`).

**Steps:** `uv run pytest -q` → iterate to green; `uv run ruff check src tests` → clean. Commit `test(phase2): render + mcp server tests; drop SDK-era tool/chat/llm tests`.

---

### Task 7: Phase-2 gate verification
1. `uv run pytest -q` → green.
2. `uv run ruff check src tests` → clean.
3. `grep -rn 'claude_agent_sdk\|agent.chat\|categorize.llm\|ANTHROPIC_API_KEY' src tests` → **empty**.
3b. Dashboard has no remaining calls to the retired routes: `grep -n '/api/chat\|/api/categorize\|/api/recategorize' src/local_budget/web/static/index.html` → **empty**, and `grep -n '/api/normalize/status' src/local_budget/web/static/index.html` → **empty** (only the kept `/api/normalize` run + `/api/normalize/confirm` + `/api/normalize/undo` calls remain). Also `grep -n 'launchPlan\|ai-plan' src/local_budget/web/static/index.html` → **empty** (the ✨ "Plan to save" button + its handler are removed — a stray button would `ReferenceError` on click).
4. `grep -n 'claude-agent-sdk' pyproject.toml` → empty; `mcp` present.
5. `uv run python -c "import local_budget.cli, local_budget.web.routes, local_budget.web.mcp_server; from local_budget.web.mcp_server import build_server; s=build_server(); print('server built')"`.
6. `uv run budget-mcp` starts (smoke; Ctrl-C) OR a test exercises `list_tools`/`call_tool`.
6b. Every tool's `input_schema` JSON-serializes: a test asserts `json.dumps([t.inputSchema for t in await _list()])` succeeds (or the stdio `list_tools` round-trip completes) — so a non-serializable Python-class shorthand schema can't slip through.
7. `.mcp.json` exists, committed, and its `args` are relative (no absolute path) — `grep -q '/Users/' .mcp.json && echo BAD || echo ok`.
8. `bash scripts/secret-scan.sh` → clean.

---

## Acceptance (Phase 2)
- `agent/render.py` exists, pure + unit-tested (money float-free, table/bars snapshots).
- `agent/tools.py` is SDK-free; read tools return `{data, rendered}`; a `TOOL_SPECS` registry is the single source.
- `web/mcp_server.py` serves the read tools over stdio; `budget-mcp` entry point; `.mcp.json` committed with a relative invocation.
- `claude-agent-sdk` removed from `pyproject` (+ `uv.lock`); `mcp` a direct dep; **no `claude_agent_sdk` import anywhere**; no `ANTHROPIC_API_KEY` in code.
- In-app AI gone: `agent/chat.py`, `agent/prompts.py`, `categorize/llm.py` deleted; CLI `ask`/`brief`/`chat`/`categorize`/`recategorize` removed; import/intake do rule-based categorization only; dashboard AI routes retired (`/api/chat`/`/api/categorize`/`/api/recategorize` deleted; `/api/normalize` REWRITTEN to deterministic `apply_aliases`; `/api/normalize/status` dropped).
- Dashboard frontend (`web/static/index.html`) carries no calls to retired routes: chat box + `/api/chat` removed; the `/api/normalize/status` poll + `tidying… N/M` counter removed; "Tidy names" still works (deterministic alias apply) and `/api/normalize/confirm`+`/api/normalize/undo` still wired.
- Full suite + ruff green; the deterministic "Tidy names" + confirm/undo dashboard flow still passes; package imports; MCP server builds and lists tools.

## Invariants
- **Checkable:** no `claude_agent_sdk` import in `src/`; `.mcp.json` invocation is relative; every FINANCIAL read tool returns a `rendered` field (the notes tools return `{saved}`/`{notes}`/`{deleted}`; the server's `result.get("rendered") or json.dumps(...)` fallback handles them); `render.money` uses no float.
- **Testable:** `render.py` exact-string snapshots; the MCP server `list_tools`/`call_tool` round-trip returns the `rendered` markdown and never emits a read-denied column (`raw_ofx`/`acct_hash`); the full suite is green; deterministic dashboard route tests stay green.

## Failure modes considered
- **Raw-`mcp` API drift** (the low-level `Server` decorator signatures differ from the SDK): the `"mcp>=1.0"` dep with the `uv.lock`-pinned resolution + the `tests/test_mcp_server.py` round-trip is the guard.
- **`/api/normalize` half-deletion** breaks a deterministic dashboard feature: THIS phase removes the normalize AI clustering (`propose_unknowns`/`normalize_now`/`_unaliased_merchants` in `normalize.py`; it was NOT removed in Phase 1). The deterministic "Tidy names" feature is PRESERVED by REWRITING the `/api/normalize` run route to call `normalize.apply_aliases()` (built-in/cached alias collapse) — NOT by the confirm/undo routes (those are a separate review/undo surface, never the entry point; `normalize_run`→`normalize_now` was the only run trigger). Drop only `/api/normalize/status` (its `normalize_now` progress driver is gone). KEEP `/api/normalize/confirm` (→`normalize.confirm`) and `/api/normalize/undo` (→`normalize.undo_last`).
- **Dashboard frontend calls a retired route** (same class as the Round-2 CLI miss — the suite stays green because no test clicks the dashboard): `index.html` is edited in lockstep (Task 4) — the chat box + `/api/chat` call removed, the `/api/normalize/status` poll + `tidying… N/M` counter removed, and `renderNormalizeReview` re-pointed at `apply_aliases()`'s `{txns_updated, budgets_merged}` shape. Task 7 gate-greps `index.html` for the retired routes to catch a stray call.
- **`rendered` drift** silently uglifies output: render snapshots lock it.
- **A skill needs a write tool that doesn't exist yet:** documented — write tools are Phase 3; Phase-2 skills (Phase 3) start read-only.
