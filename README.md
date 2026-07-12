# local-budget

[![CI](https://github.com/natejswenson/local-budget/actions/workflows/ci.yml/badge.svg)](https://github.com/natejswenson/local-budget/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A **local-first, agent-first** personal spending agent for your bank/financial
statements. Your data stays in one
local `data/budget.db` on your own machine — full account numbers and every
transaction. You don't click through an app to understand your money; you *talk*
to it from a Claude Code session pointed at this repo, through a small MCP server
and a set of no-code skills. The server runs **no inference** — it just exposes
deterministic, column-guarded tools; the reasoning happens in your Claude Code
session under your own subscription auth.

## How it works

1. **Import.** Drop a bank statement export (`.qfx`/`.ofx`/`.csv`) in your inbox and
   run `budget intake` (or `budget import <file>`). Account numbers are masked at
   import time; the raw transaction is stored once in `data/budget.db`.
2. **The MCP server.** The committed `.mcp.json` wires up `uv run budget-mcp` — a
   standalone stdio MCP server that exposes **32 deterministic tools** (18 read,
   14 write) over `budget.db`. Every tool runs behind a connection-scoped,
   column-level SQLite authorizer (`db.agent_connect`): imported facts are
   immutable, and account numbers, raw OFX, and raw payee/memo are read-denied —
   the sanitized `merchant_norm` is the agent's only merchant text. Read tools
   return a `{data, rendered}` pair so the agent can
   print an exact, deterministic markdown block instead of paraphrasing numbers.
3. **The skills.** Eight no-code `budget-*` skills (under `.claude/skills/`)
   orchestrate those tools in your session — grounded in a shared
   `budget-analyst` persona that enforces "never invent a number, print the
   tool's `rendered` block verbatim, confirm before any write."
4. **The dashboard (optional).** `budget serve` starts a loopback-only web
   dashboard at `http://127.0.0.1:8770` — a deterministic visual glance at your
   spending. It runs no Claude inference.

Open a Claude Code session in this repo and the MCP tools (`.mcp.json`) and the
budget skills (`.claude/skills/`) load automatically. Then just ask:
*"How much did I spend on groceries this month?"*, *"Categorize my unreviewed
merchants,"* *"Give me a monthly brief."*

## Privacy

- **One local DB.** Everything lives in `data/budget.db`, which is **gitignored**
  and never committed.
- **Account numbers masked at import** and read-denied to the agent; **raw
  payee/memo read-denied** by the authorizer — the agent sees only the
  sanitized `merchant_norm`.
- **The agent can never alter an imported fact.** The write authorizer permits
  only the derived category columns and the app-config tables — not the imported
  transaction rows. No tool can rewrite history.
- The dashboard is loopback-only by default; binding a non-loopback host requires
  a 32+ char `LOCAL_BUDGET_API_TOKEN` (see `.env.example`).

## Quick start

```bash
uv sync

# import a bank statement export
uv run budget import ~/Downloads/statement.qfx
# …or drop exports in your inbox and run:
uv run budget intake

# then open a Claude Code session in this repo and ask your money questions —
# the budget skills + MCP tools auto-load from .mcp.json and .claude/skills/.
```

For the optional visual dashboard:

```bash
uv run budget serve --open   # http://127.0.0.1:8770 (loopback-only)
```

Run `uv run budget --help` for the full CLI (import, intake, report, reconcile,
recurring, limits, subscriptions, backup, …).

## Skills

| Skill | What you ask it |
|---|---|
| `budget-setup` | first-run setup — expected income, an overview of where you stand |
| `budget-coach` | spending questions — categories, top merchants, "how am I doing" |
| `budget-monthly-brief` | a full month wrap-up — summary, trends, anomalies, recurring |
| `budget-categorize` | pin merchants to categories, clear the review queue |
| `budget-budgets` | set and check monthly category/subcategory limits |
| `budget-income` | income by source and the underlying transactions |
| `budget-subscriptions` | detected recurring charges, split into their own subcategories |
| `budget-reconcile` | review and resolve import conflicts |

All eight reference the shared `budget-analyst` persona; visual reports follow
the shared `budget-visualizer` discipline.

## Evals

Skills are tested like code. `scripts/eval.py` runs a **deterministic mock tier**
(replays committed transcripts, no spend) in CI, plus an **opt-in live tier**
(`--live`, drives `claude -p`) that is cost-capped for when you want to verify
real model behavior.

## What's committed vs. local

| Committed (the app — runs anywhere) | Local only (gitignored — your data/host) |
|---|---|
| `src/`, `tests/`, `scripts/`, `pyproject.toml`, `uv.lock` | `data/` (`budget.db`, `local_key`, `-wal`/`-shm`) |
| `.mcp.json`, `.claude/skills/budget-*`, `docs/` | `.env` (real tokens, host paths) |
| `LICENSE`, `README.md`, fabricated test fixtures | `briefings/`, `backups/`, raw bank exports |

Install the commit guard so personal data can't slip into git:

```bash
ln -sf ../../scripts/secret-scan.sh .git/hooks/pre-commit
```

## Requirements

Python 3.12. MIT licensed (see `LICENSE`).
