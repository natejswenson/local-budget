#!/bin/bash
# Explicit operator action only, after a successful authorized pilot.
set -euo pipefail
umask 077
if [[ $# -ne 3 || "$1" != /* || "$2" != /* || "$3" != /* ]]; then
  printf '%s\n' 'usage: install-plaid-sync.sh ABSOLUTE_REPO ABSOLUTE_VENV ABSOLUTE_DATA_DIR' >&2
  exit 64
fi
REPO="$1"
VENV="$2"
DATA="$3"
LABEL="com.local-budget.plaid-sync"
PLIST="$DATA/plaid-schedule/$LABEL.plist"
DOMAIN="gui/$(id -u)"
if [[ ! -f "$REPO/ops/budget-plaid-sync.sh" || ! -x "$VENV/bin/budget" ]]; then
  printf '%s\n' 'Repository wrapper and installed venv budget CLI are required.' >&2
  exit 64
fi
python3 - "$REPO" "$VENV" "$DATA" "$PLIST" <<'PY'
from pathlib import Path
import plistlib
import sys

repo, venv, data, plist = map(Path, sys.argv[1:])
logs = plist.parent / 'logs'
for directory in (data, plist.parent, logs):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
with (repo / 'ops/com.local-budget.plaid-sync.plist.template').open('rb') as source:
    payload = plistlib.load(source)
payload['ProgramArguments'] = ['/bin/bash', str(repo / 'ops/budget-plaid-sync.sh'),
                               str(venv / 'bin/budget'), str(data)]
for key, name in (('StandardOutPath', 'sync.log'), ('StandardErrorPath', 'sync.err.log')):
    log = logs / name
    log.touch(mode=0o600, exist_ok=True)
    log.chmod(0o600)
    payload[key] = str(log)
with plist.open('wb') as target:
    plistlib.dump(payload, target)
plist.chmod(0o600)
PY
plutil -lint "$PLIST"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
printf '%s\n' "Installed $LABEL; safe logs: $DATA/plaid-schedule/logs"
