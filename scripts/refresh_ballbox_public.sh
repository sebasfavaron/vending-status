#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p /home/sebas/runtime/ballbox/logs /home/sebas/runtime/ballbox/state /home/sebas/runtime/ballbox/tmp /home/sebas/runtime/ballbox/snapshots/source /home/sebas/runtime/ballbox/snapshots/derived /home/sebas/runtime/ballbox/snapshots/publish
exec 9>/home/sebas/runtime/ballbox/state/public-refresh.lock
if ! flock -n 9; then
  echo "refresh already running"
  exit 0
fi

if [[ -f /home/sebas/runtime/secrets/ourvend.env ]]; then
  set -a
  source /home/sebas/runtime/secrets/ourvend.env
  set +a
fi

if [[ -f ./.env ]]; then
  set -a
  source ./.env
  set +a
fi

if ! python3 scripts/fetch_status.py; then
  echo "status refresh failed; keeping last status snapshot" >&2
fi
rm -f runtime/vending-web/cache/adidas_inventory_views.json
if ! python3 scripts/build_adidas_snapshot.py; then
  echo "adidas inventory snapshot failed; keeping last inventory snapshot" >&2
fi
if [[ -n "${BEET_USERNAME:-}" && -n "${BEET_PASSWORD:-}" ]]; then
  python3 scripts/fetch_beetwallet_operations.py
  if ! python3 scripts/send_beetwallet_amount_warnings.py; then
    echo "beetwallet amount warnings failed; keeping refresh successful" >&2
  fi
  python3 scripts/build_beetwallet_summary.py
else
  echo "skip beetwallet refresh: missing credentials; keeping last summary" >&2
fi
python3 scripts/build_ballbox_publish.py
if ! scripts/run_low_stock_alerts.sh; then
  echo "low-stock alerts failed; keeping refresh successful" >&2
fi

stage="$(mktemp -d /tmp/ballbox-public.XXXXXX)"
trap 'rm -rf "$stage"' EXIT
cp -r site/ballbox/. "$stage/"
cp -r site/ourvend "$stage/"
cp -r site/machine "$stage/"
rm -rf /var/www/public-funnel-ballbox/*
cp -r "$stage"/. /var/www/public-funnel-ballbox/
chmod -R a+rX /var/www/public-funnel-ballbox

echo "refreshed ballbox public funnel"
