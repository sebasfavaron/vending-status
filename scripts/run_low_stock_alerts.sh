#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f ./.env ]]; then
  set -a
  source ./.env
  set +a
fi

exec python3 scripts/send_low_stock_alerts.py "$@"
