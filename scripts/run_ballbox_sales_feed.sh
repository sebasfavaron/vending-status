#!/usr/bin/env bash
# Live Beetwallet sales feed for ballbox.app "Ventas en vivo". Run by ballbox-sales-feed.timer
# (pi-config/systemd/user/) every 5 minutes.
#
# - Every run: fetch the last 3 days of Beetwallet operations into a runtime file and send
#   only transactions (no status/inventory snapshot rows in Ballbox).
# - Every 6 hours (or with --full): OurVend status + inventory, all Beetwallet operations
#   (data/beetwallet_operations.json, also read by the monthly report) and the full export.
#
# It does not send the amount-warning Telegram and does not publish the old portal. It sends one
# Telegram when it starts failing and one when it recovers.
# Secret: BALLBOX_MACHINE_DATA_SHARED_SECRET in ~/runtime/secrets/ballbox-machine-data.env.
set -euo pipefail
cd "$(dirname "$0")/.."

SECRET_FILE="${BALLBOX_MACHINE_DATA_ENV:-$HOME/runtime/secrets/ballbox-machine-data.env}"
STATE_DIR="$HOME/runtime/ballbox/state"
RECENT_OPERATIONS="$STATE_DIR/beetwallet_operations_recent.json"
LAST_FULL="$STATE_DIR/sales-feed-last-full"
FULL_EVERY_SECONDS=$((6 * 3600))

mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/sales-feed.lock"
if ! flock -n 9; then
  echo "sales feed already running"
  exit 0
fi

# One Telegram when the feed starts failing and one when it recovers, not one per 5-minute run.
# Without it, a dead feed shows only as the "data is old" banner to whoever opens Ventas en vivo.
DOWN_FLAG="$STATE_DIR/sales-feed-down"
TELEGRAM_NOTIFY="$HOME/.agents/skills/telegram-notify/telegram-notify"
notify() { [[ -x "$TELEGRAM_NOTIFY" ]] && "$TELEGRAM_NOTIFY" send -- "$1" >/dev/null 2>&1 || true; }
on_exit() {
  local code=$?
  if (( code != 0 )) && [[ ! -f "$DOWN_FLAG" ]]; then
    date -Is > "$DOWN_FLAG"
    notify "⚠️ Feed de ventas Ballbox falla (exit $code). Ventas en vivo deja de actualizarse. journalctl --user -u ballbox-sales-feed"
  elif (( code == 0 )) && [[ -f "$DOWN_FLAG" ]]; then
    notify "✅ Feed de ventas Ballbox volvió a andar (caído desde $(cat "$DOWN_FLAG"))."
    rm -f "$DOWN_FLAG"
  fi
}
trap on_exit EXIT
# systemd's timeout sends TERM; turn it into an exit so the EXIT trap still runs.
trap 'exit 143' TERM

if [[ ! -f "$SECRET_FILE" ]]; then
  echo "missing $SECRET_FILE (BALLBOX_MACHINE_DATA_SHARED_SECRET); nothing sent" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$SECRET_FILE"
if [[ -f /home/sebas/runtime/secrets/ourvend.env ]]; then source /home/sebas/runtime/secrets/ourvend.env; fi
set +a
export BALLBOX_BASE_URL="${BALLBOX_BASE_URL:-https://ballbox.app}"

full=0
if [[ "${1:-}" == "--full" ]]; then
  full=1
elif [[ ! -f "$LAST_FULL" ]] || (( $(date +%s) - $(stat -c %Y "$LAST_FULL") >= FULL_EVERY_SECONDS )); then
  full=1
fi

stamp() { date -Is; }
# One line per export: the endpoint's verdict and counts, not the whole response.
summary() { python3 -c 'import json,sys; b=json.load(sys.stdin); print(json.dumps({k: b[k] for k in ("ok", "replayed", "status", "counts", "warnings", "error") if k in b}, separators=(",", ":")))'; }

if (( full )); then
  echo "$(stamp) full export to $BALLBOX_BASE_URL"
  python3 scripts/fetch_status.py || echo "status fetch failed; exporting the last status snapshot" >&2
  rm -f runtime/vending-web/cache/adidas_inventory_views.json
  python3 scripts/build_adidas_snapshot.py || echo "inventory snapshot failed; exporting the last one" >&2
  python3 scripts/fetch_beetwallet_operations.py
  python3 scripts/export_ballbox_import.py | summary
  touch "$LAST_FULL"
else
  since="$(TZ=America/Argentina/Buenos_Aires date -d '2 days ago' +%F)"
  echo "$(stamp) transactions since $since to $BALLBOX_BASE_URL"
  BEET_OPERATIONS_OUT="$RECENT_OPERATIONS" python3 scripts/fetch_beetwallet_operations.py "$since" 2100-01-01 >/dev/null
  python3 scripts/export_ballbox_import.py --transactions-only --operations "$RECENT_OPERATIONS" | summary
fi
