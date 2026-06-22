#!/usr/bin/env python3
import json
from copy import deepcopy
from pathlib import Path

from send_low_stock_alerts import (
    DEFAULT_INVENTORY_PATH,
    apply_state_changes,
    build_subject,
    build_text_body,
    collect_low_slots,
    load_json,
)

LIVE_INVENTORY_PATH = Path("/home/sebas/work/projects/vending-status/site/ballbox/data/inventory.json")
THRESHOLD = 2
TARGET_CLIENT = "adidas"


def scenario_result(name, ok, **extra):
    return {"scenario": name, "ok": ok, **extra}


def main():
    inventory = load_json(LIVE_INVENTORY_PATH if LIVE_INVENTORY_PATH.exists() else DEFAULT_INVENTORY_PATH)
    if not inventory:
        raise SystemExit("missing inventory snapshot")

    empty_mapping = {"machines": {}}
    low_rows, current_low_keys, processable_machine_ids = collect_low_slots(inventory, empty_mapping, TARGET_CLIENT, THRESHOLD)
    first_state = apply_state_changes(
        {"version": 1, "alerts": {}},
        current_alerts=low_rows,
        current_low_keys=current_low_keys,
        processable_machine_ids=processable_machine_ids,
        newly_low_keys={row["key"] for row in low_rows},
        sent_at="2026-06-22T22:10:00Z",
    )
    first_new = [row for row in low_rows if not ({"alerts": {}}.get("alerts", {}).get(row["key"], {})).get("active")]
    live_preview = {
        "subject": build_subject("Ballbox", "Adidas", len(first_new)),
        "text": build_text_body(first_new, THRESHOLD, inventory.get("generated_at"), "Adidas", "https://ballbox-first.emperor-ratio.ts.net:8446/ballbox/inventory/?client=adidas"),
    }

    second_new = [row for row in low_rows if not (first_state.get("alerts", {}).get(row["key"]) or {}).get("active")]

    mapped = {"machines": {"2601070191": {"3": "Producto QA Sheraton Slot 3"}}}
    mapped_low_rows, _, _ = collect_low_slots(inventory, mapped, TARGET_CLIENT, THRESHOLD)
    mapped_new = [row for row in mapped_low_rows if row["key"] == "2601070191:3"]
    mapped_preview = {
        "subject": build_subject("Ballbox", "Adidas", len(mapped_new)),
        "text": build_text_body(mapped_new, THRESHOLD, inventory.get("generated_at"), "Adidas", "https://ballbox-first.emperor-ratio.ts.net:8446/ballbox/inventory/?client=adidas"),
    }

    recovered_inventory = deepcopy(inventory)
    for machine in recovered_inventory.get("data", {}).get("machines", []):
        if str(machine.get("machine_id")) == "2601070191":
            for slot in machine.get("slots", []):
                if str(slot.get("slot_no")) == "3":
                    slot["quantity"] = "5"
    recovered_rows, recovered_low_keys, recovered_processable = collect_low_slots(recovered_inventory, empty_mapping, TARGET_CLIENT, THRESHOLD)
    recovered_state = apply_state_changes(
        first_state,
        current_alerts=recovered_rows,
        current_low_keys=recovered_low_keys,
        processable_machine_ids=recovered_processable,
        newly_low_keys=set(),
        sent_at=None,
    )

    dropped_again_inventory = deepcopy(inventory)
    for machine in dropped_again_inventory.get("data", {}).get("machines", []):
        if str(machine.get("machine_id")) == "2601070191":
            for slot in machine.get("slots", []):
                if str(slot.get("slot_no")) == "3":
                    slot["quantity"] = "1"
    dropped_rows, dropped_low_keys, dropped_processable = collect_low_slots(dropped_again_inventory, empty_mapping, TARGET_CLIENT, THRESHOLD)
    dropped_new = [row for row in dropped_rows if not (recovered_state.get("alerts", {}).get(row["key"]) or {}).get("active")]
    dropped_state = apply_state_changes(
        recovered_state,
        current_alerts=dropped_rows,
        current_low_keys=dropped_low_keys,
        processable_machine_ids=dropped_processable,
        newly_low_keys={row["key"] for row in dropped_new},
        sent_at="2026-06-22T22:20:00Z",
    )

    results = {
        "threshold": THRESHOLD,
        "inventory_generated_at": inventory.get("generated_at"),
        "scenarios": [
            scenario_result(
                "live_snapshot_first_run",
                len(first_new) == 1 and first_new[0]["key"] == "2601070191:3" and first_new[0]["quantity"] == 2,
                new_alert_count=len(first_new),
                alert_keys=[row["key"] for row in first_new],
                preview=live_preview,
            ),
            scenario_result(
                "same_snapshot_second_run_dedupes",
                len(second_new) == 0,
                new_alert_count=len(second_new),
            ),
            scenario_result(
                "manual_mapping_changes_label",
                len(mapped_new) == 1 and mapped_new[0]["product_label"] == "Producto QA Sheraton Slot 3",
                preview=mapped_preview,
            ),
            scenario_result(
                "recovery_clears_active_state",
                len(recovered_rows) == 0 and not recovered_state.get("alerts", {}).get("2601070191:3", {}).get("active"),
                recovered_active=recovered_state.get("alerts", {}).get("2601070191:3", {}).get("active"),
                recovered_at=recovered_state.get("alerts", {}).get("2601070191:3", {}).get("last_recovered_at"),
            ),
            scenario_result(
                "drop_again_realerts",
                len(dropped_new) == 1 and dropped_new[0]["quantity"] == 1 and dropped_state.get("alerts", {}).get("2601070191:3", {}).get("active"),
                new_alert_count=len(dropped_new),
                alert_keys=[row["key"] for row in dropped_new],
                preview={
                    "subject": build_subject("Ballbox", "Adidas", len(dropped_new)),
                    "text": build_text_body(dropped_new, THRESHOLD, inventory.get("generated_at"), "Adidas", "https://ballbox-first.emperor-ratio.ts.net:8446/ballbox/inventory/?client=adidas"),
                },
            ),
        ],
    }
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
