#!/bin/bash
# Pre-commit guard (design §3.5 / I4): refuse to commit personal financial data
# or host-specific secrets. A filename denylist over the staged tree — dead
# simple, catches the realistic accident (committing your own data/ or .env).
#
# Install as a git pre-commit hook:
#   ln -sf ../../scripts/secret-scan.sh .git/hooks/pre-commit
# or run manually:
#   ./scripts/secret-scan.sh
set -euo pipefail

DENY='(^|/)data/|\.db$|(^|/)\.env$|\.ofx$|\.qfx$|\.qbo$|(^|/)briefings/|(^|/)backups/|\.plist\.rendered$|(^|/)local_key$'

staged="$(git diff --cached --name-only)"
hits="$(printf '%s\n' "$staged" | grep -iE "$DENY" || true)"

if [[ -n "$hits" ]]; then
  echo "✋ secret-scan: refusing to commit personal/financial files:" >&2
  printf '   %s\n' $hits >&2
  echo "   These must stay local (see .gitignore). Unstage them and retry." >&2
  exit 1
fi
exit 0
