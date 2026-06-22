# vending-status

Simple GitHub Pages dashboard for OurVend machine status.

## Secrets

Set these repo secrets:

- `OURVEND_USERNAME`
- `OURVEND_PASSWORD`

## What it does

- fetches status from OurVend PC + H5 web apps
- builds `status.json`
- deploys a static dashboard to GitHub Pages
- runs on schedule and manually
- can send deduplicated Adidas low-stock alert emails from local JSON snapshots

## Adidas low-stock alerts

Current MVP shape:
- source of truth: local hourly `inventory.json`
- one shared threshold across slots
- one alert when a slot drops to or below threshold
- no repeat alert until the slot recovers above threshold and falls again
- manual `machine + slot -> product` mapping with fallback to `Slot X`
- transactional email via Resend from `communications@ballbox.app`

Files:
- config: `config/stock_alerts.json`
- slot mapping: `config/adidas_slot_products.json`
- script: `scripts/send_low_stock_alerts.py`
- wrapper: `scripts/run_low_stock_alerts.sh`
- dedupe state: `/home/sebas/runtime/ballbox/state/adidas_low_stock_alert_state.json`

Dry run:

```bash
python3 scripts/send_low_stock_alerts.py --dry-run --inventory-path /home/sebas/work/projects/vending-status/site/ballbox/data/inventory.json
```

Live setup still needed:
- Resend account
- verified domain `ballbox.app`
- sender `communications@ballbox.app`
- `RESEND_API_KEY` in the runtime env
- final threshold confirmation before enabling `config/stock_alerts.json`
