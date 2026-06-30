#!/bin/bash
# launchd-invoked wrapper for automated intake (design §3.4).
#
# Determines the repo root from its own location, so it needs no rendering.
# Runs `budget intake`: import + free, deterministic rule-based categorization
# only — no AI, no network, no cost (the in-app LLM categorizer was removed; AI
# categorization is now the `/budget-categorize` skill, user-initiated from a
# Claude Code session). Safe to fire on every launchd tick.
#
# Logs go to logs/intake.log (counts only — never file contents or any token).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
mkdir -p logs
exec uv run budget intake >> logs/intake.log 2>&1
