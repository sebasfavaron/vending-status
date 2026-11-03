#!/usr/bin/env bash
# Live Beetwallet sales feed for ballbox.app "Ventas en vivo". Runs in GitHub Actions
# (.github/workflows/sales-feed.yml) and, until Sebas turns it off, in ballbox-sales-feed.timer
# on the Pi (pi-config/systemd/user/), every 5 minutes.
#
# - Recent run: fetch the last 3 days of Beetwallet operations into a runtime file and send
#   only transactions (no status/inventory snapshot rows in Ballbox).
# - Full run, every 6 hours (or with --full): OurVend status + inventory, all Beetwallet
#   operations (data/beetwallet_operations.json, also read by the monthly report) and the full
#   export.
#
# Usage: run_ballbox_sales_feed.sh [--full | --recent]
#   no flag: full when the last full run is 6 hours old (state in SALES_FEED_STATE_DIR), else recent.
#
# Environment (all optional when the Pi's files exist):
#   BALLBOX_MACHINE_DATA_SHARED_SECRET  import secret; when unset, read from BALLBOX_MACHINE_DATA_ENV
#                                       (default ~/runtime/secrets/ballbox-machine-data.env)
#   BALLBOX_BASE_URL                    default https://ballbox.app
#   BEET_USERNAME, BEET_PASSWORD        Beetwallet (or .env in this repo)
#   OURVEND_USERNAME, OURVEND_PASSWORD  full run only; when unset, read from OURVEND_ENV
#                                       (default ~/runtime/secrets/ourvend.env)
#   SALES_FEED_STATE_DIR                default ~/runtime/ballbox/state
#   SALES_FEED_ERR_LOG                  where the helper scripts' stderr goes (default: stderr)
#
# Output is one summary line per export: counts and the number of warnings, never the warnings
# themselves, because the Actions logs of this public repo are readable by anyone. For the same
# reason the workflow sends the helpers' stderr (a failed login prints the vendor's response) to
# SALES_FEED_ERR_LOG, a file that is never uploaded.
#
# It does not send the amount-warning Telegram and does not publish the old portal. Where
# telegram-notify exists (the Pi) it sends one Telegram when it starts failing and one when it
# recovers; in Actions a failed run is the alert.
set -euo pipefail
cd "$(dirname "$0")/.."

SECRET_FILE="${BALLBOX_MACHINE_DATA_ENV:-$HOME/runtime/secrets/ballbox-machine-data.env}"
OURVEND_FILE="${OURVEND_ENV:-$HOME/runtime/secrets/ourvend.env}"
STATE_DIR="${SALES_FEED_STATE_DIR:-$HOME/runtime/ballbox/state}"
ERR_LOG="${SALES_FEED_ERR_LOG:-/dev/stderr}"
RECENT_OPERATIONS="$STATE_DIR/beetwallet_operations_recent.json"
LAST_FULL="$STATE_DIR/sales-feed-last-full"
FULL_EVERY_SECONDS=$((6 * 3600))

# date -Is is GNU only; this format works on the Pi, a runner and a Mac.
stamp() { date +%Y-%m-%dT%H:%M:%S%z; }

mkdir -p "$STATE_DIR"
if command -v flock >/dev/null; then
  exec 9>"$STATE_DIR/sales-feed.lock"
  if ! flock -n 9; then
    echo "sales feed already running"
    exit 0
  fi
fi

# One Telegram when the feed starts failing and one when it recovers, not one per 5-minute run.
# Without it, a dead feed shows only as the "data is old" banner to whoever opens Ventas en vivo.
DOWN_FLAG="$STATE_DIR/sales-feed-down"
TELEGRAM_NOTIFY="$HOME/.agents/skills/telegram-notify/telegram-notify"
notify() { [[ -x "$TELEGRAM_NOTIFY" ]] && "$TELEGRAM_NOTIFY" send -- "$1" >/dev/null 2>&1 || true; }
on_exit() {
  local code=$?
  if (( code != 0 )) && [[ ! -f "$DOWN_FLAG" ]]; then
    stamp > "$DOWN_FLAG"
    notify "⚠️ Feed de ventas Ballbox falla (exit $code). Ventas en vivo deja de actualizarse. journalctl --user -u ballbox-sales-feed"
  elif (( code == 0 )) && [[ -f "$DOWN_FLAG" ]]; then
    notify "✅ Feed de ventas Ballbox volvió a andar (caído desde $(cat "$DOWN_FLAG"))."
    rm -f "$DOWN_FLAG"
  fi
}
trap on_exit EXIT
# systemd's timeout sends TERM; turn it into an exit so the EXIT trap still runs.
trap 'exit 143' TERM

set -a
if [[ -z "${BALLBOX_MACHINE_DATA_SHARED_SECRET:-}" && -f "$SECRET_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$SECRET_FILE"
fi
if [[ -z "${OURVEND_USERNAME:-}" && -f "$OURVEND_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$OURVEND_FILE"
fi
set +a
if [[ -z "${BALLBOX_MACHINE_DATA_SHARED_SECRET:-}" ]]; then
  echo "BALLBOX_MACHINE_DATA_SHARED_SECRET is not set and $SECRET_FILE does not exist; nothing sent" >&2
  exit 1
fi
export BALLBOX_BASE_URL="${BALLBOX_BASE_URL:-https://ballbox.app}"

case "${1:-}" in
  --full) full=1 ;;
  --recent) full=0 ;;
  "")
    full=0
    if [[ ! -f "$LAST_FULL" ]] || (( $(date +%s) - $(date -r "$LAST_FULL" +%s) >= FULL_EVERY_SECONDS )); then
      full=1
    fi
    ;;
  *) echo "usage: $0 [--full | --recent]" >&2; exit 2 ;;
esac

# One line per export: the endpoint's verdict, counts and how many warnings, not the whole response.
summary() { python3 -c 'import json,sys; b=json.load(sys.stdin); s={k: b[k] for k in ("ok", "replayed", "status", "counts", "error") if k in b}; s["warnings"]=len(b.get("warnings") or []); print(json.dumps(s, separators=(",", ":")))'; }

if (( full )); then
  echo "$(stamp) full export to $BALLBOX_BASE_URL"
  python3 scripts/fetch_status.py >/dev/null 2>>"$ERR_LOG" || echo "status fetch failed; exporting the last status snapshot" >&2
  rm -f runtime/vending-web/cache/adidas_inventory_views.json
  python3 scripts/build_adidas_snapshot.py >/dev/null 2>>"$ERR_LOG" || echo "inventory snapshot failed; exporting the last one" >&2
  python3 scripts/fetch_beetwallet_operations.py >/dev/null 2>>"$ERR_LOG" || { echo "Beetwallet fetch failed" >&2; exit 1; }
  python3 scripts/export_ballbox_import.py 2>>"$ERR_LOG" | summary
  touch "$LAST_FULL"
else
  since="$(python3 -c 'from datetime import datetime, timedelta; from zoneinfo import ZoneInfo; print((datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")) - timedelta(days=2)).date())')"
  echo "$(stamp) transactions since $since to $BALLBOX_BASE_URL"
  BEET_OPERATIONS_OUT="$RECENT_OPERATIONS" python3 scripts/fetch_beetwallet_operations.py "$since" 2100-01-01 >/dev/null 2>>"$ERR_LOG" || { echo "Beetwallet fetch failed" >&2; exit 1; }
  python3 scripts/export_ballbox_import.py --transactions-only --operations "$RECENT_OPERATIONS" 2>>"$ERR_LOG" | summary
fi
