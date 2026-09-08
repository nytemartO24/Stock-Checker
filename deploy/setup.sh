#!/usr/bin/env bash
# Bootstrap stock-checker on the VPS. Safe to re-run.
#
#   cd /root/stock-checker && ./deploy/setup.sh
#
# Assumes the code is already present (deployed over SSH or cloned). It does
# NOT fetch anything itself — there is no git remote configured for this
# project yet.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
MARKER="# >>> stock-checker (managed by deploy/setup.sh) >>>"
END_MARKER="# <<< stock-checker <<<"

echo "== stock-checker setup in $APP_DIR =="

# --- venv -----------------------------------------------------------------
if [[ ! -x .venv/bin/python ]]; then
  echo "-- creating venv"
  python3 -m venv .venv
fi
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt
echo "-- python deps installed ($(./.venv/bin/python --version))"

# --- browser --------------------------------------------------------------
# Amazon needs a real browser (delivery/seller/price blocks are injected
# client-side, and pinning the delivery location means driving a modal).
# The download is shared across projects via ~/.cache/ms-playwright, so this
# is usually a no-op on a box already running news-notifier.
# playwright and beautifulsoup4 come from requirements.txt above; only the
# browser binary needs a separate fetch.
./.venv/bin/python -m playwright install chromium
echo "-- chromium ready"

# --- secrets --------------------------------------------------------------
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "!! created .env from .env.example"
fi
chmod 600 .env

# Check it rather than trust it. A blank webhook fails silently at send time
# (the notifier logs a warning and moves on), which once let a test run
# report success while delivering nothing.
if ! grep -qE '^DISCORD_WEBHOOK_URL=https://' .env; then
  echo "!! DISCORD_WEBHOOK_URL is not set in .env — alerts CANNOT be delivered."
  echo "!! Fill it in, then re-run this script. Verify with:"
  echo "!!   ./deploy/run.sh scripts/test_discord.py --send"
fi

mkdir -p logs state

# --- logrotate ------------------------------------------------------------
if [[ -w /etc/logrotate.d ]]; then
  sed "s#__APP_DIR__#$APP_DIR#g" deploy/logrotate.conf > /etc/logrotate.d/stock-checker
  echo "-- installed /etc/logrotate.d/stock-checker"
else
  echo "!! cannot write /etc/logrotate.d — install deploy/logrotate.conf by hand"
fi

# --- crontab --------------------------------------------------------------
# MERGE, never replace: news-notifier's jobs live in the same crontab and
# clobbering them would silently stop the live Beyblade tracking.
desired="$(sed "s#__APP_DIR__#$APP_DIR#g" deploy/crontab.txt)"
current="$(crontab -l 2>/dev/null || true)"
# Drop any previous block of ours so re-running doesn't duplicate entries.
cleaned="$(printf '%s\n' "$current" | awk -v s="$MARKER" -v e="$END_MARKER" '
  $0 == s {skip=1; next} $0 == e {skip=0; next} !skip')"
printf '%s\n%s\n%s\n%s\n' "$cleaned" "$MARKER" "$desired" "$END_MARKER" \
  | grep -v '^$' | crontab -
echo "-- crontab updated (existing entries preserved)"

echo
echo "== done =="
echo "Scheduled DRY-RUN. Nothing will be sent to Discord until you add"
echo "--send-discord — see deploy/README.md 'Going live' first, because the"
echo "news-notifier pilot is probably still watching the same ASINs."
echo
"$APP_DIR/deploy/status.sh" || true
