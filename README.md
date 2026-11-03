# vending-status

Ballbox portal + local control plane for OurVend and Beetwallet operations.

> **The Ballbox portal is retired (2026-10-23).** Nobody outside used it. The Pi no longer runs
> the hourly refresh (`scripts/refresh_ballbox_public.sh`), its Telegram Beetwallet warnings or
> the Flask app, and nginx no longer serves `/ballbox/` (pi-config `docs/routines.md`). GitHub
> Pages for this repo has been off since the move to the Pi. `site/` stays as history. The monthly
> Adidas report (`.github/workflows/monthly-sales-report.yml`) is separate and still runs.

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

## Published portal (retired 2026-10-23)

Surface it had:

- `/ballbox/` — operational home
- `/ballbox/reports/` — reports/documents index
- `/ballbox/reports/hardware-signage/` — interactive RPi vs ONN/TV Box cost analysis

Static source lives under `site/ballbox/` and is published by `scripts/refresh_ballbox_public.sh`.

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

Lessee keys (low stock, sales mail, monthly report):
- the lessee (arrendatario) rents the machines from Ballbox and pays the rent, e.g. Adidas
  (ballbox-db `company/glossary.md`)
- `config/stock_alerts.json`, `config/sales_email.json`, `config/monthly_sales_report.json`:
  `lessee` (slug, default `adidas`) and `lessee_label` (shown in subjects and headers,
  default `Adidas`)
- `config/machines_metadata.json`: each row's `lessee` picks the machines that belong to it
- the old `client` / `client_label` keys still work as a fallback (`scripts/lessee.py`); the new
  key wins when both are set
- the published JSON under `site/ballbox/data/` and the `?client=` URLs keep the name `client`

Beetwallet amount anomaly warning:
- runner: `python3 scripts/send_beetwallet_amount_warnings.py`
- first run creates a baseline and ignores existing bad historical rows
- later runs send Telegram only for new anomalous purchases
- state: `/home/sebas/runtime/ballbox/state/beetwallet_non_round_amount_alert_state.json`

## Monthly report in Actions

`.github/workflows/monthly-sales-report.yml` sends the Adidas monthly report on the 1st at
07:00 Buenos Aires (it replaces `ballbox-monthly-sales-report.timer` on the Pi).

| send mode | what happens |
|---|---|
| `render` | fetch Beetwallet, build the report, no email |
| `preview` (default) | render + review copy to `review_recipient_emails` |
| `client` | preview, then the real send to `recipient_emails`, after approval in the `adidas-client` environment |

The cron uses the repo variable `MONTHLY_REPORT_SEND_MODE`; a manual run uses the `send_mode`
input. The client job refuses to run when `adidas-client` has no required reviewer, for a
month before 2026-10 (sent from the Pi), and for a month that already has a
`monthly-report-sent-YYYY-MM` artifact, unless `force` is set.

Secrets: `BEET_USERNAME`, `BEET_PASSWORD`, `RESEND_API_KEY`, and `MONTHLY_REPORT_CONFIG_TGZ_B64`,
a base64 tarball of `config/monthly_sales_report.json`, `config/adidas_slot_products.json` and
`config/machines_metadata.json`. Rebuild it whenever one of those changes:

```bash
tar -czf - config/monthly_sales_report.json config/adidas_slot_products.json config/machines_metadata.json \
  | base64 | gh secret set MONTHLY_REPORT_CONFIG_TGZ_B64 -R sebasfavaron/vending-status
```

Gotchas:

- This repo is public, so run logs and artifacts are too. The workflow prints only status
  lines and never uploads the rendered HTML; keep it that way.
- The report does not need OurVend: without `site/ballbox/data/inventory.json` the product
  names come from `config/adidas_slot_products.json`. The render was byte-identical with and
  without it for 2026-08, 2026-09 and 2026-10.
- GitHub turns off the cron of a public repo after 60 days without commits.
- `scripts/fetch_beetwallet_operations.py` pages 10 operations per request; the full history
  took 2.5 min on 2026-10-23.
- The Telegram notices in `send_monthly_sales_report.py` are skipped on the runner (no
  `telegram-notify` there). A failed run shows up as a GitHub Actions failure email instead.

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

### Schedule

`scripts/run_ballbox_sales_feed.sh [--full | --recent]` runs in GitHub Actions
(`.github/workflows/sales-feed.yml`) and, until Sebas turns it off, in `ballbox-sales-feed.timer`
on the Pi (pi-config `systemd/user/`). Both can run at once: Ballbox skips a sale it already has
(`externalId`), and two feeds that fetched the same sales send the same request, which Ballbox
replays.

- recent, every 5 minutes: Beetwallet operations of the last 3 days into
  `$SALES_FEED_STATE_DIR/beetwallet_operations_recent.json`, then
  `export_ballbox_import.py --transactions-only --operations <that file>`. `machines` is empty,
  so Ballbox writes no status or inventory snapshot. `sourceGeneratedAt` is the latest sale, so
  with no new sale the request is the same and Ballbox replays it;
- full, every 6 hours: `fetch_status.py`, `build_adidas_snapshot.py`, all Beetwallet operations
  into `data/beetwallet_operations.json`, and the full export. With no flag the script picks full
  when its last full run (in `SALES_FEED_STATE_DIR`) is 6 hours old.

| variable | default |
|---|---|
| `BALLBOX_MACHINE_DATA_SHARED_SECRET` | read from `BALLBOX_MACHINE_DATA_ENV` (`~/runtime/secrets/ballbox-machine-data.env`) |
| `BALLBOX_BASE_URL` | `https://ballbox.app` |
| `BEET_USERNAME`, `BEET_PASSWORD` | `.env` in this repo |
| `OURVEND_USERNAME`, `OURVEND_PASSWORD` (full only) | read from `OURVEND_ENV` (`~/runtime/secrets/ourvend.env`) |
| `SALES_FEED_STATE_DIR` | `~/runtime/ballbox/state` |

It prints one line per export: the endpoint's verdict, counts and the number of warnings. The
warnings themselves stay in Ballbox (`GET /api/admin/machine-data/import-runs`), because the
Actions logs of this public repo are readable by anyone. Where `telegram-notify` exists (the Pi)
it sends one Telegram when it starts failing and one when it recovers; in Actions a failed run is
the alert.

Actions gotchas:

- secrets: `BALLBOX_MACHINE_DATA_SHARED_SECRET` (the value set in Vercel), `BEET_USERNAME`,
  `BEET_PASSWORD`, `OURVEND_USERNAME`, `OURVEND_PASSWORD`, and for the full run
  `MONTHLY_REPORT_CONFIG_TGZ_B64` (machine metadata and slot mapping);
- GitHub runs schedules best-effort: a 5-minute cron often lands 10 to 20 minutes late, and
  Ballbox shows "data is old" after 20 minutes without a check;
- two imports that insert the same new sale at the same second can collide on `externalId`; one
  run fails and the next one counts the sale as a duplicate.

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
