#!/bin/bash
set -euo pipefail
umask 077
if [[ $# -ne 2 ]]; then
  printf '%s\n' 'usage: budget-plaid-sync.sh ABSOLUTE_BUDGET_BINARY ABSOLUTE_DATA_DIR' >&2
  exit 64
fi
export LOCAL_BUDGET_DATA_DIR="$2"
exec "$1" plaid sync --all --non-interactive --json
