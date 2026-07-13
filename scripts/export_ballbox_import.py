#!/usr/bin/env python3
"""Builds and posts the T-034 machine-data import payload to Ballbox.

Ballbox Postgres (Vercel) cannot read local files, so this repackages
already-fetched local artifacts (OurVend status/inventory snapshots,
Beetwallet operations, machine metadata/slot config) into one payload and
POSTs it to Ballbox's authenticated ingestion endpoint. This script never
fetches OurVend/Beetwallet itself.

Contract this must match:
  /home/sebas/work/tasks/T-034/artifacts/INGESTION-CONTRACT-2026-07-13.md

Usage:
  python3 scripts/export_ballbox_import.py [--dry-run] [--pretty]

Env:
  BALLBOX_BASE_URL                     Ballbox base URL (default http://localhost:3000)
  BALLBOX_MACHINE_DATA_SHARED_SECRET   Bearer secret, must match the Ballbox deployment
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Reuse vending-status's own merged-slot / product-profile inference instead of
# reimplementing it — see build_ballbox_publish.py::build_merged_slot_maps and
# ::enrich_inventory_slots for the source of truth this must stay in sync with.
from build_ballbox_publish import (  # noqa: E402
    build_merged_slot_maps,
    build_profile_maps,
    enrich_inventory_slots,
    index_by_machine,
    load_json,
    normalize_slot_no,
)

ENV_PATH = ROOT / ".env"
SITE = ROOT / "site"
STATUS_PATH = SITE / "status.json"
ADIDAS_PATH = SITE / "adidas_inventory_snapshot.json"
MACHINES_META = ROOT / "config" / "machines_metadata.json"
SLOT_MAPPING_PATH = ROOT / "config" / "adidas_slot_products.json"
ADIDAS_VIEWS_CACHE = ROOT / "runtime" / "vending-web" / "cache" / "adidas_inventory_views.json"
OPERATIONS_PATH = ROOT / "data" / "beetwallet_operations.json"

DEFAULT_BALLBOX_BASE_URL = "http://localhost:3000"
IMPORT_PATH = "/api/integrations/machine-data/import"
SOURCE = "vending-status"


def load_env(path: Path):
    env = {}
    if not path.exists():
        return env
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def getenv(env: dict, name: str, default=None):
    local = env.get(name)
    if local not in (None, ""):
        return local
    return os.environ.get(name, default)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(text: str):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_online(status_row):
    network = (status_row or {}).get("network") or {}
    label = str(network.get("label") or "").strip().lower()
    return label == "online" if label else None


def is_door_open(status_row):
    door = (status_row or {}).get("door") or {}
    label = str(door.get("label") or "").strip().lower()
    return label == "open" if label else None


def build_machine_status_input(status_row, fallback_generated_at):
    if status_row is None:
        return None
    freshness = status_row.get("freshness") or {}
    source_generated_at = freshness.get("last_upload_time") or fallback_generated_at
    return {
        "online": is_online(status_row),
        "network": status_row.get("network"),
        "doorOpen": is_door_open(status_row),
        "temperatureRaw": status_row.get("temperature_raw"),
        "sourceGeneratedAt": source_generated_at,
        "raw": status_row,
    }


def to_price_minor(price_number):
    if price_number is None:
        return None
    # `priceMinor` (unlike the transaction `amount` field) is already the
    # minor-unit contract name, so the exporter — not Ballbox — does the ARS
    # whole-pesos -> minor-units conversion here.
    return round(float(price_number) * 100)


def build_inventory_input(
    machine_id,
    view,
    slot_names_by_machine,
    merged_slots_by_machine,
    product_profiles,
    machine_slot_profiles,
    fallback_generated_at,
):
    if view is None:
        return None

    # `enrich_inventory_slots` already collapses each merged group down to one
    # row (keyed by the anchor slot number) and never emits a separate row for
    # non-anchor member slots — that's the right shape for vending-status's
    # own display, but Ballbox also needs one InventorySlot row per *physical*
    # slot number so `rawSlots` reflects real machine hardware. Expand each
    # collapsed group back out to one row per member slot, all sharing the
    # group's resolved product/price/quantity identity, and mark exactly one
    # of them (the anchor) as `isMergedAnchor`.
    slots, _product_summary = enrich_inventory_slots(
        machine_id,
        view.get("slots", []),
        slot_names_by_machine,
        merged_slots_by_machine,
        product_profiles,
        machine_slot_profiles,
    )

    slot_inputs = []
    for slot in slots:
        anchor_slot_no = normalize_slot_no(slot.get("slot_no"))
        member_slots = [
            normalize_slot_no(member) for member in (slot.get("merged_member_slots") or []) if normalize_slot_no(member)
        ] or ([anchor_slot_no] if anchor_slot_no else [])

        price_minor = to_price_minor(slot.get("price_number")) if slot.get("price_defined") else None
        currency = "ARS" if price_minor is not None else None

        for member_slot_no in member_slots:
            is_anchor_row = member_slot_no == anchor_slot_no
            slot_inputs.append(
                {
                    "slotNo": member_slot_no,
                    "mergedAnchorSlotNo": None if is_anchor_row else anchor_slot_no,
                    "isMergedAnchor": is_anchor_row,
                    "productName": slot.get("display_name") or None,
                    "productExternalId": slot.get("product_id") or None,
                    "priceMinor": price_minor,
                    "currency": currency,
                    "quantity": slot.get("quantity_int"),
                    "capacity": slot.get("capacity_int"),
                    "inventoryState": slot.get("inventory_state"),
                    "quantityDefined": bool(slot.get("quantity_defined")),
                    "capacityDefined": bool(slot.get("capacity_defined")),
                    "priceDefined": bool(slot.get("price_defined")),
                    "raw": slot if is_anchor_row else {**slot, "member_of_anchor_slot_no": anchor_slot_no},
                }
            )

    return {"sourceGeneratedAt": fallback_generated_at, "slots": slot_inputs}


def build_machines_input(status, adidas, meta_rows, views_by_slug, slot_mapping, fallback_generated_at):
    status_by_id = index_by_machine(status.get("machines", []))
    product_profiles, machine_slot_profiles = build_profile_maps(slot_mapping)
    slot_names_by_machine = slot_mapping.get("machines", {}) or {}
    merged_slots_by_machine = slot_mapping.get("merged_slots", {}) or {}

    machines_input = []
    for meta in meta_rows:
        machine_id = str(meta["machine_id"])
        station_id = meta.get("station_id")
        status_row = status_by_id.get(machine_id)
        view = views_by_slug.get(str(meta.get("slug")))

        entry = {"externalMachineId": machine_id}
        if station_id:
            entry["externalStationId"] = str(station_id)

        status_input = build_machine_status_input(status_row, fallback_generated_at)
        if status_input is not None:
            entry["status"] = status_input

        inventory_input = build_inventory_input(
            machine_id,
            view,
            slot_names_by_machine,
            merged_slots_by_machine,
            product_profiles,
            machine_slot_profiles,
            fallback_generated_at,
        )
        if inventory_input is not None:
            entry["inventory"] = inventory_input

        machines_input.append(entry)

    return machines_input


def pick_occurred_at(op):
    return op.get("fecha_finalizado") or op.get("fecha")


def build_transactions_input(operations_payload):
    operations = (operations_payload or {}).get("operations") or []
    transactions_input = []
    for op in operations:
        external_id = op.get("_id")
        station_id = (op.get("estacion") or {}).get("_id")
        occurred_at = pick_occurred_at(op)
        amount = op.get("monto")

        if not external_id or not station_id or not occurred_at or amount is None:
            print(
                f"warning: skipping malformed Beetwallet operation (missing required field): "
                f"_id={external_id!r} estacion={station_id!r} fecha={occurred_at!r} monto={amount!r}",
                file=sys.stderr,
            )
            continue

        seleccion = (op.get("venta") or {}).get("seleccion")
        slot_no = normalize_slot_no(seleccion) if seleccion not in (None, "") else None

        transactions_input.append(
            {
                "externalId": str(external_id),
                "externalStationId": str(station_id),
                "slotNo": slot_no or None,
                # Raw ARS pesos, NOT minor units — Ballbox owns the *100 conversion
                # for transactions so that rule lives in exactly one place.
                "amount": amount,
                "status": str(op.get("status") or "unknown"),
                "occurredAt": occurred_at,
                "raw": op,
            }
        )

    return transactions_input


def build_payload():
    status = load_json(STATUS_PATH, {}) or {}
    adidas = load_json(ADIDAS_PATH, {}) or {}
    meta_rows = load_json(MACHINES_META, []) or []
    slot_mapping = load_json(SLOT_MAPPING_PATH, {}) or {}
    views_cache = load_json(ADIDAS_VIEWS_CACHE, {}) or {}
    operations_payload = load_json(OPERATIONS_PATH, {}) or {}

    views_by_slug = {str(row.get("slug")): row for row in views_cache.get("views", []) if row.get("slug")}
    fallback_generated_at = status.get("updated_at") or adidas.get("updated_at") or now_iso()

    machines = build_machines_input(status, adidas, meta_rows, views_by_slug, slot_mapping, fallback_generated_at)
    transactions = build_transactions_input(operations_payload)

    payload_without_key = {
        "source": SOURCE,
        "sourceGeneratedAt": fallback_generated_at,
        "machines": machines,
        "transactions": transactions,
    }
    idempotency_key = sha256_hex(canonical_json(payload_without_key))

    return {"idempotencyKey": idempotency_key, **payload_without_key}


def post_payload(base_url: str, secret: str, payload: dict, timeout: int = 60):
    url = base_url.rstrip("/") + IMPORT_PATH
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {secret}")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body
    except urllib.error.HTTPError as error:
        body_text = error.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError:
            body = {"raw": body_text}
        return error.code, body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Build the payload and print it, do not POST it.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args()

    env = load_env(ENV_PATH)
    base_url = getenv(env, "BALLBOX_BASE_URL", DEFAULT_BALLBOX_BASE_URL)
    secret = getenv(env, "BALLBOX_MACHINE_DATA_SHARED_SECRET")

    payload = build_payload()

    if args.dry_run:
        indent = 2 if args.pretty else None
        print(json.dumps(payload, indent=indent, ensure_ascii=False))
        return

    if not secret:
        print("BALLBOX_MACHINE_DATA_SHARED_SECRET is not set (.env or environment).", file=sys.stderr)
        sys.exit(1)

    status, body = post_payload(base_url, secret, payload)
    print(json.dumps(body, indent=2, ensure_ascii=False))

    if status != 200 or not body.get("ok"):
        print(f"export failed: HTTP {status}", file=sys.stderr)
        sys.exit(1)

    print(
        f"importRunId={body.get('importRunId')} replayed={body.get('replayed')} "
        f"status={body.get('status')} warnings={len(body.get('warnings') or [])}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
