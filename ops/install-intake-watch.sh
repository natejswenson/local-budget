#!/bin/bash
# Render + (re)install the launchd LaunchAgent that auto-runs `budget intake`
# when a file is dropped in the inbox. Idempotent — safe to re-run; re-run after
# moving the repo (the rendered plist holds absolute paths).
#
#   ./ops/install-intake-watch.sh              # install / reinstall
#   ./ops/install-intake-watch.sh --uninstall  # remove
#
# Inbox defaults to ~/budget-inbox; override with LOCAL_BUDGET_INBOX_DIR.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.local-budget.intake"
# Rendered plist lives IN the repo (gitignored: ops/*.plist.rendered) and is
# bootstrapped from there — keeps the whole automation self-contained.
PLIST="$REPO/ops/$LABEL.plist.rendered"
INBOX="${LOCAL_BUDGET_INBOX_DIR:-$HOME/budget-inbox}"
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"

uninstall() {
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "  ✓ uninstalled $LABEL"
}

if [[ "${1:-}" == "--uninstall" ]]; then uninstall; exit 0; fi

mkdir -p "$INBOX" "$REPO/logs"
chmod +x "$REPO/ops/budget-intake.sh"

sed -e "s#@REPO@#$REPO#g" -e "s#@INBOX@#$INBOX#g" \
  "$REPO/ops/$LABEL.plist.template" > "$PLIST"

# Reinstall cleanly so a re-run picks up any path change.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "  ✓ installed $LABEL"
echo "    watching: $INBOX  → drop a Wells Fargo export there; it imports automatically"
echo "    logs:     $REPO/logs/intake.log"
echo "    remove:   ./ops/install-intake-watch.sh --uninstall"
