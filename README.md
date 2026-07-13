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
- exports machine status/inventory/Beetwallet sales to Ballbox's canonical Postgres store
  (see `## Ballbox machine-data export (T-034)` below)

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

## Ballbox machine-data export (T-034)

Ballbox Postgres is the canonical read-only store for imported machine status, inventory/slots,
and normalized Beetwallet transactions. Ballbox runs on Vercel and cannot read this repo's local
files, so `scripts/export_ballbox_import.py` repackages the already-fetched local artifacts
(`site/status.json`, `site/adidas_inventory_snapshot.json`,
`runtime/vending-web/cache/adidas_inventory_views.json`, `config/machines_metadata.json`,
`config/adidas_slot_products.json`, `data/beetwallet_operations.json`) into one payload and POSTs
it to Ballbox's authenticated ingestion endpoint. This script never fetches OurVend/Beetwallet
itself — it only reformats what other scripts in this repo already fetched.

Full request/response contract:
`/home/sebas/work/tasks/T-034/artifacts/INGESTION-CONTRACT-2026-07-13.md`.

### Secrets

- `BALLBOX_MACHINE_DATA_SHARED_SECRET` — bearer token, must be the exact same value configured
  on the Ballbox deployment (`BALLBOX_MACHINE_DATA_SHARED_SECRET` env var there too). Never
  commit a real value; load it from `.env` (gitignored) or the environment.
- `BALLBOX_BASE_URL` — where Ballbox is running (default `http://localhost:3000` for local use;
  point at the real deployment URL for production runs).

### First import (local)

1. Have a local Ballbox dev server running (see the `ballbox` repo) with
   `BALLBOX_MACHINE_DATA_SHARED_SECRET` set to the same value, and at least one
   `MachineExternalLink` row pointing at a real `machine_id`/`estacion._id` you want to test.
2. `BALLBOX_BASE_URL=http://localhost:3000 BALLBOX_MACHINE_DATA_SHARED_SECRET=... python3 scripts/export_ballbox_import.py`
3. Check the printed response: `importRunId`, `replayed` (should be `false` the first time),
   `status`, and `counts`. Anything unresolved shows up in `warnings`, not silently.
4. Re-run the exact same command — the response should come back with `replayed: true` and
   identical `counts` (nothing new is written); this is the idempotency/replay guarantee.
5. Use `--dry-run` (optionally with `--pretty`) to print the built payload without POSTing it,
   e.g. to eyeball it or diff it before wiring up a real run.

### Schedule (proposed, not wired up yet)

Run alongside the existing `refresh_ballbox_public.sh` / `refresh_beetwallet.sh` pattern — e.g.
add `python3 scripts/export_ballbox_import.py` as one more step in whichever cron/systemd timer
already refreshes those local snapshots, after the OurVend/Beetwallet fetch steps and before (or
instead of) publishing the static portal. Not installed as a real schedule by this task.

### Retries

Safe to re-run: `idempotencyKey` is a deterministic hash of the payload content, so retrying
after a network failure (or running it twice by mistake) is a no-op on the Ballbox side rather
than a duplicate import. A non-zero exit code means the run did not succeed — check the printed
JSON body for `error`/`details`.

### Observability

The script prints the full JSON response including `importRunId`/`counts`/`warnings`. The
durable, queryable history lives on the Ballbox side at `GET /api/admin/machine-data/import-runs`
(admin-session auth) — that is the source of truth for import health, not local logs here.

### Tests

`python3 -m unittest tests/test_export_ballbox_import.py -v` — pure unit tests over the
payload-builder functions using small fake fixtures (no real data, no network, no secrets
required). Covers: transaction normalization (raw ARS `monto`, timestamp fallback), malformed
Beetwallet operations (skipped with a warning, not a crash), merged-slot expansion into one row
per physical slot, standalone-slot handling, missing-metadata machines (still emitted, so Ballbox
can quarantine centrally instead of this script silently filtering them), and idempotency-key
determinism.
