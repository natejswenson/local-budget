# ops/ — automated intake (macOS launchd)

Auto-runs `budget intake` on the **host** whenever a bank statement export lands in
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

## Opt-in Plaid polling (macOS host)

Plaid automation is **off by default**. Install only as an explicit operator action
**after an authorized successful pilot**: review mapping and overlap decisions,
activate the intended accounts, and run a successful manual sync first. The
installer itself bootstraps a user LaunchAgent and `RunAtLoad` starts a sync
immediately. Do not run it as part of development or testing.

Use an installed `budget` CLI in a pinned host virtual environment; prepare it
from the reviewed lockfile before scheduling. Supply absolute paths for the
repository, that virtual environment, and the existing ledger data directory:

```bash
./ops/install-plaid-sync.sh "/absolute/path/to/budget" "/absolute/path/to/venv" "/absolute/path/to/data"
```

The wrapper executes only the virtual environment's `bin/budget` with
`plaid sync --all --non-interactive --json`; it sets `LOCAL_BUDGET_DATA_DIR`,
uses `umask 077`, and never loads shell profiles or runs `uv`. The plist contains
literal paths rendered with `plistlib` and checked with `plutil -lint`; there
are no secret environment variables or `KeepAlive`. Credentials remain in the
host Keychain. A locked Keychain fails safely without prompting or opening a
browser. Each eligible Item is attempted independently; held Item/intake locks
skip safely. Exit 0 means completed, safely skipped, or no eligible Items;
nonzero means a credential/configuration/fetch or blocked health failure.
JSON reports local connection handles, counts, outcomes, and safe codes only.

The interval is six hours (`21600` seconds). The user must be logged in and the
Mac awake for scheduled work to run; sleep and logout can delay it. Polling
retrieves data Plaid already has available and does not force banks to refresh.
Read-only health: `budget plaid status --json` (no provider calls). Reconnect,
review, credential recovery, and mapping changes remain manual CLI actions.

The installer creates owner-only directories (0700) and files (0600) at
`DATA/plaid-schedule/`, including `logs/sync.log` and `logs/sync.err.log`.
It preserves existing log content. Re-run the explicit installer after moving
the repository, virtual environment, or ledger. To stop and remove the schedule:

```bash
launchctl bootout "gui/$(id -u)/com.local-budget.plaid-sync"
rm "/absolute/path/to/data/plaid-schedule/com.local-budget.plaid-sync.plist"
```

Keep or delete the two log files separately as desired. Removing the schedule
leaves the ledger, mappings, and Keychain credentials intact.
