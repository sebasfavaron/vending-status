#!/usr/bin/env python3
import argparse
import calendar
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "sales_email.json"
DEFAULT_OPERATIONS_PATH = ROOT / "data" / "beetwallet_operations.json"
DEFAULT_INVENTORY_PATH = ROOT / "site" / "ballbox" / "data" / "inventory.json"
DEFAULT_MAPPING_PATH = ROOT / "config" / "adidas_slot_products.json"
DEFAULT_MACHINES_META_PATH = ROOT / "config" / "machines_metadata.json"
DEFAULT_PREVIEW_PATH = Path("/home/sebas/runtime/ballbox/tmp/sales_report_email_preview.html")
DEFAULT_SECRET_ENV_PATHS = [
    Path("/home/sebas/runtime/secrets/resend.env"),
    Path("/home/sebas/runtime/secrets/ballbox-alerts.env"),
    ROOT / ".env",
]
DEFAULT_LOGO_URL = "https://ballbox.app/images/ballbox-logo-full.png"
RESEND_API_URL = "https://api.resend.com/emails"
TIMEOUT = 30
# Email-safe sRGB approximations of the user-provided Lab anchors:
# - lab(30.372% -13.1853 -18.7887) -> #004f64
# - lab(80.1641% 16.6016 99.2089) -> #ffb800
# - lab(81.0535% -36.5689 86.4309) -> #b0d900
PALETTE = [
    "#004f64",  # teal anchor
    "#ffb800",  # gold anchor
    "#b0d900",  # lime anchor
    "#4f8a2d",  # moss green bridge
    "#d99d00",  # gold shade
    "#8db300",  # lime shade
    "#2c8799",  # soft teal
    "#ffd054",  # gold tint
    "#c6ea45",  # lime tint
    "#4fa1b0",  # mist teal
    "#e7ad1a",  # warm gold
    "#9dc400",  # fresh lime
]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


