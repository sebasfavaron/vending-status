# vending-status

Ballbox portal + local control plane for OurVend and Beetwallet operations.

## What it does

- fetches status from OurVend PC + H5 web apps
- builds Ballbox portal JSON under `site/ballbox/data/`
- refreshes Beetwallet sales snapshots
- sends low-stock email alerts for Adidas
- sends Telegram warnings for new Beetwallet purchase amounts that are not multiples of `1000`
- serves the local Flask control plane in `app.py`
- publishes the static Ballbox portal from the local server

## Local-only artifacts

These stay out of git:

- `*.json`
- `.env`
- `runtime/`
- `TASKS.md`

The repo alone is not enough to run the full stack; local JSON configs, snapshots, and runtime state are required.

## Internal web app

Run it:

- `python app.py`

Optional auth:

- `VENDING_WEB_PASSWORD=algo`
- `VENDING_WEB_SECRET_KEY=algo-largo`

Vendor-backed routes need:

- `OURVEND_USERNAME`
- `OURVEND_PASSWORD`

By default the app also auto-loads local creds from:

- `/home/sebas/runtime/secrets/ourvend.env`

Local data lands in:

- `runtime/vending-web/app.db`

Machine feed preview:

- `/api/machines/<machine_id>/feed`

## Alerts

Low stock:
- runner: `python3 scripts/send_low_stock_alerts.py`
- config: local JSON

Sales mail prototype:
- runner: `python3 scripts/send_sales_report_email.py --dry-run`
- config: local JSON
- preview HTML: `/home/sebas/runtime/ballbox/tmp/sales_report_email_preview.html`

Beetwallet amount anomaly warning:
- runner: `python3 scripts/send_beetwallet_amount_warnings.py`
- first run creates a baseline and ignores existing bad historical rows
- later runs send Telegram only for new anomalous purchases
- state: `/home/sebas/runtime/ballbox/state/beetwallet_non_round_amount_alert_state.json`

## Action probes

- screenshot probe:
  `python scripts/probe_actions.py screenshot --machine-id 2601070188`
- product/catalog surface probe:
  `python scripts/probe_actions.py commodity`
