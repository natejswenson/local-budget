# Skill evals

The 8 budget skills carry **behavioral evals**: do they call the right tools, never
invent a number, print the tool's `rendered` block, confirm before writing, and
leak no PII? Two tiers.

## Deterministic tier (CI, no spend)

`uv run python scripts/eval.py --mock` replays the committed transcript corpus
(`tests/evals/transcripts/<skill>__<scenario>.jsonl`) through the pure assertion
harness and scores each scenario's family checks. `uv run pytest tests/evals/`
unit-tests the harness functions and the runner's caps. **Zero model calls** — this
runs on a fresh clone and in CI.

The 6 assertion families (`tests/evals/harness.py`):
- **invention_rate** — fraction of `$` figures in the answer not traceable to a tool
  result (advisory; rounding + derived-sum tolerant).
- **confirm_gate** — un-granted write scenario emits no write tool + asks to confirm;
  granted scenario performs the write.
- **tool_call** — the expected tools were called.
- **structure** — briefs carry the prescribed sections.
- **no_pii** — no contiguous ≥7-digit account-number run, no `raw_ofx`/`acct_hash`.
- **fingerprint / parity** — structural signature (tools, figure count, did_write…),
  compared to `baseline.json`. No `$` amounts or merchant strings in the fingerprint.

## Live tier (opt-in, spends)

`uv run python scripts/eval.py [<skill>] --live [--capture] [--max-runs 30] [--max-cost 25]`
drives each scenario through headless Claude Code:

```
claude -p "<prompt>" --output-format stream-json --verbose \
  --mcp-config .mcp.json --strict-mcp-config \
  --allowedTools "mcp__budget__*" "ToolSearch" \
  --disallowedTools Read Write Edit Bash Glob Grep WebFetch WebSearch Task NotebookEdit \
  --max-turns 12
```

`--disallowedTools` is a PRIVACY isolation: it blocks the filesystem/web builtins
so a live run can never read the operator's real files (memory, `~/.claude`, other
repos) and bleed real data into a transcript. The skills need only the budget MCP
tools + `ToolSearch` + skill loading.

against a **seeded, fabricated** eval DB (`scripts/eval_seed.py`, pointed at via an
absolute `LOCAL_BUDGET_DATA_DIR`) — **never** your real `data/budget.db`. The stream
is parsed (assistant `tool_use` / user `tool_result` / `result.result`), `ToolSearch`
is stripped, and each scenario is scored.

**Cost:** runs on the Claude subscription (`claude -p`, no API key). The `total_cost_usd`
the result envelope reports is API-equivalent metered usage against your subscription
quota, **not a separate bill** (it would only bill if `ANTHROPIC_API_KEY` were set,
which this project never does). A full 16-scenario run ≈ ~$10 / ~2–3M tokens.
**Two hard caps, both enforced before each spawn:** `--max-runs` and `--max-cost`
(sums `total_cost_usd`, aborts before exceeding). Mock is the default; `--live` is
required to spend.

The committed `tests/evals/transcripts/` mock corpus is **synthesized fabricated
data** (`scripts/eval_gen_corpus.py`) — never a raw `claude -p` session dump, so it
carries zero PII. The live `--capture` writes fingerprints to `baseline.json` and
raw runs to gitignored `tests/evals/.runs/` for inspection; those raw runs are NOT
committed.

If a skill **fails** a family check, fix its `SKILL.md` (or the scenario/seed if the
eval pre-satisfied the behavior) and re-run that skill: `... <skill> --live`.
