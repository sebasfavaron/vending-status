#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f ./.env ]]; then
  set -a
  source ./.env
  set +a
fi

# Reports the last fully completed calendar month, computed at run time so this
# stays correct every month without touching config/monthly_sales_report.json.
month="$(date -d 'last month' +%Y-%m)"

exec python3 scripts/send_monthly_sales_report.py --month "$month" "$@"
