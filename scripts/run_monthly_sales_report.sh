#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p /home/sebas/runtime/ballbox/state
exec 9>/home/sebas/runtime/ballbox/state/monthly-sales-report.lock
if ! flock -n 9; then
  echo "monthly sales report already running"
  exit 0
fi

if [[ -f ./.env ]]; then
  set -a
  source ./.env
  set +a
fi

# Reports the last fully completed calendar month, computed at run time so this
# stays correct every month without touching config/monthly_sales_report.json.
month="$(date -d 'last month' +%Y-%m)"

# No args = review mode: emails review_recipient_emails only, never the real client.
# This is what the monthly timer calls. Real send to the client needs a human to
# run this again with --confirm-send after reviewing (see the Telegram notification).
exec python3 scripts/send_monthly_sales_report.py --month "$month" "$@"
