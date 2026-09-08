#!/usr/bin/env bash
# Cron entry point: run one check pass and exit.
#
# Usage: run.sh <script> [args...]
#   run.sh main.py
#   run.sh main.py --send-discord
#   run.sh main.py --site amazon --send-discord
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# .env holds DISCORD_WEBHOOK_URL and friends. Sourcing it here is why cron
# jobs work without a login shell; running a script BY HAND does not load
# it, so do `set -a; source .env; set +a` first if you invoke main.py
# directly.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

script="$1"
shift

# Don't let a slow run pile up behind itself. The Amazon checker drives a
# real browser across four marketplaces, so a bad network day can push a
# run past its next scheduled tick; without this you'd get two Chromium
# instances competing and — worse — two processes writing the same state
# file.
#
# The key includes the ARGS, not just the script, so `--site amazon` and
# `--site popsplanet` can overlap freely: they touch different state.
lock_key="$(printf '%s' "$script $*" | tr -c 'A-Za-z0-9' '-')"
lock_file="/tmp/stock-checker-${lock_key}.lock"
exec 9>"$lock_file"
if ! flock -n 9; then
  echo "=== $(date -Is) SKIP $script $* (previous run still in progress) ==="
  exit 0
fi

# Bracket every invocation so the log shows a run happened even when the
# script prints nothing — otherwise a quiet log is indistinguishable from
# cron never firing at all.
echo "=== $(date -Is) START $script $* ==="
set +e
"$REPO_ROOT/.venv/bin/python" "$script" "$@"
status=$?
set -e
echo "=== $(date -Is) END $script $* (exit $status) ==="
exit "$status"