for env_path in DEFAULT_SECRET_ENV_PATHS:
    load_env_file(env_path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def resolve_path(value: str | None, default_path: Path) -> Path:
    if not value:
        return default_path
    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT / path


def normalize_text(value) -> str:
    return str(value or "").strip()


def normalize_client(value) -> str:
    return " ".join(normalize_text(value).lower().split())


def parse_amount(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def money(value) -> str:
    return f"${parse_amount(value):,.0f}".replace(",", ".")


def parse_dt(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def pick_station_data(op):
    estacion = op.get("estacion")
    if isinstance(estacion, dict):
        station_id = estacion.get("_id") or estacion.get("id") or estacion.get("uid") or "unknown"
        station_name = estacion.get("nombre") or estacion.get("uid") or station_id
        return str(station_id), str(station_name)
    if estacion:
        station_id = str(estacion)
        return station_id, station_id
    for key in ("estacionId", "stationId"):
        value = op.get(key)
        if value:
            value = str(value)
            return value, value
    venta = op.get("venta") or {}
    maquina = venta.get("maquina") or {}
    if isinstance(maquina, dict):
        station_id = maquina.get("_id") or maquina.get("id") or maquina.get("estacion") or "unknown"
        station_name = maquina.get("nombre") or station_id
        return str(station_id), str(station_name)
    return "unknown", "unknown"


def pick_slot(op):
    venta = op.get("venta") or {}
    value = venta.get("seleccion")
    return str(value) if value not in (None, "") else "unknown"


def pick_amount(op):
    value = op.get("monto")
    if value is None:
        value = op.get("amount", 0)
    return parse_amount(value)


def pick_when(op):
    for key in ("fecha", "createdAt", "updatedAt", "date"):
        dt = parse_dt(op.get(key)) if op.get(key) else None
        if dt:
            return dt
    venta = op.get("venta") or {}
    for key in ("fecha", "createdAt", "created", "approved"):
        dt = parse_dt(venta.get(key)) if venta.get(key) else None
        if dt:
            return dt
    return None


def month_key_for(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def resolve_month(spec: str | None, operations_payload: dict) -> tuple[int, int, str]:
    if spec and spec != "current":
        year, month = spec.split("-", 1)
        return int(year), int(month), f"{int(year):04d}-{int(month):02d}"
    now = datetime.now(timezone.utc)
    meta_month = normalize_text(((operations_payload.get("meta") or {}).get("generated_at")))
    if meta_month:
        try:
            dt = datetime.fromisoformat(meta_month + "T00:00:00+00:00")
            return dt.year, dt.month, month_key_for(dt)
        except ValueError:
            pass
    return now.year, now.month, month_key_for(now)


def iter_month_days(year: int, month: int):
    total_days = calendar.monthrange(year, month)[1]
    return [f"{year:04d}-{month:02d}-{day:02d}" for day in range(1, total_days + 1)]


def escape_html(value) -> str:
    text = normalize_text(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def normalize_slot(value) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    try:
        return str(int(text))
    except ValueError:
        return text


def load_config(path: Path) -> dict:
    config = load_json(path, {}) or {}
    return {
        "enabled": bool(config.get("enabled", False)),
        "provider": normalize_text(config.get("provider") or "resend") or "resend",
        "sender_name": normalize_text(config.get("sender_name") or "Ballbox"),
        "sender_email": normalize_text(config.get("sender_email") or "communications@ballbox.app"),
        "reply_to": normalize_text(config.get("reply_to") or ""),
        "recipient_emails": [normalize_text(item) for item in config.get("recipient_emails") or [] if normalize_text(item)],
        "subject_prefix": normalize_text(config.get("subject_prefix") or "Ballbox"),
        "client": normalize_text(config.get("client") or "adidas") or "adidas",
        "client_label": normalize_text(config.get("client_label") or "Adidas") or "Adidas",
        "month": normalize_text(config.get("month") or "current") or "current",
        "operations_path": resolve_path(config.get("operations_path"), DEFAULT_OPERATIONS_PATH),
        "inventory_path": resolve_path(config.get("inventory_path"), DEFAULT_INVENTORY_PATH),
        "mapping_path": resolve_path(config.get("mapping_path"), DEFAULT_MAPPING_PATH),
        "machines_meta_path": resolve_path(config.get("machines_meta_path"), DEFAULT_MACHINES_META_PATH),
        "preview_path": resolve_path(config.get("preview_path"), DEFAULT_PREVIEW_PATH),
        "logo_url": normalize_text(config.get("logo_url") or DEFAULT_LOGO_URL),
    }


def build_product_index(inventory_payload: dict, mapping_payload: dict) -> dict:
    index = {}
    for machine in ((inventory_payload.get("data") or {}).get("machines") or []):
        machine_id = normalize_text(machine.get("machine_id"))
        for slot in machine.get("slots") or []:
            slot_no = normalize_slot(slot.get("slot_no"))
            if not machine_id or not slot_no:
                continue
            name = normalize_text(slot.get("display_name") or slot.get("mapped_name") or slot.get("name"))
            if name:
                index[(machine_id, slot_no)] = name
    for machine_id, slots in ((mapping_payload.get("machines") or {})).items():
        for slot_no, label in (slots or {}).items():
            key = (normalize_text(machine_id), normalize_slot(slot_no))
            index.setdefault(key, normalize_text(label))
    return index


def build_ignored_sales_index(mapping_payload: dict) -> dict:
    out = defaultdict(set)
    machine_notes = mapping_payload.get("machine_notes") or {}
    for machine_id, note in machine_notes.items():
        for item in note.get("ignored_sales") or []:
            slot = normalize_slot(item.get("slot"))
            amount = parse_amount(item.get("amount"))
            if slot:
                out[normalize_text(machine_id)].add((slot, amount))
    return out


def build_subject(prefix: str, client_label: str, month_key: str) -> str:
    return f"{prefix} · {client_label}: ventas por máquina {month_key}"


def color_map(products: list[str]) -> dict:
    colors = {}
    for idx, product in enumerate(products):
        colors[product] = PALETTE[idx % len(PALETTE)]
    return colors


def collapse_day_rows(day_rows: list[dict]) -> list[dict]:
    collapsed = []
    gap_start = None
    gap_end = None
    gap_count = 0

    def flush_gap():
        nonlocal gap_start, gap_end, gap_count
        if gap_count <= 0:
            return
        collapsed.append({
            "kind": "gap",
            "start_day": gap_start,
            "end_day": gap_end,
            "gap_count": gap_count,
        })
        gap_start = None
        gap_end = None
        gap_count = 0

    for row in day_rows:
        if row["total_amount"] <= 0:
            if gap_count == 0:
                gap_start = row["day"]
            gap_end = row["day"]
            gap_count += 1
            continue
        flush_gap()
        collapsed.append({"kind": "day", **row})

    flush_gap()
    return collapsed


def collect_sales_report(*, operations_payload: dict, inventory_payload: dict, mapping_payload: dict, machines_meta: list[dict], target_client: str, month_key: str) -> dict:
    product_index = build_product_index(inventory_payload, mapping_payload)
    ignored_sales = build_ignored_sales_index(mapping_payload)
    client_norm = normalize_client(target_client)
    station_to_meta = {normalize_text(row.get("station_id")): row for row in machines_meta if row.get("station_id")}
    machines = [row for row in machines_meta if normalize_client(row.get("client")) == client_norm]
    machine_by_id = {normalize_text(row.get("machine_id")): row for row in machines}
    days = []
    if month_key:
        year, month = [int(part) for part in month_key.split("-", 1)]
        days = iter_month_days(year, month)

    by_machine_day_product = defaultdict(lambda: {"amount": 0.0, "sales": 0})
    by_machine_totals = defaultdict(lambda: {"amount": 0.0, "sales": 0})
    by_machine_product_totals = defaultdict(lambda: {"amount": 0.0, "sales": 0})
    operations_count = 0
    kept_count = 0

    for op in operations_payload.get("operations") or []:
        operations_count += 1
        when = pick_when(op)
        if not when or month_key_for(when) != month_key:
            continue
        station_id, station_name = pick_station_data(op)
        meta = station_to_meta.get(normalize_text(station_id))
        if not meta:
            continue
        machine_id = normalize_text(meta.get("machine_id"))
        if not machine_id or machine_id not in machine_by_id:
            continue
        slot = normalize_slot(pick_slot(op))
        amount = pick_amount(op)
        if (slot, amount) in ignored_sales.get(machine_id, set()):
            continue
        product_name = product_index.get((machine_id, slot)) or f"Slot {slot or 'unknown'}"
        day = when.date().isoformat()
        by_machine_day_product[(machine_id, day, product_name)]["amount"] += amount
        by_machine_day_product[(machine_id, day, product_name)]["sales"] += 1
        by_machine_totals[machine_id]["amount"] += amount
        by_machine_totals[machine_id]["sales"] += 1
        by_machine_product_totals[(machine_id, product_name)]["amount"] += amount
        by_machine_product_totals[(machine_id, product_name)]["sales"] += 1
        kept_count += 1

    all_products = sorted({key[2] for key in by_machine_day_product.keys()})
    colors = color_map(all_products)
    machine_sections = []
    grand_total_amount = 0.0
    grand_total_sales = 0

    for meta in sorted(machines, key=lambda row: normalize_text(row.get("label") or row.get("machine_id"))):
        machine_id = normalize_text(meta.get("machine_id"))
        total = by_machine_totals.get(machine_id, {"amount": 0.0, "sales": 0})
        grand_total_amount += total["amount"]
        grand_total_sales += total["sales"]
        product_totals = []
        machine_products = [
            {"product": product, **vals}
            for (mid, product), vals in by_machine_product_totals.items()
            if mid == machine_id
        ]
        machine_products.sort(key=lambda row: (-row["amount"], row["product"].lower()))
        for row in machine_products:
            product_totals.append({**row, "color": colors[row["product"]]})

        day_rows = []
        for day in days:
            products = [
                {"product": product, **vals}
                for (mid, row_day, product), vals in by_machine_day_product.items()
                if mid == machine_id and row_day == day
            ]
            products.sort(key=lambda row: (-row["amount"], row["product"].lower()))
            total_amount = sum(row["amount"] for row in products)
            total_sales = sum(row["sales"] for row in products)
            segments = []
            if total_amount > 0:
                for row in products:
                    share = (row["amount"] / total_amount) * 100 if total_amount else 0
                    segments.append({
                        "product": row["product"],
                        "amount": row["amount"],
                        "sales": row["sales"],
                        "share": share,
                        "color": colors[row["product"]],
                    })
            day_rows.append({
                "day": day,
                "total_amount": total_amount,
                "total_sales": total_sales,
                "segments": segments,
            })

        machine_sections.append({
            "machine_id": machine_id,
            "label": meta.get("label") or machine_id,
            "site": meta.get("site") or "",
            "slug": meta.get("slug") or "",
            "total_amount": total["amount"],
            "total_sales": total["sales"],
            "product_totals": product_totals,
            "day_rows": day_rows,
            "collapsed_day_rows": collapse_day_rows(day_rows),
        })

    return {
        "month": month_key,
        "days": days,
        "machine_sections": machine_sections,
        "product_colors": colors,
        "totals": {
            "sales": grand_total_sales,
            "amount": grand_total_amount,
        },
        "source": {
            "operations_count": operations_count,
            "kept_sales_count": kept_count,
            "inventory_generated_at": inventory_payload.get("generated_at"),
            "sales_generated_at": (operations_payload.get("meta") or {}).get("generated_at"),
        },
    }


def build_segment_html(segments: list[dict], *, interactive: bool = True) -> str:
    if not segments:
        return "<div style='height:16px;background:linear-gradient(90deg,#d8dee8 0%,#eef2f7 100%);border-radius:999px'></div>"
    cells = []
    total_segments = len(segments)
    for idx, segment in enumerate(segments):
        width = max(segment["share"], 0.8)
        tip = f"{segment['product']} · {money(segment['amount'])} · {segment['sales']} ventas · {segment['share']:.1f}% del monto del día"
        radius = ""
        if total_segments == 1:
            radius = "border-radius:999px;"
        elif idx == 0:
            radius = "border-radius:999px 0 0 999px;"
        elif idx == total_segments - 1:
            radius = "border-radius:0 999px 999px 0;"
        tab_index = " tabindex='0'" if interactive else ""
        tooltip_html = ""
        if interactive:
            tooltip_html = (
                f"<span class='bb-tip' style='display:none;visibility:hidden;max-height:0;overflow:hidden;opacity:0;position:absolute;left:50%;bottom:22px;transform:translate(-50%,-4px);background:#07111f;color:#fff;border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:6px 8px;font-size:11px;line-height:1.3;white-space:nowrap;pointer-events:none;z-index:999;box-shadow:0 8px 20px rgba(7,17,31,.28)'>{escape_html(tip)}</span>"
            )
        cells.append(
            f"<span class='bb-seg'{tab_index} "
            f"style='display:block;float:left;width:{width:.2f}%;height:16px;background:{segment['color']};position:relative;{radius}'>"
            f"{tooltip_html}&nbsp;"
            "</span>"
        )
    return "<div class='bb-bar'><div class='bb-bar-inner'>" + "".join(cells) + "</div><div style='clear:both'></div></div>"


def build_text_body(report: dict, client_label: str) -> str:
    lines = [
        f"Hola,",
        "",
        f"Ventas por máquina · {client_label} · {report['month']}",
        "",
        f"Ventas totales: {report['totals']['sales']}",
        f"Monto total: {money(report['totals']['amount'])}",
        "",
    ]
    for machine in report["machine_sections"]:
        lines.extend([
            f"- {machine['label']}",
            f"  {machine['site'] or 'sitio sin nombre'}",
            f"  {machine['total_sales']} ventas · {money(machine['total_amount'])}",
        ])
        top = machine["product_totals"][:3]
        if top:
            lines.append("  Top productos:")
            for row in top:
                lines.append(f"  - {row['product']}: {money(row['amount'])} · {row['sales']} ventas")
        lines.append("")
    lines.extend([
        "Cada fila del mail HTML representa un día del mes y se divide por producto según % del monto vendido.",
        f"Generado: {report['source'].get('sales_generated_at') or 'sin fecha'}",
    ])
    return "\n".join(lines)


def build_html_body(report: dict, client_label: str, logo_url: str | None, *, interactive: bool = True, dark_mode_css: bool = True) -> str:
    legend_items = []
    for product, color in sorted(report["product_colors"].items(), key=lambda item: item[0].lower()):
        legend_items.append(
            "<span class='bb-legend-item' style='display:inline-flex;align-items:center;gap:6px;margin:0 10px 8px 0;font-size:12px;color:#27414f'>"
            f"<span class='bb-swatch' style='display:inline-block;width:10px;height:10px;border-radius:999px;background:{color};box-shadow:0 0 0 1px rgba(0,79,100,.10)'></span>{escape_html(product)}"
            "</span>"
        )
    machine_cards = []
    for machine in report["machine_sections"]:
        rows = []
        for day_row in machine.get("collapsed_day_rows") or machine["day_rows"]:
            if day_row.get("kind") == "gap":
                start_label = day_row["start_day"][8:10]
                end_label = day_row["end_day"][8:10]
                day_label = start_label if start_label == end_label else f"{start_label}–{end_label}"
                sales_label = "sin ventas"
                if day_row["gap_count"] > 1:
                    sales_label = f"sin ventas · {day_row['gap_count']} días"
                rows.append(
                    "<tr>"
                    f"<td class='bb-day-gap' style='padding:7px 8px;color:#486170;font-size:12px;white-space:nowrap'>{escape_html(day_label)}</td>"
                    "<td style='padding:7px 8px;min-width:260px'><div class='bb-gap-bar' style='height:16px;border-radius:999px;background:linear-gradient(90deg,#edf4f7 0%,#f9fbfc 100%);border:1px dashed rgba(0,79,100,.16)'></div></td>"
                    f"<td class='bb-gap-label' colspan='2' style='padding:7px 8px;color:#7c8f9b;font-size:12px;text-align:right;white-space:nowrap'>{escape_html(sales_label)}</td>"
                    "</tr>"
                )
                continue
            rows.append(
                "<tr>"
                f"<td class='bb-day-val' style='padding:6px 8px;color:#0f2231;font-size:12px;white-space:nowrap'>{escape_html(day_row['day'][8:10])}</td>"
                f"<td style='padding:6px 8px;min-width:260px'>{build_segment_html(day_row['segments'], interactive=interactive)}</td>"
                f"<td class='bb-metric-text' style='padding:6px 8px;color:#486170;font-size:12px;text-align:right;white-space:nowrap'>{money(day_row['total_amount']) if day_row['total_amount'] else '—'}</td>"
                f"<td class='bb-metric-text' style='padding:6px 8px;color:#486170;font-size:12px;text-align:right;white-space:nowrap'>{day_row['total_sales'] if day_row['total_sales'] else '—'}</td>"
                "</tr>"
            )
        top_products = "".join(
            [
                "<tr>"
                f"<td class='bb-product-name' style='padding:4px 0;color:#0f2231;font-size:12px'><span class='bb-swatch' style='display:inline-block;width:10px;height:10px;border-radius:999px;background:{row['color']};box-shadow:0 0 0 1px rgba(0,79,100,.10);margin:0 8px 0 0;vertical-align:middle'></span>{escape_html(row['product'])}</td>"
                f"<td class='bb-metric-text' style='padding:4px 0;color:#486170;font-size:12px;text-align:right'>{money(row['amount'])}</td>"
                f"<td class='bb-metric-text' style='padding:4px 0;color:#486170;font-size:12px;text-align:right'>{row['sales']}</td>"
                "</tr>"
                for row in machine['product_totals'][:6]
            ]
        ) or "<tr><td class='bb-empty' colspan='3' style='padding:4px 0;color:#7c8f9b;font-size:12px'>Sin ventas en el período.</td></tr>"
        machine_cards.append(
            "<div class='bb-card' style='border:1px solid rgba(0,79,100,.10);border-radius:20px;padding:18px 18px 14px;background:#ffffff;margin:0 0 16px 0;box-shadow:0 22px 60px -52px rgba(0,79,100,.22)'>"
            f"<div class='bb-card-title' style='font-size:18px;line-height:1.3;font-weight:700;color:#0f2231'>{escape_html(machine['label'])}</div>"
            f"<div class='bb-card-site' style='font-size:13px;line-height:1.5;color:#486170;margin:4px 0 12px 0'>{escape_html(machine['site'])}</div>"
            "<table role='presentation' cellpadding='0' cellspacing='0' width='100%' style='border-collapse:collapse;margin:0 0 12px 0'><tr>"
            f"<td style='padding:0 16px 0 0'><div class='bb-kicker' style='font-size:12px;color:#486170'>Ventas</div><div class='bb-stat' style='font-size:24px;font-weight:700;color:#0f2231'>{machine['total_sales']}</div></td>"
            f"<td><div class='bb-kicker' style='font-size:12px;color:#486170'>Monto</div><div class='bb-stat' style='font-size:24px;font-weight:700;color:#0f2231'>{money(machine['total_amount'])}</div></td>"
            "</tr></table>"
            "<table class='bb-table' role='presentation' cellpadding='0' cellspacing='0' width='100%' style='border-collapse:collapse;border-spacing:0;background:#f7fafb;border:1px solid rgba(0,79,100,.08);border-radius:14px'>"
            "<tr><th class='bb-kicker' align='left' style='padding:8px;color:#486170;font-size:11px'>Día</th><th class='bb-kicker' align='left' style='padding:8px;color:#486170;font-size:11px'>Mix por producto (% monto)</th><th class='bb-kicker' align='right' style='padding:8px;color:#486170;font-size:11px'>Monto</th><th class='bb-kicker' align='right' style='padding:8px;color:#486170;font-size:11px'>Ventas</th></tr>"
            + "".join(rows)
            + "</table>"
            "<div class='bb-section-title' style='margin:14px 0 8px 0;font-size:12px;font-weight:700;color:#0f2231'>Top productos del mes</div>"
            "<table role='presentation' cellpadding='0' cellspacing='0' width='100%' style='border-collapse:collapse'>"
            + top_products
            + "</table>"
            "</div>"
        )

    logo_html = ""
    if logo_url:
        logo_html = f"<img src='{escape_html(logo_url)}' alt='Ballbox' width='170' style='display:block;width:170px;max-width:100%;height:auto;border:0;margin:0 0 18px 0' />"

    generated_label = report['source'].get('sales_generated_at') or report['source'].get('inventory_generated_at') or 'sin fecha'
    dark_css = ""
    if dark_mode_css:
        dark_css = """
      @media (prefers-color-scheme: dark) {
        .bb-body { background:#07111f !important; color:#ffffff !important; }
        .bb-shell { background:rgba(15,20,15,.86) !important; border-color:rgba(255,255,255,.10) !important; box-shadow:0 24px 70px -48px rgba(176,217,0,.18) !important; }
        .bb-card { background:rgba(15,20,15,.80) !important; border-color:rgba(255,255,255,.10) !important; box-shadow:0 24px 70px -48px rgba(0,0,0,.65) !important; }
        .bb-title, .bb-card-title, .bb-stat, .bb-section-title, .bb-day-val, .bb-product-name { color:#ffffff !important; }
        .bb-subtitle, .bb-card-site, .bb-kicker, .bb-metric-text, .bb-day-gap, .bb-foot { color:rgba(255,255,255,.62) !important; }
        .bb-gap-label, .bb-empty, .bb-empty-cell { color:rgba(255,255,255,.42) !important; }
        .bb-legend-item { color:rgba(255,255,255,.82) !important; }
        .bb-swatch { box-shadow:0 0 0 1px rgba(255,255,255,.08) !important; }
        .bb-gap-bar { background:linear-gradient(90deg,#101410 0%,#0b0f0b 100%) !important; border-color:rgba(255,255,255,.10) !important; }
        .bb-table { background:rgba(0,0,0,.24) !important; border-color:rgba(255,255,255,.08) !important; }
        .bb-bar { background:#0b0f0b !important; box-shadow:inset 0 0 0 1px rgba(255,255,255,.08) !important; }
      }
      [data-ogsc] .bb-body, [data-ogsb] .bb-body { background:#07111f !important; color:#ffffff !important; }
      [data-ogsc] .bb-shell, [data-ogsb] .bb-shell, [data-ogsc] .bb-card, [data-ogsb] .bb-card, [data-ogsc] .bb-table, [data-ogsb] .bb-table { background:rgba(15,20,15,.80) !important; border-color:rgba(255,255,255,.10) !important; }
      [data-ogsc] .bb-title, [data-ogsb] .bb-title, [data-ogsc] .bb-card-title, [data-ogsb] .bb-card-title, [data-ogsc] .bb-stat, [data-ogsb] .bb-stat, [data-ogsc] .bb-section-title, [data-ogsb] .bb-section-title, [data-ogsc] .bb-day-val, [data-ogsb] .bb-day-val, [data-ogsc] .bb-product-name, [data-ogsb] .bb-product-name { color:#ffffff !important; }
      [data-ogsc] .bb-subtitle, [data-ogsb] .bb-subtitle, [data-ogsc] .bb-card-site, [data-ogsb] .bb-card-site, [data-ogsc] .bb-kicker, [data-ogsb] .bb-kicker, [data-ogsc] .bb-metric-text, [data-ogsb] .bb-metric-text, [data-ogsc] .bb-day-gap, [data-ogsb] .bb-day-gap, [data-ogsc] .bb-foot, [data-ogsb] .bb-foot, [data-ogsc] .bb-gap-label, [data-ogsb] .bb-gap-label, [data-ogsc] .bb-empty, [data-ogsb] .bb-empty { color:rgba(255,255,255,.72) !important; }
      [data-ogsc] .bb-swatch, [data-ogsb] .bb-swatch { box-shadow:0 0 0 1px rgba(255,255,255,.08) !important; }
      [data-ogsc] .bb-gap-bar, [data-ogsb] .bb-gap-bar { background:linear-gradient(90deg,#101410 0%,#0b0f0b 100%) !important; border-color:rgba(255,255,255,.10) !important; }
      [data-ogsc] .bb-bar, [data-ogsb] .bb-bar { background:#0b0f0b !important; box-shadow:inset 0 0 0 1px rgba(255,255,255,.08) !important; }
"""
    tooltip_css = ""
    if interactive:
        tooltip_css = """
      .bb-tip::after { content:''; position:absolute; left:50%; top:100%; transform:translateX(-50%); border:6px solid transparent; border-top-color:#07111f; }
      .bb-seg:hover .bb-tip, .bb-seg:focus .bb-tip { display:block !important; max-height:none !important; overflow:visible !important; opacity:1 !important; visibility:visible !important; transform:translate(-50%,-8px) !important; transition-delay:0s; }
"""
    meta_color_scheme = ""
    if dark_mode_css:
        meta_color_scheme = "<meta name='color-scheme' content='light dark'>\n    <meta name='supported-color-schemes' content='light dark'>"
    root_rule = ":root { color-scheme: light dark; supported-color-schemes: light dark; }" if dark_mode_css else ""
    return f"""
<html>
  <head>
    {meta_color_scheme}
    <style>
      {root_rule}
      .bb-bar {{ position:relative; height:16px; border-radius:999px; background:#dfe9ee; box-shadow:inset 0 0 0 1px rgba(0,79,100,.10); overflow:visible; z-index:1; }}
      .bb-bar-inner {{ height:16px; border-radius:999px; overflow:visible; position:relative; z-index:2; }}
      .bb-seg {{ outline:none; position:relative; z-index:3; }}
      .bb-seg + .bb-seg {{ box-shadow:inset 1px 0 0 rgba(255,255,255,.24); }}
{tooltip_css}{dark_css}    </style>
  </head>
  <body class='bb-body' style='margin:0;padding:24px;background:#eef3f6;font-family:Arial,sans-serif;color:#0f2231'>
    <div style='max-width:960px;margin:0 auto'>
      <div class='bb-shell' style='background:#ffffff;border:1px solid rgba(0,79,100,.12);border-radius:24px;padding:24px 24px 12px 24px;margin:0 0 16px 0;box-shadow:0 24px 70px -52px rgba(176,217,0,.28)'>
        {logo_html}
        <div class='bb-title' style='font-size:28px;line-height:1.2;font-weight:800;color:#0f2231;margin:0 0 8px 0'>Ventas por máquina · {escape_html(client_label)} · {escape_html(report['month'])}</div>
        <div class='bb-subtitle' style='font-size:14px;line-height:1.6;color:#486170;margin:0 0 10px 0'>Cada fila representa un día del mes. La banda se divide por producto según el porcentaje del monto vendido en esa máquina ese día.</div>
        <table role='presentation' cellpadding='0' cellspacing='0' style='border-collapse:collapse;margin:0 0 12px 0'><tr>
          <td style='padding:0 20px 0 0'><div class='bb-kicker' style='font-size:12px;color:#486170'>Ventas totales</div><div class='bb-stat' style='font-size:26px;font-weight:800;color:#0f2231'>{report['totals']['sales']}</div></td>
          <td><div class='bb-kicker' style='font-size:12px;color:#486170'>Monto total</div><div class='bb-stat' style='font-size:26px;font-weight:800;color:#0f2231'>{money(report['totals']['amount'])}</div></td>
        </tr></table>
        <div class='bb-kicker' style='font-size:12px;color:#486170;margin:0 0 6px 0'>Leyenda</div>
        <div style='margin:0 0 4px 0'>{''.join(legend_items) or "<span class='bb-empty' style='font-size:12px;color:#7c8f9b'>Sin productos con ventas.</span>"}</div>
      </div>
      {''.join(machine_cards)}
      <div class='bb-foot' style='font-size:12px;line-height:1.6;color:#486170;padding:0 4px'>Generado desde Beetwallet: {escape_html(generated_label)}.</div>
    </div>
  </body>
</html>
""".strip()


def send_resend_email(api_key: str, *, sender_name: str, sender_email: str, reply_to: str, recipients: list[str], subject: str, text_body: str, html_body: str) -> dict:
    payload = {
        "from": f"{sender_name} <{sender_email}>",
        "to": recipients,
        "subject": subject,
        "text": text_body,
        "html": html_body,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    response = requests.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or send monthly sales report email.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config JSON")
    parser.add_argument("--month", help="Month as YYYY-MM. Default: current month")
    parser.add_argument("--operations-path", help="Override operations JSON path")
    parser.add_argument("--inventory-path", help="Override inventory JSON path")
    parser.add_argument("--mapping-path", help="Override slot mapping JSON path")
    parser.add_argument("--machines-meta-path", help="Override machines metadata JSON path")
    parser.add_argument("--preview-path", help="Write HTML preview to this path")
    parser.add_argument("--dry-run", action="store_true", help="Build report and preview without sending email")
    args = parser.parse_args()

    config = load_config(resolve_path(args.config, DEFAULT_CONFIG_PATH))
    if args.month:
        config["month"] = normalize_text(args.month)
    if args.operations_path:
        config["operations_path"] = resolve_path(args.operations_path, config["operations_path"])
    if args.inventory_path:
        config["inventory_path"] = resolve_path(args.inventory_path, config["inventory_path"])
    if args.mapping_path:
        config["mapping_path"] = resolve_path(args.mapping_path, config["mapping_path"])
    if args.machines_meta_path:
        config["machines_meta_path"] = resolve_path(args.machines_meta_path, config["machines_meta_path"])
    if args.preview_path:
        config["preview_path"] = resolve_path(args.preview_path, config["preview_path"])

    operations_payload = load_json(config["operations_path"], None)
    if not operations_payload:
        print(f"missing operations: {config['operations_path']}")
        return 2
    inventory_payload = load_json(config["inventory_path"], None)
    if not inventory_payload:
        print(f"missing inventory: {config['inventory_path']}")
        return 2
    machines_meta = load_json(config["machines_meta_path"], []) or []
    mapping_payload = load_json(config["mapping_path"], {}) or {}

    year, month, month_key = resolve_month(config["month"], operations_payload)
    report = collect_sales_report(
        operations_payload=operations_payload,
        inventory_payload=inventory_payload,
        mapping_payload=mapping_payload,
        machines_meta=machines_meta,
        target_client=config["client"],
        month_key=f"{year:04d}-{month:02d}",
    )
    subject = build_subject(config["subject_prefix"], config["client_label"], month_key)
    text_body = build_text_body(report, config["client_label"])
    preview_html = build_html_body(report, config["client_label"], config["logo_url"], interactive=True, dark_mode_css=True)
    html_body = build_html_body(report, config["client_label"], config["logo_url"], interactive=False, dark_mode_css=False)
    write_text(config["preview_path"], preview_html)

    summary = {
        "enabled": config["enabled"],
        "dry_run": bool(args.dry_run),
        "month": month_key,
        "subject": subject,
        "preview_path": str(config["preview_path"]),
        "machine_count": len(report["machine_sections"]),
        "total_sales": report["totals"]["sales"],
        "total_amount": report["totals"]["amount"],
        "operations_count": report["source"]["operations_count"],
        "kept_sales_count": report["source"]["kept_sales_count"],
    }

    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if not config["enabled"]:
        print(json.dumps({**summary, "status": "disabled"}, ensure_ascii=False, indent=2))
        return 0
    if not config["recipient_emails"]:
        print("no recipients configured")
        return 2
    if config["provider"] != "resend":
        print(f"unsupported provider: {config['provider']}")
        return 2
    api_key = normalize_text(os.getenv("RESEND_API_KEY"))
    if not api_key:
        print("missing RESEND_API_KEY")
        return 2
    send_result = send_resend_email(
        api_key,
        sender_name=config["sender_name"],
        sender_email=config["sender_email"],
        reply_to=config["reply_to"],
        recipients=config["recipient_emails"],
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )
    print(json.dumps({**summary, "status": "sent", "send_result": send_result, "sent_at": now_iso()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
