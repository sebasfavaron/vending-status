# vending-status plan

This repo did not previously keep a `PLAN.md`; durable task tracking for this repo lives in
Sebas's global task system (`/home/sebas/work/tasks/`), not here. This file is added now only to
record the T-034 change per that task's own requirement to keep both touched repos' plan docs
current — see `README.md` for the repo's actual day-to-day documentation entry point.

## T-034 — Ballbox machine-data export

- Added `scripts/export_ballbox_import.py`: builds a JSON payload from this repo's existing
  local artifacts (OurVend status/inventory snapshots, machine metadata/slot config, Beetwallet
  operations) and POSTs it to Ballbox's authenticated `POST /api/integrations/machine-data/import`
  endpoint. No new live OurVend/Beetwallet fetch was added — this only repackages what other
  scripts here already fetch.
- Reuses this repo's own merged-slot / product-profile inference
  (`scripts/build_ballbox_publish.py::build_merged_slot_maps`/`enrich_inventory_slots`/
  `build_profile_maps`) instead of reimplementing it, so the two stay in sync.
- `idempotencyKey` is a deterministic sha256 of the canonicalized payload content, so re-running
  the exporter with unchanged local data is a safe no-op on the Ballbox side.
- Beetwallet's raw `monto` (whole ARS pesos) is sent as-is (`amount`, not `amountMinor`) — the
  ARS-to-minor-units conversion is owned entirely by Ballbox's ingestion endpoint.
- Full contract: `/home/sebas/work/tasks/T-034/artifacts/INGESTION-CONTRACT-2026-07-13.md`.
- Full operational detail (secrets, first import, schedule, retries, observability, tests):
  `README.md` -> "Ballbox machine-data export (T-034)".
- Verified end-to-end against a real local Ballbox dev server + local Postgres during this task
  (real HTTP POST, replayed twice, confirmed `replayed: true` and zero duplicate rows on the
  second call) — see T-034 task evidence for the captured run.

## Not done in this task

- No recurring schedule/cron was installed for the exporter (documented as a proposal only).
- No production Ballbox secrets were touched; this task only ran against a local Ballbox dev
  instance.
