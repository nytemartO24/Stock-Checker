#!/usr/bin/env bash
# What is scheduled, when it last ran, and whether anything is wrong.
#
#   /root/stock-checker/deploy/status.sh
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
LOG="logs/stock-checker.log"

echo "=== stock-checker status ($APP_DIR) ==="

echo
echo "-- scheduled --"
crontab -l 2>/dev/null | grep -F "$APP_DIR" | grep -vE '^\s*#' \
  || echo "   NOTHING SCHEDULED — run deploy/setup.sh"

echo
echo "-- last run --"
if [[ -f "$LOG" ]]; then
  grep -E '^=== .* (START|END) ' "$LOG" | tail -2 || echo "   no START/END markers yet"
  last_pass=$(grep -c 'run complete' "$LOG" 2>/dev/null || echo 0)
  echo "   completed passes in current log: $last_pass"
else
  echo "   $LOG does not exist yet (job may not have fired)"
fi

echo
echo "-- recent failures --"
if [[ -f "$LOG" ]]; then
  fails=$(grep -E 'END .* \(exit [^0]' "$LOG" | tail -5)
  [[ -n "$fails" ]] && echo "$fails" || echo "   none in current log"
else
  echo "   (no log)"
fi

echo
echo "-- state --"
shopt -s nullglob
for f in state/*.json; do
  case "$f" in
    *_reference_prices.json)
      n=$(./.venv/bin/python -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" "$f" 2>/dev/null || echo '?')
      echo "   $(basename "$f"): $n reference price(s)" ;;
    *)
      read -r total instock < <(./.venv/bin/python - "$f" <<'PY' 2>/dev/null || echo "? ?"
import json, sys
d = json.load(open(sys.argv[1]))
print(len(d), sum(1 for v in d.values() if v.get("in_stock")))
PY
)
      echo "   $(basename "$f"): $total tracked, $instock in stock" ;;
  esac
done
[[ -z "$(echo state/*.json)" ]] && echo "   no state files yet"

echo
echo "-- conflict check --"
# The Amazon checker and news-notifier's pilot watch the same ASINs. If BOTH
# are scheduled with --send-discord you get duplicate alerts from diverging
# state — the single most likely way this deployment goes wrong.
pilot=$(crontab -l 2>/dev/null | grep -E 'track_delivery_multi\.py.*--send-discord' | grep -v '^#' || true)
mine=$(crontab -l 2>/dev/null | grep -F "$APP_DIR" | grep -- '--send-discord' | grep -v '^#' || true)
if [[ -n "$pilot" && -n "$mine" ]]; then
  echo "   *** CONFLICT: news-notifier's pilot AND stock-checker are both"
  echo "       sending to Discord, and both track the same ASINs. Remove the"
  echo "       pilot's delivery job — see deploy/README.md 'Going live'."
elif [[ -n "$pilot" ]]; then
  echo "   pilot is live; stock-checker is dry-run. Expected during migration."
elif [[ -n "$mine" ]]; then
  echo "   stock-checker is live; pilot delivery job not scheduled. Good."
else
  echo "   neither is sending to Discord (both dry-run or unscheduled)."
fi
