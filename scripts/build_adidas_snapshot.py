#!/usr/bin/env python3
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as vending_app
OUT = ROOT / "site" / "adidas_inventory_snapshot.json"
ADIDAS_SUMMARY = ROOT / "data" / "beetwallet_summary.json"


def load_sales_by_station():
    if not ADIDAS_SUMMARY.exists():
        return {}
    data = json.loads(ADIDAS_SUMMARY.read_text())
    by_station = {}
    for row in data.get("by_station", []):
        by_station[row.get("station_name")] = {
            "station_id": row.get("station_id"),
            "ventas": row.get("ventas", 0),
            "monto_total": row.get("monto_total", 0),
        }
    return by_station


def main():
    sales_by_station = load_sales_by_station()
    views, error = vending_app.adidas_inventory_views()
    machines = []
    totals = {
        "machine_count": 0,
        "matched_machine_count": 0,
        "inventory_machine_count": 0,
        "total_slots": 0,
        "total_quantity": 0,
        "empty_slots": 0,
        "low_slots": 0,
        "ventas": 0,
        "monto_total": 0,
    }
    for item in views:
        machine = item.get("machine") or {}
        machine_id = item.get("machine_id") or machine.get("machine_id")
        if not machine_id:
            continue
        sales = sales_by_station.get(item.get("label"), {})
        summary = item.get("summary") or {}
        row = {
            "machine_id": str(machine_id),
            "client": "Adidas Padel",
            "label": item.get("label"),
            "site": item.get("site"),
            "inventory_status": item.get("inventory_status"),
            "status_note": item.get("status_note"),
            "fetch_error": item.get("fetch_error"),
            "summary": summary,
            "sales": sales,
        }
        machines.append(row)
        totals["machine_count"] += 1
        totals["matched_machine_count"] += 1
        if summary:
            totals["inventory_machine_count"] += 1
            totals["total_slots"] += summary.get("total_slots", 0)
            totals["total_quantity"] += summary.get("total_quantity", 0)
            totals["empty_slots"] += summary.get("empty_slots", 0)
            totals["low_slots"] += summary.get("low_slots", 0)
        totals["ventas"] += sales.get("ventas", 0) or 0
        totals["monto_total"] += sales.get("monto_total", 0) or 0

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "client": "Adidas Padel",
        "machines": machines,
        "totals": totals,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(OUT)


if __name__ == "__main__":
    main()
