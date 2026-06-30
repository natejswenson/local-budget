# ops/ — automated intake (macOS launchd)

Auto-runs `budget intake` on the **host** whenever a Wells Fargo export lands in
your inbox folder. Raw bank files (full account numbers) stay entirely on the
host — they never enter the container.

## Install

```bash
./ops/install-intake-watch.sh
```

Drop a `.qfx`/`.ofx`/`.csv` export into `~/budget-inbox` and it imports
automatically. Logs: `logs/intake.log`. Remove with
`./ops/install-intake-watch.sh --uninstall`.

Inbox location defaults to `~/budget-inbox`; override with
`LOCAL_BUDGET_INBOX_DIR=/some/path ./ops/install-intake-watch.sh`.

## How it works (so it's never mysterious later)

| File | Role |
|---|---|
| `com.local-budget.intake.plist.template` | LaunchAgent definition with `@REPO@`/`@INBOX@` placeholders. |
| `install-intake-watch.sh` | Renders the template → `*.plist.rendered` (gitignored, host-specific paths) and `launchctl bootstrap`s it. Idempotent. |
| `budget-intake.sh` | The wrapper launchd runs: `cd <repo> && uv run budget intake` (deterministic, no AI). |

**Two triggers, on purpose:**
- `WatchPaths` fires the instant a file lands — the "kicks off on drop" path.
- `StartInterval=900` (15 min) is a safety net: a large/slow-copied file can lose
  the WatchPaths race against intake's 3-second stability gate, so the periodic
  sweep catches anything missed. Intake is idempotent + deterministic (no AI), so
  an empty or already-imported sweep is a cheap no-op (no import, no LLM call).

**No AI on the tick:** `budget intake` imports + applies free, deterministic
rule-based categorization only — no network, no cost — so it's safe under launchd
(no TTY, fires on every tick). The in-app LLM categorizer was removed in the
agent-first redesign; to AI-categorize the leftovers, open a Claude Code session
in the repo and run the `/budget-categorize` skill (it calls the budget MCP tools
with confirm-gating).

**"It runs twice":** moving an imported file into `~/budget-inbox/processed/`
generates a second `WatchPaths` event; that run finds nothing new and exits
without moving anything, so the chain stops. Harmless.

**After moving the repo:** re-run `./ops/install-intake-watch.sh` — the rendered
plist holds absolute paths.
