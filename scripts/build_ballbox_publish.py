#!/usr/bin/env python3
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from send_sales_report_email import collect_sales_report

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
BALLBOX = SITE / "ballbox"
DATA_DIR = BALLBOX / "data"
MACHINES_META = ROOT / "config" / "machines_metadata.json"
STATUS_PATH = SITE / "status.json"
ADIDAS_PATH = SITE / "adidas_inventory_snapshot.json"
SALES_PATH = ROOT / "data" / "beetwallet_summary.json"
PRODUCTS_CACHE = ROOT / "runtime" / "vending-web" / "cache" / "vendor_products.json"
ADIDAS_VIEWS_CACHE = ROOT / "runtime" / "vending-web" / "cache" / "adidas_inventory_views.json"
SLOT_MAPPING_PATH = ROOT / "config" / "adidas_slot_products.json"
OPERATIONS_PATH = ROOT / "data" / "beetwallet_operations.json"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(name, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return path


def to_int(value, default=0):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def envelope(artifact, data, *, sources=None, source_as_of=None, max_age_seconds=900, errors=None, ok=True, degraded=False):
    generated_at = now_iso()
    return {
        "ok": ok,
        "artifact": artifact,
        "version": 1,
        "generated_at": generated_at,
        "source_as_of": source_as_of or generated_at,
        "max_age_seconds": max_age_seconds,
        "stale": False,
        "degraded": degraded,
        "errors": errors or [],
        "sources": sources or [],
        "data": data,
    }


def index_by_machine(rows):
    return {str(row.get("machine_id")): row for row in rows or [] if row.get("machine_id")}


def build_sources(status, adidas, sales, products):
    sources = []
    if status:
        sources.append({"name": "status", "generated_at": status.get("updated_at"), "ok": True, "stale": False})
    if adidas:
        sources.append({"name": "inventory_adidas", "generated_at": adidas.get("updated_at"), "ok": adidas.get("error") is None, "stale": False})
    if sales:
        sources.append({"name": "sales", "generated_at": sales.get("meta", {}).get("generated_at"), "ok": True, "stale": False})
    if products:
        sources.append({"name": "products", "generated_at": None, "ok": True, "stale": False})
    return sources


def build_status(status, meta_by_id, adidas_by_id, sales_by_station):
    machines = []
    for row in status.get("machines", []):
        machine_id = str(row.get("machine_id"))
        meta = meta_by_id.get(machine_id, {})
        adidas = adidas_by_id.get(machine_id, {})
        sales = None
        station_id = meta.get("station_id")
        if station_id:
            sales = sales_by_station.get(station_id)
        machines.append({
            **row,
            "slug": meta.get("slug"),
            "label": meta.get("label") or row.get("alias") or machine_id,
            "site": meta.get("site"),
            "client": meta.get("client"),
            "inventory": adidas.get("summary"),
            "inventory_status": adidas.get("inventory_status"),
            "sales_summary": sales,
        })
    return {
        "machine_count": status.get("machine_count", len(machines)),
        "online_count": status.get("online_count", 0),
        "offline_count": status.get("offline_count", 0),
        "overview": status.get("overview", {}),
        "machines": machines,
    }


def build_machines(status, meta_rows, adidas_by_id, sales_by_station):
    status_by_id = index_by_machine(status.get("machines", []))
    machines = []
    for meta in meta_rows:
        machine_id = str(meta["machine_id"])
        status_row = status_by_id.get(machine_id)
        adidas = adidas_by_id.get(machine_id, {})
        sales = sales_by_station.get(meta.get("station_id"), {})
        machines.append({
            "machine_id": machine_id,
            "slug": meta.get("slug"),
            "label": meta.get("label"),
            "alias": meta.get("alias"),
            "site": meta.get("site"),
            "client": meta.get("client"),
            "group_name": (status_row or {}).get("group_name") or meta.get("group_name"),
            "enabled": meta.get("enabled", True),
            "notes": meta.get("notes"),
            "network": (status_row or {}).get("network"),
            "freshness": (status_row or {}).get("freshness"),
            "temperature_raw": (status_row or {}).get("temperature_raw"),
            "door": (status_row or {}).get("door"),
            "stock": (status_row or {}).get("stock"),
            "sales_today": (status_row or {}).get("sales_today"),
            "inventory": adidas.get("summary"),
            "inventory_status": adidas.get("inventory_status") or ("missing_snapshot" if not adidas else None),
            "status_note": adidas.get("status_note") or meta.get("notes"),
            "sales_summary": sales,
            "actions": {
                "inventory": f"/ballbox/inventory/?{urlencode({'machine_id': machine_id})}",
                "sales": f"/ballbox/sales/?{urlencode({'machine_id': machine_id})}",
            },
        })
    return {
        "machine_count": len(machines),
        "machines": machines,
    }


def normalize_slot_no(value):
    return str(value).strip() if value not in (None, "") else ""


def slot_sort_key(value):
    slot = normalize_slot_no(value)
    try:
        return (0, int(slot))
    except Exception:
        return (1, slot)


def fallback_slot_label(slot_no):
    slot = normalize_slot_no(slot_no)
    return f"Slot {slot}" if slot else "Slot —"


def merged_slot_label(member_slots):
    members = [normalize_slot_no(item) for item in (member_slots or []) if normalize_slot_no(item)]
    if not members:
        return fallback_slot_label("")
    if len(members) == 1:
        return fallback_slot_label(members[0])
    return f"Slots {' + '.join(members)}"


def build_merged_slot_maps(machine_merged_slots):
    merged_members_by_anchor = {}
    merged_anchor_by_member = {}
    for raw_anchor, raw_merged in (machine_merged_slots or {}).items():
        anchor = normalize_slot_no(raw_anchor)
        if not anchor:
            continue
        members = []
        seen = set()
        for item in [anchor, *((raw_merged or {}).get("member_slots") or [])]:
            member = normalize_slot_no(item)
            if not member or member in seen:
                continue
            seen.add(member)
            members.append(member)
        if not members:
            members = [anchor]
        merged_members_by_anchor[anchor] = members
        for member in members:
            if member != anchor:
                merged_anchor_by_member[member] = anchor
    return merged_members_by_anchor, merged_anchor_by_member


def parse_price_number(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except Exception:
        return None


def parse_int_number(value):
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except Exception:
        return None


def is_defined_quantity(quantity_int):
    return quantity_int is not None and quantity_int < 100


def is_defined_capacity(capacity_int):
    return capacity_int is not None and capacity_int < 100


def is_defined_price(price_number):
    if price_number is None:
        return False
    rounded = round(price_number)
    return abs(price_number - rounded) < 0.001 and rounded % 1000 == 0


def format_number_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def normalize_name(value):
    return str(value or "").strip()


def resolve_product_profile(name, product_profiles, seen=None):
    key = normalize_name(name)
    if not key:
        return {}
    if seen is None:
        seen = set()
    if key in seen:
        return {}
    seen.add(key)
    raw = dict(product_profiles.get(key) or {})
    if not raw:
        return {}
    base = {}
    copy_from = normalize_name(raw.get("copy_from"))
    if copy_from:
        base.update(resolve_product_profile(copy_from, product_profiles, seen))
    for field, value in raw.items():
        if field == "copy_from" or value in (None, ""):
            continue
        base[field] = value
    return base


def resolve_machine_slot_profile(machine_id, slot_no, machine_slot_profiles, product_profiles):
    raw = dict((machine_slot_profiles.get(machine_id, {}) or {}).get(slot_no) or {})
    if not raw:
        return {}
    base = {}
    copy_from = normalize_name(raw.get("copy_from"))
    if copy_from:
        base.update(resolve_product_profile(copy_from, product_profiles))
    for field, value in raw.items():
        if field == "copy_from" or value in (None, ""):
            continue
        base[field] = value
    return base


def resolve_slot_profile(machine_id, slot_no, mapped_name, display_name, product_profiles, machine_slot_profiles):
    profile = {}
    if mapped_name:
        profile.update(resolve_product_profile(mapped_name, product_profiles))
    elif display_name and not display_name.startswith("Slot "):
        profile.update(resolve_product_profile(display_name, product_profiles))
    profile.update(resolve_machine_slot_profile(machine_id, slot_no, machine_slot_profiles, product_profiles))
    return profile


def enrich_inventory_slots(machine_id, slots, slot_names_by_machine, merged_slots_by_machine, product_profiles, machine_slot_profiles):
    machine_slot_names = slot_names_by_machine.get(machine_id, {}) or {}
    machine_merged_slots = merged_slots_by_machine.get(machine_id, {}) or {}
    merged_members_by_anchor, merged_anchor_by_member = build_merged_slot_maps(machine_merged_slots)
    raw_slot_nos = {normalize_slot_no(row.get("slot_no")) for row in (slots or []) if normalize_slot_no(row.get("slot_no"))}
    synthetic_slots = [
        {
            "machine_id": machine_id,
            "slot_no": slot_no,
            "product_id": "",
            "name": "",
            "price": "",
            "capacity": "",
            "quantity": "",
            "image_url": "",
            "work_status": "",
            "download_state": "",
            "source": "mapping_missing_vendor_slot",
        }
        for slot_no in sorted(machine_slot_names, key=slot_sort_key)
        if normalize_slot_no(slot_no) and normalize_slot_no(slot_no) not in raw_slot_nos
    ]
    enriched = []
    grouped = defaultdict(lambda: {
        "product_name": "",
        "slot_count": 0,
        "physical_slot_count": 0,
        "quantity": 0,
        "capacity": 0,
        "low_slots": 0,
        "empty_slots": 0,
        "slot_refs": [],
        "stock_defined": True,
        "quantity_defined": True,
        "capacity_defined": True,
        "price_defined": True,
    })

    for raw in [*(slots or []), *synthetic_slots]:
        slot_no = normalize_slot_no(raw.get("slot_no"))
        if slot_no and merged_anchor_by_member.get(slot_no):
            continue
        vendor_name = normalize_name(raw.get("name"))
        mapped_name = normalize_name(machine_slot_names.get(slot_no))
        if not vendor_name and not mapped_name:
            continue
        display_name = vendor_name or mapped_name or fallback_slot_label(slot_no)
        slot_profile = resolve_slot_profile(machine_id, slot_no, mapped_name, display_name, product_profiles, machine_slot_profiles)

        raw_quantity_int = parse_int_number(raw.get("quantity"))
        raw_capacity_int = parse_int_number(raw.get("capacity"))
        raw_price_number = parse_price_number(raw.get("price"))

        quantity_defined = is_defined_quantity(raw_quantity_int)
        raw_capacity_defined = is_defined_capacity(raw_capacity_int)
        raw_price_defined = is_defined_price(raw_price_number)

        inferred_capacity_int = parse_int_number(slot_profile.get("capacity")) if slot_profile else None
        inferred_price_number = parse_price_number(slot_profile.get("price")) if slot_profile else None

        capacity_int = raw_capacity_int if raw_capacity_defined else inferred_capacity_int
        capacity_defined = capacity_int is not None
        price_number = raw_price_number if raw_price_defined else inferred_price_number
        price_defined = price_number is not None
        quantity_int = raw_quantity_int if quantity_defined else None

        if not quantity_defined:
            inventory_state = "unknown"
        elif quantity_int <= 0:
            inventory_state = "empty"
        elif quantity_int <= 2:
            inventory_state = "low"
        else:
            inventory_state = "ok"

        member_slots = merged_members_by_anchor.get(slot_no) or ([slot_no] if slot_no else [])
        slot_label = merged_slot_label(member_slots)
        price_text = raw.get("price") if raw_price_defined else format_number_text(price_number)
        capacity_text = raw.get("capacity") if raw_capacity_defined else format_number_text(capacity_int)
        quantity_text = raw.get("quantity") if quantity_defined else None
        inference_source = normalize_name(slot_profile.get("source") or slot_profile.get("copy_from") or mapped_name or display_name)

        slot = {
            **raw,
            "slot_no": slot_no,
            "slot_label": slot_label,
            "vendor_name": vendor_name,
            "mapped_name": mapped_name,
            "display_name": display_name,
            "display_slot_label": f"{display_name} · {slot_label}",
            "name_source": "vendor" if vendor_name else ("mapping" if mapped_name else "fallback"),
            "raw_quantity": raw.get("quantity"),
            "raw_capacity": raw.get("capacity"),
            "raw_price": raw.get("price"),
            "raw_quantity_int": raw_quantity_int,
            "raw_capacity_int": raw_capacity_int,
            "raw_price_number": raw_price_number,
            "quantity": quantity_text,
            "capacity": capacity_text,
            "price": price_text,
            "quantity_int": quantity_int,
            "capacity_int": capacity_int,
            "price_number": price_number,
            "stock_defined": quantity_defined,
            "quantity_defined": quantity_defined,
            "capacity_defined": capacity_defined,
            "price_defined": price_defined,
            "quantity_inferred": False,
            "capacity_inferred": (not raw_capacity_defined) and capacity_defined,
            "price_inferred": (not raw_price_defined) and price_defined,
            "inference_source": inference_source or None,
            "inventory_state": inventory_state,
            "merged_member_slots": member_slots,
            "physical_slot_count": len(member_slots),
            "is_merged_anchor": len(member_slots) > 1,
        }
        enriched.append(slot)

        grouped_row = grouped[display_name]
        grouped_row["product_name"] = display_name
        grouped_row["slot_count"] += 1
        grouped_row["physical_slot_count"] += len(member_slots)
        if quantity_defined:
            grouped_row["quantity"] += max(quantity_int or 0, 0)
        if capacity_defined:
            grouped_row["capacity"] += max(capacity_int or 0, 0)
        grouped_row["stock_defined"] = grouped_row["stock_defined"] and quantity_defined
        grouped_row["quantity_defined"] = grouped_row["quantity_defined"] and quantity_defined
        grouped_row["capacity_defined"] = grouped_row["capacity_defined"] and capacity_defined
        grouped_row["price_defined"] = grouped_row["price_defined"] and price_defined
        grouped_row["low_slots"] += 1 if inventory_state == "low" else 0
        grouped_row["empty_slots"] += 1 if inventory_state == "empty" else 0
        if slot_no:
            grouped_row["slot_refs"].append({"slot_no": slot_no, "slot_label": slot_label})

    for row in grouped.values():
        row["slot_refs"] = sorted(row["slot_refs"], key=lambda item: slot_sort_key(item.get("slot_no")))
        row["slots"] = [item["slot_no"] for item in row["slot_refs"]]
        row["slot_labels"] = [item["slot_label"] for item in row["slot_refs"]]
        row["slots_text"] = ", ".join(row["slot_labels"]) if row["slot_labels"] else "—"
        row["stock_defined"] = row["quantity_defined"]
        row["quantity_display"] = row["quantity"] if row["quantity_defined"] else None
        row["capacity_display"] = row["capacity"] if row["capacity_defined"] else None
        row.pop("slot_refs", None)

    enriched.sort(key=lambda row: slot_sort_key(row.get("slot_no")))
    product_summary = sorted(grouped.values(), key=lambda row: (-row["empty_slots"], -row["low_slots"], row["product_name"].lower()))
    return enriched, product_summary


def summarize_inventory_slots(slots, fallback_summary=None):
    derived_physical_slots = sum(int(slot.get("physical_slot_count", 1) or 1) for slot in (slots or []))
    summary = {
        "total_slots": len(slots or []),
        "physical_slots": derived_physical_slots,
        "total_quantity": 0,
        "total_capacity": 0,
        "empty_slots": 0,
        "low_slots": 0,
        "suspicious_capacity_slots": 0,
    }
    for slot in slots or []:
        if slot.get("quantity_defined"):
            summary["total_quantity"] += max(int(slot.get("quantity_int", 0) or 0), 0)
        if slot.get("capacity_defined"):
            summary["total_capacity"] += max(int(slot.get("capacity_int", 0) or 0), 0)
        if (slot.get("raw_capacity_int") or 0) >= 100 or (slot.get("raw_quantity_int") or 0) >= 100:
            summary["suspicious_capacity_slots"] += 1
        summary["empty_slots"] += 1 if slot.get("inventory_state") == "empty" else 0
        summary["low_slots"] += 1 if slot.get("inventory_state") == "low" else 0
    if fallback_summary:
        summary["raw_total_slots"] = int(fallback_summary.get("total_slots", summary["physical_slots"]) or 0)
        summary["raw_total_quantity"] = int(fallback_summary.get("total_quantity", summary["total_quantity"]) or 0)
    return summary


def build_inventory(meta_rows, adidas_by_id, views_by_slug, slot_names_by_machine, merged_slots_by_machine, product_profiles, machine_slot_profiles):
    machines = []
    totals = {"machine_count": 0, "total_slots": 0, "total_quantity": 0, "empty_slots": 0, "low_slots": 0}
    for meta in meta_rows:
        machine_id = str(meta["machine_id"])
        item = dict(adidas_by_id.get(machine_id) or {})
        view = dict(views_by_slug.get(meta.get("slug")) or {})
        if not item:
            item = {
                "machine_id": machine_id,
                "client": "Adidas Padel" if meta.get("client") == "adidas" else meta.get("client"),
                "label": meta.get("label"),
                "site": meta.get("site"),
                "inventory_status": "missing_snapshot",
                "status_note": meta.get("notes"),
                "fetch_error": None,
                "summary": None,
                "sales": None,
            }
        item["machine_id"] = machine_id
        item["slug"] = meta.get("slug")
        raw_summary = dict(item.get("summary") or {})
        slots, product_summary = enrich_inventory_slots(machine_id, view.get("slots", []), slot_names_by_machine, merged_slots_by_machine, product_profiles, machine_slot_profiles)
        item["slots"] = slots
        item["product_summary"] = product_summary
        item["raw_summary"] = raw_summary or None
        if slots:
            item["summary"] = summarize_inventory_slots(slots, raw_summary)
        item["has_untrusted_stock"] = any(not slot.get("stock_defined", True) for slot in slots)
        item["has_untrusted_price"] = any(not slot.get("price_defined", True) for slot in slots)
        item["low_slots_preview"] = [
            {
                "slot_no": slot.get("slot_no"),
                "slot_label": slot.get("slot_label"),
                "display_name": slot.get("display_name"),
                "quantity": slot.get("quantity_int"),
                "inventory_state": slot.get("inventory_state"),
            }
            for slot in slots
            if slot.get("inventory_state") in {"low", "empty"}
        ][:4]
        if item.get("inventory_status") == "missing_snapshot" and view.get("inventory_status") and view.get("inventory_status") != "missing_machine_id":
            item["inventory_status"] = view.get("inventory_status")
        machines.append(item)
        totals["machine_count"] += 1
        summary = item.get("summary") or {}
        totals["total_slots"] += int(summary.get("total_slots", 0) or 0)
        totals["total_quantity"] += int(summary.get("total_quantity", 0) or 0)
        totals["empty_slots"] += int(summary.get("empty_slots", 0) or 0)
        totals["low_slots"] += int(summary.get("low_slots", 0) or 0)
    totals["has_untrusted_stock"] = any(machine.get("has_untrusted_stock") for machine in machines)
    totals["has_untrusted_price"] = any(machine.get("has_untrusted_price") for machine in machines)
    return {"totals": totals, "machines": machines}


def build_sales(sales, meta_rows, inventory_data, slot_names_by_machine, product_profiles, monthly_report=None):
    station_to_machine = {row.get("station_id"): row for row in meta_rows if row.get("station_id")}
    inventory_slot_index = {}
    for machine in inventory_data.get("machines", []):
        machine_id = str(machine.get("machine_id") or "")
        for slot in machine.get("slots", []) or []:
            inventory_slot_index[(machine_id, normalize_slot_no(slot.get("slot_no")))] = slot

    by_station = []
    for row in sales.get("by_station", []):
        meta = station_to_machine.get(row.get("station_id"), {})
        by_station.append({**row, "machine_id": meta.get("machine_id"), "slug": meta.get("slug"), "client": meta.get("client")})
    by_station_slot = []
    for row in sales.get("by_station_slot", []):
        meta = station_to_machine.get(row.get("station_id"), {})
        machine_id = str(meta.get("machine_id") or "")
        slot_no = normalize_slot_no(row.get("slot"))
        slot_info = inventory_slot_index.get((machine_id, slot_no), {})
        mapped_name = normalize_name((slot_names_by_machine.get(machine_id, {}) or {}).get(slot_no))
        mapped_profile = resolve_product_profile(mapped_name, product_profiles) if mapped_name else {}
        product_name = slot_info.get("display_name") or mapped_name
        if not product_name:
            continue
        inventory_capacity = slot_info.get("capacity_int")
        if inventory_capacity is None and mapped_profile:
            inventory_capacity = parse_int_number(mapped_profile.get("capacity"))
        inventory_stock_defined = slot_info.get("stock_defined") if slot_info else False
        by_station_slot.append({
            **row,
            "machine_id": meta.get("machine_id"),
            "slug": meta.get("slug"),
            "client": meta.get("client"),
            "slot_label": fallback_slot_label(slot_no),
            "product_name": product_name,
            "inventory_quantity": slot_info.get("quantity_int"),
            "inventory_capacity": inventory_capacity,
            "inventory_state": slot_info.get("inventory_state"),
            "inventory_status_label": slot_info.get("display_slot_label") or f"{product_name} · {fallback_slot_label(slot_no)}",
            "inventory_stock_defined": inventory_stock_defined,
        })
    return {
        "meta": sales.get("meta", {}),
        "totals": sales.get("totals", {}),
        "monthly_report": monthly_report or {},
        "by_day": sales.get("by_day", []),
        "by_station": by_station,
        "by_station_slot": by_station_slot,
    }


def index_inventory_by_machine(inventory_data):
    return {str(machine.get("machine_id")): machine for machine in (inventory_data.get("machines", []) or []) if machine.get("machine_id")}


def build_products(products, inventory_data):
    rows = products.get("products", []) if products else []
    configured_price_points = []
    seen = set()
    for machine in inventory_data.get("machines", []):
        for slot in machine.get("slots", []) or []:
            key = (slot.get("price"), slot.get("display_name"), slot.get("product_id"))
            if key in seen:
                continue
            seen.add(key)
            configured_price_points.append({
                "machine_id": machine.get("machine_id"),
                "label": machine.get("label"),
                "site": machine.get("site"),
                "price": slot.get("price"),
                "name": slot.get("display_name"),
                "product_id": slot.get("product_id"),
            })
    return {
        "count": len(rows),
        "categories": products.get("categories", []) if products else [],
        "manufacturers": products.get("manufacturers", []) if products else [],
        "products": rows,
        "configured_price_points": configured_price_points,
        "truth_note": "Vendor catalog and machine-configured slot catalog are different things. Machine reality today is visible in inventory/slots; vendor products here may be a partial or test catalog.",
    }


def build_home(status_data, machines_data, inventory_data, sales_data, products_data):
    spotlight = next((m for m in machines_data.get("machines", []) if m.get("slug") == "almagro"), None)
    return {
        "summary": {
            "machines": machines_data.get("machine_count", 0),
            "machines_with_live_status": status_data.get("machine_count", 0),
            "online": status_data.get("online_count", 0),
            "offline": status_data.get("offline_count", 0),
            "inventory_machines": inventory_data.get("totals", {}).get("machine_count", 0),
            "inventory_slots": inventory_data.get("totals", {}).get("total_slots", 0),
            "sales_ventas": sales_data.get("totals", {}).get("ventas", 0),
            "sales_monto_total": sales_data.get("totals", {}).get("monto_total", 0),
            "products": products_data.get("count", 0),
        },
        "clients": [{"slug": "adidas", "label": "Adidas", "inventory_url": "/ballbox/inventory/?client=adidas", "sales_url": "/ballbox/sales/?client=adidas"}],
        "links": [
            {"label": "Status", "url": "/ballbox/status/"},
            {"label": "Machines", "url": "/ballbox/machines/"},
            {"label": "Inventory", "url": "/ballbox/inventory/"},
            {"label": "Sales", "url": "/ballbox/sales/"},
            {"label": "Catalog", "url": "/ballbox/products/"},
            {"label": "Cloud", "url": "/ballbox/cloud/"},
            {"label": "QA", "url": "/ballbox/qa/"},
        ],
        "spotlight": spotlight,
    }


def build_cloud(status, adidas, sales, products):
    return {
        "sources": [
            {"name": "status", "path": "site/status.json", "public_url": "/ballbox/data/status.json", "generated_at": status.get("updated_at") if status else None, "ok": bool(status), "notes": "OurVend status snapshot"},
            {"name": "inventory", "path": "site/adidas_inventory_snapshot.json", "public_url": "/ballbox/data/inventory.json", "generated_at": adidas.get("updated_at") if adidas else None, "ok": bool(adidas) and adidas.get("error") is None, "notes": "Adidas inventory snapshot"},
            {"name": "sales", "path": "data/beetwallet_summary.json", "public_url": "/ballbox/data/sales.json", "generated_at": (sales.get("meta") or {}).get("generated_at") if sales else None, "ok": bool(sales), "notes": "Beetwallet summary"},
            {"name": "products", "path": "runtime/vending-web/cache/vendor_products.json", "public_url": "/ballbox/data/products.json", "generated_at": None, "ok": bool(products), "notes": "Last cached vendor products"},
            {"name": "machines", "path": "config/machines_metadata.json + snapshots", "public_url": "/ballbox/data/machines.json", "generated_at": status.get("updated_at") if status else None, "ok": True, "notes": "Ballbox machine directory"},
            {"name": "home", "path": "derived aggregate", "public_url": "/ballbox/data/home.json", "generated_at": status.get("updated_at") if status else None, "ok": True, "notes": "Ballbox home aggregate"},
        ],
        "knowledge": [
            {"name": "Q&A", "url": "/ballbox/qa/", "notes": "Ask company and ops questions over ballbox-db canonical files."},
            {"name": "Control-plane health", "url": "/ballbox/api/health", "notes": "Minimal Flask control-plane health endpoint."},
            {"name": "Canonical files index", "repo_path": "/home/sebas/work/projects/ballbox-db/indexes/current-canonical-files.md", "notes": "Entry point for company truth and remote agent Q&A."},
            {"name": "Operational JSON source guide", "repo_path": "/home/sebas/work/projects/ballbox-db/operations/vending-status-json-artifacts.md", "notes": "Maps questions like total sold amount to the right /ballbox/data JSON."},
            {"name": "Open questions", "repo_path": "/home/sebas/work/projects/ballbox-db/operations/open-questions.md", "notes": "Known gaps and unresolved truth."},
        ],
        "system_map": {
            "public_surface": ["/ballbox/", "/ballbox/status/", "/ballbox/machines/", "/ballbox/inventory/", "/ballbox/sales/", "/ballbox/products/", "/ballbox/cloud/", "/ballbox/qa/"],
            "repos": [
                {"name": "vending-status", "role": "portal pages, snapshots, publish, minimal control plane"},
                {"name": "ballbox-db", "role": "canonical knowledge base and QA backend"},
                {"name": "ballbox-machine-ads-sync", "role": "machine-side Android ads/media sync sidecar"},
            ],
            "runtime": [
                {"name": "public static publish", "path": "/var/www/public-funnel-ballbox"},
                {"name": "control plane flask", "path": "127.0.0.1:5081", "public_health": "/ballbox/api/health"},
                {"name": "qa backend", "path": "127.0.0.1:31417"},
                {"name": "internal pi-web", "path": "127.0.0.1:31416", "public_role": "none"},
            ],
        },
        "legacy_docs": [
            {"label": "OurVend docs", "url": "/ballbox/ourvend/index.html"},
            {"label": "Machine docs", "url": "/ballbox/machine/index.html"},
        ],
    }


def build_monthly_sales_report(operations, inventory_data, slot_mapping, meta_rows):
    month_key = datetime.now().strftime("%Y-%m")
    if not operations:
        return {"month": month_key, "error": "missing operations payload"}
    try:
        report = collect_sales_report(
            operations_payload=operations,
            inventory_payload=inventory_data,
            mapping_payload=slot_mapping,
            machines_meta=meta_rows,
            target_client="adidas",
            month_key=month_key,
        )
    except Exception as exc:
        return {"month": month_key, "error": str(exc)}
    return report


def build_profile_maps(slot_mapping):
    product_profiles = {normalize_name(name): dict(profile or {}) for name, profile in (slot_mapping.get("fallback_profiles") or {}).items() if normalize_name(name)}
    machine_slot_profiles = {}
    for machine_id, slot_profiles in (slot_mapping.get("machine_slot_profiles") or {}).items():
        machine_slot_profiles[str(machine_id)] = {
            normalize_slot_no(slot_no): dict(profile or {})
            for slot_no, profile in (slot_profiles or {}).items()
            if normalize_slot_no(slot_no)
        }
    return product_profiles, machine_slot_profiles


def main():
    status = load_json(STATUS_PATH, {}) or {}
    adidas = load_json(ADIDAS_PATH, {}) or {}
    sales = load_json(SALES_PATH, {}) or {}
    operations = load_json(OPERATIONS_PATH, {}) or {}
    products = load_json(PRODUCTS_CACHE, {}) or {}
    views_cache = load_json(ADIDAS_VIEWS_CACHE, {}) or {}
    slot_mapping = load_json(SLOT_MAPPING_PATH, {}) or {}
    meta_rows = load_json(MACHINES_META, []) or []
    meta_by_id = {str(row["machine_id"]): row for row in meta_rows}
    adidas_rows = list(adidas.get("machines", []) or [])
    adidas_by_id = index_by_machine(adidas_rows)
    views_by_slug = {str(row.get("slug")): row for row in views_cache.get("views", []) if row.get("slug")}
    slot_names_by_machine = slot_mapping.get("machines", {}) or {}
    merged_slots_by_machine = slot_mapping.get("merged_slots", {}) or {}
    product_profiles, machine_slot_profiles = build_profile_maps(slot_mapping)
    sales_by_station = {str(row.get("station_id")): row for row in sales.get("by_station", []) if row.get("station_id")}
    sources = build_sources(status, adidas, sales, products)

    inventory_data = build_inventory(meta_rows, adidas_by_id, views_by_slug, slot_names_by_machine, merged_slots_by_machine, product_profiles, machine_slot_profiles)
    inventory_by_id = index_inventory_by_machine(inventory_data)
    status_data = build_status(status, meta_by_id, inventory_by_id, sales_by_station)
    machines_data = build_machines(status, meta_rows, inventory_by_id, sales_by_station)
    monthly_report = build_monthly_sales_report(operations, inventory_data, slot_mapping, meta_rows)
    sales_data = build_sales(sales, meta_rows, inventory_data, slot_names_by_machine, product_profiles, monthly_report)
    products_data = build_products(products, inventory_data)
    home_data = build_home(status_data, machines_data, inventory_data, sales_data, products_data)
    cloud_data = build_cloud(status, adidas, sales, products)

    write_json("status", envelope("status", status_data, sources=sources, source_as_of=status.get("updated_at"), max_age_seconds=300))
    write_json("machines", envelope("machines", machines_data, sources=sources, source_as_of=status.get("updated_at"), max_age_seconds=900, degraded=any(not m.get("network") for m in machines_data.get("machines", []))))
    write_json("inventory", envelope("inventory", inventory_data, sources=sources, source_as_of=adidas.get("updated_at") or status.get("updated_at"), max_age_seconds=900, degraded=any((m.get("inventory_status") or "").startswith("missing") for m in inventory_data.get("machines", []))))
    write_json("sales", envelope("sales", sales_data, sources=sources, source_as_of=(sales.get("meta") or {}).get("generated_at"), max_age_seconds=900))
    write_json("products", envelope("products", products_data, sources=sources, source_as_of=status.get("updated_at"), max_age_seconds=1800, degraded=not bool(products_data.get("products"))))
    write_json("home", envelope("home", home_data, sources=sources, source_as_of=status.get("updated_at"), max_age_seconds=300))
    write_json("cloud", envelope("cloud", cloud_data, sources=sources, source_as_of=now_iso(), max_age_seconds=300))
    print(DATA_DIR)


if __name__ == "__main__":
    main()
