#!/usr/bin/env python3
import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

import requests

from send_sales_report_email import build_segment_html, collect_sales_report

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "monthly_sales_report.json"
DEFAULT_PREVIEW_PATH = Path("/home/sebas/runtime/ballbox/tmp/monthly_sales_report_preview.html")
DEFAULT_SECRET_ENV_PATHS = [
    Path("/home/sebas/runtime/secrets/resend.env"),
    Path("/home/sebas/runtime/secrets/ballbox-alerts.env"),
    ROOT / ".env",
]
RESEND_API_URL = "https://api.resend.com/emails"
TIMEOUT = 30

PALETTE_TEAL = "#004f64"
PALETTE_GOLD = "#ffb800"
PALETTE_LIME = "#b0d900"
PALETTE_MOSS = "#4f8a2d"
PALETTE_TEAL_SOFT = "#2c8799"
PALETTE_GOLD_SOFT = "#ffd054"
PALETTE_PANEL_DARK = "rgba(15,20,15,.80)"

ANOMALY_LABELS = {
    "suspicious_low_amount": "monto sospechosamente bajo",
    "price_mismatch": "precio cobrado distinto al esperado",
}

PRICE_STATUS_LABELS = {
    "matches_expected": "precio validado",
    "differs_from_expected": "precio distinto al esperado",
    "expected_price_pending": "precio pendiente de validar",
    "unclassified": "sin clasificar",
}


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


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def parse_amount(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def money(value: float) -> str:
    return f"${value:,.0f}".replace(",", ".")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted(json_safe(item) for item in value)
    return value


def month_bounds(month: str) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(f"{month}-01T00:00:00+00:00")
    year = start.year + (1 if start.month == 12 else 0)
    next_month = 1 if start.month == 12 else start.month + 1
    end = datetime.fromisoformat(f"{year:04d}-{next_month:02d}-01T00:00:00+00:00")
    return start, end


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def load_config(path: Path) -> dict[str, Any]:
    config = load_json(path, {}) or {}

    def resolve(value: Any, default: str | Path) -> Path:
        raw = normalize_text(value)
        if not raw:
            return Path(default)
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
        return ROOT / candidate

    return {
        "enabled": bool(config.get("enabled", False)),
        "client": normalize_text(config.get("client") or "adidas"),
        "client_label": normalize_text(config.get("client_label") or "Adidas"),
        "sender_name": normalize_text(config.get("sender_name") or "Ballbox"),
        "sender_email": normalize_text(config.get("sender_email") or "communications@ballbox.app"),
        "reply_to": normalize_text(config.get("reply_to") or ""),
        "recipient_emails": [normalize_text(item) for item in config.get("recipient_emails") or [] if normalize_text(item)],
        "subject_prefix": normalize_text(config.get("subject_prefix") or "Ballbox"),
        "logo_url": normalize_text(config.get("logo_url") or ""),
        "operations_path": resolve(config.get("operations_path"), "data/beetwallet_operations.json"),
        "inventory_path": resolve(config.get("inventory_path"), ROOT / "site" / "ballbox" / "data" / "inventory.json"),
        "mapping_path": resolve(config.get("mapping_path"), "config/adidas_slot_products.json"),
        "machines_path": resolve(config.get("machines_path"), "config/machines_metadata.json"),
        "preview_path": resolve(config.get("preview_path"), DEFAULT_PREVIEW_PATH),
        "default_month": normalize_text(config.get("default_month") or datetime.now(timezone.utc).strftime("%Y-%m")),
    }


def build_machine_maps(machines_meta: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_machine_id = {}
    by_station_id = {}
    for row in machines_meta:
        machine_id = normalize_text(row.get("machine_id"))
        station_id = normalize_text(row.get("station_id"))
        if machine_id:
            by_machine_id[machine_id] = row
        if station_id:
            by_station_id[station_id] = row
    return by_machine_id, by_station_id


def build_ignored_index(mapping_config: dict[str, Any], by_machine_id: dict[str, dict[str, Any]]) -> dict[tuple[str, str, int], dict[str, Any]]:
    ignored = {}
    machine_notes = mapping_config.get("machine_notes") or {}
    for machine_id, note in machine_notes.items():
        machine = by_machine_id.get(machine_id) or {}
        station_id = normalize_text(machine.get("station_id"))
        for row in note.get("ignored_sales") or []:
            slot = normalize_text(row.get("slot"))
            amount = parse_amount(row.get("amount"))
            if not station_id or not slot or amount is None:
                continue
            ignored[(station_id, slot, int(round(amount * 100)))] = row
    return ignored


def resolve_operation_fields(op: dict[str, Any]) -> dict[str, Any]:
    venta = op.get("venta") or {}
    machine = venta.get("maquina") or {}
    estacion = op.get("estacion")
    if isinstance(estacion, dict):
        station_id = normalize_text(estacion.get("_id") or estacion.get("id") or estacion.get("uid"))
        station_name = normalize_text(estacion.get("nombre") or estacion.get("uid") or station_id)
    else:
        station_id = normalize_text(estacion or machine.get("_id") or machine.get("id"))
        station_name = normalize_text(machine.get("nombre") or station_id)
    slot = normalize_text(venta.get("seleccion"))
    amount = parse_amount(op.get("monto", op.get("amount")))
    timestamp = None
    for key in ("fecha", "createdAt", "updatedAt", "date"):
        timestamp = parse_dt(op.get(key))
        if timestamp:
            break
    if not timestamp:
        for key in ("fecha", "createdAt"):
            timestamp = parse_dt(venta.get(key))
            if timestamp:
                break
    return {
        "station_id": station_id,
        "station_name": station_name,
        "slot": slot,
        "amount": amount,
        "timestamp": timestamp,
        "op_id": normalize_text(op.get("_id") or op.get("id")),
    }


def classify_sales(config: dict[str, Any], month: str) -> dict[str, Any]:
    mapping_config = load_json(config["mapping_path"], {}) or {}
    machines_meta = load_json(config["machines_path"], []) or []
    operations_payload = load_json(config["operations_path"], {}) or {}
    operations = operations_payload.get("operations") or []
    by_machine_id, by_station_id = build_machine_maps(machines_meta)
    ignored_index = build_ignored_index(mapping_config, by_machine_id)
    price_expectations = mapping_config.get("price_expectations") or {}
    machine_notes = mapping_config.get("machine_notes") or {}
    start, end = month_bounds(month)

    included_sales = []
    excluded_sales = []
    anomalies = []
    price_evidence = defaultdict(lambda: {"machines": set(), "slots": set(), "count": 0, "amount_total": 0.0})

    for op in operations:
        row = resolve_operation_fields(op)
        if row["amount"] is None or row["timestamp"] is None:
            continue
        if row["timestamp"] < start or row["timestamp"] >= end:
            continue
        meta = by_station_id.get(row["station_id"]) or {}
        machine_id = normalize_text(meta.get("machine_id"))
        client = normalize_text(meta.get("client"))
        if config["client"] and client != config["client"]:
            continue
        ignore_key = (row["station_id"], row["slot"], int(round(row["amount"] * 100)))
        if ignore_key in ignored_index:
            excluded_sales.append({
                **row,
                "machine_id": machine_id,
                "machine_label": normalize_text(meta.get("label") or row["station_name"]),
                "reason": normalize_text(ignored_index[ignore_key].get("reason") or "ignored"),
            })
            continue

        product = normalize_text((((mapping_config.get("machines") or {}).get(machine_id) or {}).get(row["slot"])))
        price_info = price_expectations.get(product) or {}
        expected_price = price_info.get("expected_price")
        if isinstance(expected_price, (int, float)):
            if abs(row["amount"] - float(expected_price)) < 0.001:
                price_status = "matches_expected"
                evidence = price_evidence[product]
                evidence["machines"].add(normalize_text(meta.get("label") or row["station_name"]))
                evidence["slots"].add(row["slot"])
                evidence["count"] += 1
                evidence["amount_total"] += row["amount"]
            else:
                price_status = "differs_from_expected"
        elif product:
            price_status = "expected_price_pending"
        else:
            price_status = "unclassified"

        sale = {
            **row,
            "machine_id": machine_id,
            "machine_label": normalize_text(meta.get("label") or row["station_name"]),
            "site": normalize_text(meta.get("site")),
            "product": product or f"Slot {row['slot']}",
            "classification_status": "classified" if product else "unclassified",
            "price_status": price_status,
        }
        included_sales.append(sale)

        if row["amount"] <= 100:
            anomalies.append({**sale, "kind": "suspicious_low_amount", "kind_label": ANOMALY_LABELS["suspicious_low_amount"]})
        elif price_status == "differs_from_expected":
            anomalies.append({
                **sale,
                "kind": "price_mismatch",
                "kind_label": ANOMALY_LABELS["price_mismatch"],
                "expected_price": expected_price,
            })

    totals = {
        "ventas": len(included_sales),
        "monto_total": sum(row["amount"] for row in included_sales),
        "excluded_count": len(excluded_sales),
        "anomaly_count": len(anomalies),
        "classified_count": sum(1 for row in included_sales if row["classification_status"] == "classified"),
    }
    totals["classified_amount"] = sum(row["amount"] for row in included_sales if row["classification_status"] == "classified")

    by_machine = defaultdict(lambda: {"ventas": 0, "monto_total": 0.0})
    by_product = defaultdict(lambda: {"ventas": 0, "monto_total": 0.0, "machines": set(), "price_statuses": set()})
    by_day = defaultdict(lambda: {"ventas": 0, "monto_total": 0.0})
    by_slot = defaultdict(lambda: {"ventas": 0, "monto_total": 0.0, "machine_label": "", "product": "", "slot": ""})
    anomaly_by_machine = defaultdict(int)
    for row in included_sales:
        machine_key = row["machine_label"]
        by_machine[machine_key]["ventas"] += 1
        by_machine[machine_key]["monto_total"] += row["amount"]
        product_key = row["product"]
        by_product[product_key]["ventas"] += 1
        by_product[product_key]["monto_total"] += row["amount"]
        by_product[product_key]["machines"].add(row["machine_label"])
        by_product[product_key]["price_statuses"].add(row["price_status"])
        day_key = row["timestamp"].date().isoformat()
        by_day[day_key]["ventas"] += 1
        by_day[day_key]["monto_total"] += row["amount"]
        slot_key = (row["machine_label"], row["slot"], row["product"])
        slot_row = by_slot[slot_key]
        slot_row["ventas"] += 1
        slot_row["monto_total"] += row["amount"]
        slot_row["machine_label"] = row["machine_label"]
        slot_row["product"] = row["product"]
        slot_row["slot"] = row["slot"]

    by_machine_rows = [
        {"machine_label": label, **vals}
        for label, vals in sorted(by_machine.items(), key=lambda item: item[1]["monto_total"], reverse=True)
    ]
    by_product_rows = [
        {
            "product": product,
            "ventas": vals["ventas"],
            "monto_total": vals["monto_total"],
            "machines": sorted(vals["machines"]),
            "price_statuses": sorted(vals["price_statuses"]),
            "price_status_labels": [PRICE_STATUS_LABELS.get(status, status) for status in sorted(vals["price_statuses"])],
        }
        for product, vals in sorted(by_product.items(), key=lambda item: item[1]["monto_total"], reverse=True)
    ]
    by_day_rows = [
        {"day": day, **vals}
        for day, vals in sorted(by_day.items())
    ]
    by_slot_rows = [
        {
            **row,
            "slot_label": f"{row['machine_label']} · slot {row['slot']} · {row['product']}",
        }
        for row in sorted(by_slot.values(), key=lambda row: row["monto_total"], reverse=True)
    ]
    price_evidence_rows = [
        {
            "product": product,
            "count": vals["count"],
            "amount_total": vals["amount_total"],
            "machines": sorted(vals["machines"]),
            "slots": sorted(vals["slots"], key=lambda x: (len(x), x)),
        }
        for product, vals in sorted(price_evidence.items())
    ]
    anomaly_rows = sorted(anomalies, key=lambda row: row["timestamp"], reverse=True)
    excluded_rows = sorted(excluded_sales, key=lambda row: row["timestamp"], reverse=True)
    for row in anomaly_rows:
        anomaly_by_machine[row["machine_label"]] += 1
    anomaly_by_machine_rows = [
        {"machine_label": label, "anomalies": count}
        for label, count in sorted(anomaly_by_machine.items(), key=lambda item: item[1], reverse=True)
    ]

    return {
        "month": month,
        "generated_at": now_iso(),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "totals": totals,
        "by_machine": by_machine_rows,
        "by_product": by_product_rows,
        "by_day": by_day_rows,
        "top_slots": by_slot_rows[:10],
        "excluded_sales": excluded_rows,
        "anomalies": anomaly_rows,
        "anomaly_by_machine": anomaly_by_machine_rows,
        "price_evidence": price_evidence_rows,
        "machine_notes": machine_notes,
        "sales": sorted(included_sales, key=lambda row: row["timestamp"], reverse=True),
    }


def build_subject(prefix: str, client_label: str, month: str) -> str:
    return f"{prefix} · {client_label}: resumen ventas {month}"


def build_horizontal_chart(rows: list[dict[str, Any]], *, label_key: str, value_key: str, value_formatter, bar_color: str, detail_formatter=None) -> str:
    if not rows:
        return "<p class='bbm-muted' style='margin:0;color:#667085'>Sin datos.</p>"
    max_value = max(float(row[value_key]) for row in rows) or 1.0
    blocks = []
    for row in rows:
        width = max(6.0, (float(row[value_key]) / max_value) * 100.0)
        detail = detail_formatter(row) if detail_formatter else value_formatter(float(row[value_key]))
        blocks.append(
            "<tr><td style='padding:8px 0'>"
            f"<div class='bbm-chart-label' style='font-size:13px;line-height:1.4;color:#111827;margin-bottom:6px'><strong>{escape(str(row[label_key]))}</strong> <span class='bbm-chart-value' style='color:#667085'>· {escape(detail)}</span></div>"
            f"<div class='bbm-track' style='height:10px;border-radius:999px;background:#edf1f7;overflow:hidden'><div style='width:{width:.2f}%;height:10px;border-radius:999px;background:{bar_color}'></div></div>"
            "</td></tr>"
        )
    return "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'>" + "".join(blocks) + "</table>"


def build_vertical_day_chart(rows: list[dict[str, Any]], *, value_key: str, title_formatter, fill_style: str) -> str:
    if not rows:
        return "<p class='bbm-muted' style='margin:0;color:#667085'>Sin datos.</p>"
    max_value = max(float(row[value_key]) for row in rows) or 1.0
    cols = []
    for row in rows:
        height = max(10.0, (float(row[value_key]) / max_value) * 120.0)
        cols.append(
            "<td valign='bottom' style='padding:0 4px;text-align:center'>"
            f"<div style='height:120px;display:flex;align-items:flex-end;justify-content:center'><div title='{escape(title_formatter(row))}' style='width:100%;max-width:28px;height:{height:.2f}px;border-radius:10px 10px 4px 4px;background:{fill_style}'></div></div>"
            f"<div class='bbm-day-label' style='margin-top:8px;font-size:11px;color:#667085'>{escape(row['day'][5:])}</div>"
            "</td>"
        )
    return "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'><tr>" + "".join(cols) + "</tr></table>"


def build_top_summary(report: dict[str, Any]) -> list[tuple[str, str]]:
    top_machine = report['by_machine'][0] if report['by_machine'] else None
    top_product = report['by_product'][0] if report['by_product'] else None
    summary = []
    if top_machine:
        summary.append(("Máquina líder", f"{top_machine['machine_label']} · {money(top_machine['monto_total'])}"))
    if top_product:
        summary.append(("Producto líder", f"{top_product['product']} · {money(top_product['monto_total'])}"))
    summary.append(("Máquinas", str(len(report['by_machine']))))
    summary.append(("Productos", str(len(report['by_product']))))
    return summary


def build_top_story_lines(report: dict[str, Any]) -> list[str]:
    totals = report['totals']
    top_machine = report['by_machine'][0] if report['by_machine'] else None
    top_product = report['by_product'][0] if report['by_product'] else None
    lines = [f"Se registraron {totals['ventas']} ventas por {money(totals['monto_total'])} en el período."]
    if top_machine and totals['monto_total']:
        share = (float(top_machine['monto_total']) / float(totals['monto_total'])) * 100.0
        lines.append(f"{top_machine['machine_label']} lideró el mes con {money(top_machine['monto_total'])} ({share:.0f}% del total).")
    if top_product and totals['monto_total']:
        share = (float(top_product['monto_total']) / float(totals['monto_total'])) * 100.0
        lines.append(f"{top_product['product']} fue el producto más fuerte con {money(top_product['monto_total'])} ({share:.0f}% del total).")
    return lines


def build_text_body(report: dict[str, Any], client_label: str) -> str:
    totals = report["totals"]
    lines = [
        f"Resumen mensual de ventas — {client_label}",
        "",
        f"Período: {report['month']}",
        f"Ventas: {totals['ventas']}",
        f"Monto total: {money(totals['monto_total'])}",
        "",
        "Por máquina:",
    ]
    for row in report["by_machine"]:
        lines.append(f"- {row['machine_label']}: {row['ventas']} ventas · {money(row['monto_total'])}")
    lines.extend(["", "Por producto:"])
    for row in report["by_product"]:
        status = ", ".join(row["price_status_labels"])
        lines.append(f"- {row['product']}: {row['ventas']} ventas · {money(row['monto_total'])} · {status}")
    return "\n".join(lines)


def build_machine_mix_section(machine_report: dict[str, Any], *, interactive: bool = True) -> str:
    legend_items = []
    for product, color in sorted(machine_report["product_colors"].items(), key=lambda item: item[0].lower()):
        legend_items.append(
            "<span class='bbm-legend-item' style='display:inline-flex;align-items:center;gap:6px;margin:0 10px 8px 0;font-size:12px;color:#27414f'>"
            f"<span class='bbm-swatch' style='display:inline-block;width:10px;height:10px;border-radius:999px;background:{color};box-shadow:0 0 0 1px rgba(0,79,100,.10)'></span>{escape(product)}"
            "</span>"
        )

    machine_cards = []
    for machine in machine_report["machine_sections"]:
        rows = []
        for day_row in machine.get("collapsed_day_rows") or machine["day_rows"]:
            if day_row.get("kind") == "gap":
                start_label = day_row["start_day"][8:10]
                end_label = day_row["end_day"][8:10]
                day_label = start_label if start_label == end_label else f"{start_label}–{end_label}"
                sales_label = "sin ventas" if day_row["gap_count"] <= 1 else f"sin ventas · {day_row['gap_count']} días"
                rows.append(
                    "<tr>"
                    f"<td style='padding:7px 8px;color:#486170;font-size:12px;white-space:nowrap'>{escape(day_label)}</td>"
                    "<td style='padding:7px 8px;min-width:260px'><div class='bbm-gap-bar' style='height:16px;border-radius:999px;background:linear-gradient(90deg,#edf4f7 0%,#f9fbfc 100%);border:1px dashed rgba(0,79,100,.16)'></div></td>"
                    f"<td style='padding:7px 8px;color:#7c8f9b;font-size:12px;text-align:right;white-space:nowrap' colspan='2'>{escape(sales_label)}</td>"
                    "</tr>"
                )
                continue
            rows.append(
                "<tr>"
                f"<td style='padding:6px 8px;color:#0f2231;font-size:12px;white-space:nowrap'>{escape(day_row['day'][8:10])}</td>"
                f"<td style='padding:6px 8px;min-width:260px'>{build_segment_html(day_row['segments'], interactive=interactive)}</td>"
                f"<td style='padding:6px 8px;color:#486170;font-size:12px;text-align:right;white-space:nowrap'>{money(day_row['total_amount']) if day_row['total_amount'] else '—'}</td>"
                f"<td style='padding:6px 8px;color:#486170;font-size:12px;text-align:right;white-space:nowrap'>{day_row['total_sales'] if day_row['total_sales'] else '—'}</td>"
                "</tr>"
            )

        top_products = "".join(
            [
                "<tr>"
                f"<td style='padding:4px 0;color:#0f2231;font-size:12px'><span class='bbm-swatch' style='display:inline-block;width:10px;height:10px;border-radius:999px;background:{row['color']};box-shadow:0 0 0 1px rgba(0,79,100,.10);margin:0 8px 0 0;vertical-align:middle'></span>{escape(row['product'])}</td>"
                f"<td style='padding:4px 0;color:#486170;font-size:12px;text-align:right'>{money(row['amount'])}</td>"
                f"<td style='padding:4px 0;color:#486170;font-size:12px;text-align:right'>{row['sales']}</td>"
                "</tr>"
                for row in machine['product_totals'][:6]
            ]
        ) or "<tr><td colspan='3' style='padding:4px 0;color:#7c8f9b;font-size:12px'>Sin ventas en el período.</td></tr>"

        machine_cards.append(
            "<div class='bbm-panel bbm-machine-card' style='border:1px solid rgba(0,79,100,.10);border-radius:20px;padding:18px 18px 14px;background:#ffffff;margin:0 0 16px 0;box-shadow:0 22px 60px -52px rgba(0,79,100,.22)'>"
            f"<div class='bbm-strong' style='font-size:18px;line-height:1.3;font-weight:700;color:#0f2231'>{escape(machine['label'])}</div>"
            f"<div class='bbm-muted' style='font-size:13px;line-height:1.5;color:#486170;margin:4px 0 12px 0'>{escape(machine['site'])}</div>"
            "<table role='presentation' cellpadding='0' cellspacing='0' width='100%' style='border-collapse:collapse;margin:0 0 12px 0'><tr>"
            f"<td style='padding:0 16px 0 0'><div class='bbm-muted' style='font-size:12px;color:#486170'>Ventas</div><div class='bbm-strong' style='font-size:24px;font-weight:700;color:#0f2231'>{machine['total_sales']}</div></td>"
            f"<td><div class='bbm-muted' style='font-size:12px;color:#486170'>Monto</div><div class='bbm-strong' style='font-size:24px;font-weight:700;color:#0f2231'>{money(machine['total_amount'])}</div></td>"
            "</tr></table>"
            "<table class='bbm-panel bbm-table' role='presentation' cellpadding='0' cellspacing='0' width='100%' style='border-collapse:collapse;border-spacing:0;background:#f7fafb;border:1px solid rgba(0,79,100,.08);border-radius:14px'>"
            "<tr><th align='left' style='padding:8px;color:#486170;font-size:11px'>Día</th><th align='left' style='padding:8px;color:#486170;font-size:11px'>Mix por producto (% monto)</th><th align='right' style='padding:8px;color:#486170;font-size:11px'>Monto</th><th align='right' style='padding:8px;color:#486170;font-size:11px'>Ventas</th></tr>"
            + "".join(rows)
            + "</table>"
            "<div class='bbm-strong' style='margin:14px 0 8px 0;font-size:12px;font-weight:700;color:#0f2231'>Top productos del mes</div>"
            "<table role='presentation' cellpadding='0' cellspacing='0' width='100%' style='border-collapse:collapse'>"
            + top_products
            + "</table>"
            "</div>"
        )

    return (
        "<h3 class='bbm-heading' style='margin:28px 0 10px 0;font-size:18px;color:#111827'>Por máquina</h3>"
        "<div class='bbm-muted' style='margin:0 0 10px 0;color:#486170;font-size:14px;line-height:1.6'>"
        "Cada fila representa un día del mes. La banda se divide por producto según el porcentaje del monto vendido en esa máquina ese día."
        "</div>"
        f"<div style='margin:0 0 10px 0'>{''.join(legend_items) or "<span class='bbm-muted' style='font-size:12px;color:#7c8f9b'>Sin productos con ventas.</span>"}</div>"
        + "".join(machine_cards)
    )


def build_html_body(report: dict[str, Any], machine_report: dict[str, Any], client_label: str, logo_url: str | None, *, interactive: bool = True, dark_mode_css: bool = True) -> str:
    totals = report["totals"]
    top_story_lines = build_top_story_lines(report)
    preheader = " · ".join(top_story_lines[:3])
    metric_cards = [
        ("Ventas", str(totals["ventas"])),
        ("Monto total", money(totals["monto_total"])),
    ]
    card_width = 100 / max(len(metric_cards), 1)
    cards_html = "".join(
        f"<td style='width:{card_width:.0f}%;padding:0 6px 12px 6px'><div class='bbm-panel' style='border:1px solid #e9ecf1;border-radius:18px;background:#ffffff;padding:16px'><div class='bbm-muted' style='font-size:12px;color:#667085;text-transform:uppercase;letter-spacing:.08em'>{escape(label)}</div><div class='bbm-strong' style='margin-top:8px;font-size:24px;font-weight:700;color:#111827'>{escape(value)}</div></div></td>"
        for label, value in metric_cards
    )
    top_summary = build_top_summary(report)
    summary_html = "".join(
        f"<td style='padding:0 6px 12px 6px'><div class='bbm-panel' style='border:1px solid #dbe2ea;border-radius:16px;background:#ffffff;padding:14px 16px'><div class='bbm-muted' style='font-size:12px;color:#667085;text-transform:uppercase;letter-spacing:.08em'>{escape(label)}</div><div class='bbm-strong' style='margin-top:6px;font-size:15px;font-weight:700;color:#111827;line-height:1.4'>{escape(value)}</div></div></td>"
        for label, value in top_summary
    )
    top_story_html = "".join(
        f"<li style='margin:0 0 8px 0'>{escape(line)}</li>"
        for line in top_story_lines
    )
    revenue_product_chart = build_horizontal_chart(
        report["by_product"],
        label_key="product",
        value_key="monto_total",
        value_formatter=money,
        detail_formatter=lambda row: f"{money(row['monto_total'])} · {row['ventas']} ventas",
        bar_color=f"linear-gradient(90deg,{PALETTE_GOLD_SOFT} 0%,{PALETTE_GOLD} 100%)",
    )
    machine_mix_section = build_machine_mix_section(machine_report, interactive=interactive)
    logo_html = f"<img src='{escape(logo_url)}' alt='Ballbox' width='170' style='display:block;width:170px;max-width:100%;height:auto;border:0;margin:0 0 18px 0' />" if logo_url else ""
    tooltip_css = ""
    if interactive:
        tooltip_css = """
      .bb-tip::after { content:''; position:absolute; left:50%; top:100%; transform:translateX(-50%); border:6px solid transparent; border-top-color:#07111f; }
      .bb-seg:hover .bb-tip, .bb-seg:focus .bb-tip { display:block !important; max-height:none !important; overflow:visible !important; opacity:1 !important; visibility:visible !important; transform:translate(-50%,-8px) !important; transition-delay:0s; }
"""
    meta_color_scheme = ""
    dark_css = ""
    if dark_mode_css:
        meta_color_scheme = "<meta name='color-scheme' content='light dark'>\n    <meta name='supported-color-schemes' content='light dark'>"
        dark_css = """
      @media (prefers-color-scheme: dark) {
        .bbm-body, .bbm-body-table, .bbm-content { background:#07111f !important; color:#ffffff !important; }
        .bbm-panel, .bbm-machine-card, .bbm-table, .bbm-data-table { background:rgba(15,20,15,.80) !important; border-color:rgba(255,255,255,.10) !important; box-shadow:0 24px 70px -48px rgba(0,0,0,.65) !important; }
        .bbm-heading, .bbm-strong, .bbm-panel .bbm-strong, .bbm-machine-card .bbm-strong { color:#ffffff !important; }
        .bbm-muted, .bbm-chart-label, .bbm-chart-value, .bbm-day-label, .bbm-list, .bbm-panel td, .bbm-panel th, .bbm-machine-card td, .bbm-machine-card th { color:rgba(255,255,255,.72) !important; }
        .bbm-data-table td, .bbm-data-table th, .bbm-table td, .bbm-table th { color:rgba(255,255,255,.72) !important; border-bottom-color:rgba(255,255,255,.08) !important; }
        .bbm-data-table th, .bbm-table th { color:rgba(255,255,255,.56) !important; }
        .bbm-legend-item { color:rgba(255,255,255,.82) !important; }
        .bbm-swatch { box-shadow:0 0 0 1px rgba(255,255,255,.08) !important; }
        .bbm-track { background:rgba(255,255,255,.08) !important; }
        .bbm-gap-bar { background:linear-gradient(90deg,#101410 0%,#0b0f0b 100%) !important; border-color:rgba(255,255,255,.10) !important; }
        .bb-bar { background:#0b0f0b !important; box-shadow:inset 0 0 0 1px rgba(255,255,255,.08) !important; }
      }
      [data-ogsc] .bbm-body, [data-ogsb] .bbm-body, [data-ogsc] .bbm-body-table, [data-ogsb] .bbm-body-table, [data-ogsc] .bbm-content, [data-ogsb] .bbm-content { background:#07111f !important; color:#ffffff !important; }
      [data-ogsc] .bbm-panel, [data-ogsb] .bbm-panel, [data-ogsc] .bbm-machine-card, [data-ogsb] .bbm-machine-card, [data-ogsc] .bbm-table, [data-ogsb] .bbm-table, [data-ogsc] .bbm-data-table, [data-ogsb] .bbm-data-table { background:rgba(15,20,15,.80) !important; border-color:rgba(255,255,255,.10) !important; box-shadow:0 24px 70px -48px rgba(0,0,0,.65) !important; }
      [data-ogsc] .bbm-heading, [data-ogsb] .bbm-heading, [data-ogsc] .bbm-strong, [data-ogsb] .bbm-strong, [data-ogsc] .bbm-panel .bbm-strong, [data-ogsb] .bbm-panel .bbm-strong, [data-ogsc] .bbm-machine-card .bbm-strong, [data-ogsb] .bbm-machine-card .bbm-strong { color:#ffffff !important; }
      [data-ogsc] .bbm-muted, [data-ogsb] .bbm-muted, [data-ogsc] .bbm-chart-label, [data-ogsb] .bbm-chart-label, [data-ogsc] .bbm-chart-value, [data-ogsb] .bbm-chart-value, [data-ogsc] .bbm-day-label, [data-ogsb] .bbm-day-label, [data-ogsc] .bbm-list, [data-ogsb] .bbm-list, [data-ogsc] .bbm-panel td, [data-ogsb] .bbm-panel td, [data-ogsc] .bbm-panel th, [data-ogsb] .bbm-panel th, [data-ogsc] .bbm-machine-card td, [data-ogsb] .bbm-machine-card td, [data-ogsc] .bbm-machine-card th, [data-ogsb] .bbm-machine-card th { color:rgba(255,255,255,.72) !important; }
      [data-ogsc] .bbm-data-table td, [data-ogsb] .bbm-data-table td, [data-ogsc] .bbm-data-table th, [data-ogsb] .bbm-data-table th, [data-ogsc] .bbm-table td, [data-ogsb] .bbm-table td, [data-ogsc] .bbm-table th, [data-ogsb] .bbm-table th { color:rgba(255,255,255,.72) !important; border-bottom-color:rgba(255,255,255,.08) !important; }
      [data-ogsc] .bbm-data-table th, [data-ogsb] .bbm-data-table th, [data-ogsc] .bbm-table th, [data-ogsb] .bbm-table th { color:rgba(255,255,255,.56) !important; }
      [data-ogsc] .bbm-legend-item, [data-ogsb] .bbm-legend-item { color:rgba(255,255,255,.82) !important; }
      [data-ogsc] .bbm-swatch, [data-ogsb] .bbm-swatch { box-shadow:0 0 0 1px rgba(255,255,255,.08) !important; }
      [data-ogsc] .bbm-track, [data-ogsb] .bbm-track { background:rgba(255,255,255,.08) !important; }
      [data-ogsc] .bbm-gap-bar, [data-ogsb] .bbm-gap-bar { background:linear-gradient(90deg,#101410 0%,#0b0f0b 100%) !important; border-color:rgba(255,255,255,.10) !important; }
      [data-ogsc] .bb-bar, [data-ogsb] .bb-bar { background:#0b0f0b !important; box-shadow:inset 0 0 0 1px rgba(255,255,255,.08) !important; }
"""
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
  <body class="bbm-body" style="margin:0;padding:0;background:#f5f7fb;color:#111827;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
    <div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all;">{escape(preheader)}</div>
    <table class="bbm-body-table" role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f5f7fb;padding:28px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:720px;background:#0f140f;border-radius:28px;overflow:hidden;">
            <tr>
              <td style="padding:32px 28px 18px 28px;background:radial-gradient(circle at top left, rgba(196,214,0,0.24), rgba(15,20,15,0) 40%), linear-gradient(180deg, #111711 0%, #0f140f 100%);">
                {logo_html}
                <div style="display:inline-block;padding:7px 12px;border-radius:999px;background:rgba(196,214,0,0.16);border:1px solid rgba(196,214,0,0.25);color:#d7f53b;font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;">Resumen mensual</div>
                <h1 style="margin:18px 0 10px 0;font-size:32px;line-height:1.1;color:#ffffff;">Resumen de ventas {escape(client_label)}</h1>
                <p style="margin:0;font-size:16px;line-height:1.6;color:rgba(255,255,255,0.78);">Período {escape(report['month'])}</p>
              </td>
            </tr>
            <tr>
              <td class="bbm-content" style="padding:0 28px 30px 28px;background:#f5f7fb;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:20px;"><tr>{cards_html}</tr></table>
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:6px;"><tr>{summary_html}</tr></table>
                <div class="bbm-panel" style="margin-top:8px;background:#ffffff;border:1px solid #e9ecf1;border-radius:18px;padding:18px 20px;">
                  <h3 class="bbm-heading" style="margin:0 0 10px 0;font-size:18px;color:#111827">Info clave</h3>
                  <ul class="bbm-list" style="margin:0;padding-left:20px;color:#344054;font-size:14px;line-height:1.6;">{top_story_html}</ul>
                </div>
                {machine_mix_section}
                <h3 class="bbm-heading" style="margin:28px 0 10px 0;font-size:18px;color:#111827">Monto por producto</h3>
                <div class="bbm-panel" style="background:#ffffff;border:1px solid #e9ecf1;border-radius:18px;padding:18px;">{revenue_product_chart}</div>
                <p class="bbm-muted" style="margin:28px 0 0 0;font-size:12px;line-height:1.7;color:#667085;">Reporte generado desde las ventas registradas del período.</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
""".strip()


def send_resend_email(api_key: str, *, sender_name: str, sender_email: str, reply_to: str, recipients: list[str], subject: str, text_body: str, html_body: str) -> dict[str, Any]:
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
    parser = argparse.ArgumentParser(description="Send internal monthly Adidas sales report.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--month", help="YYYY-MM")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write-json", help="Write report JSON to path")
    parser.add_argument("--preview-path", help="Write HTML preview to this path")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    if args.preview_path:
        config["preview_path"] = Path(args.preview_path)
    month = normalize_text(args.month) or config["default_month"]
    report = classify_sales(config, month)

    operations_payload = load_json(config["operations_path"], {}) or {}
    inventory_payload = load_json(config["inventory_path"], {}) or {}
    mapping_payload = load_json(config["mapping_path"], {}) or {}
    machines_meta = load_json(config["machines_path"], []) or []
    machine_report = collect_sales_report(
        operations_payload=operations_payload,
        inventory_payload=inventory_payload,
        mapping_payload=mapping_payload,
        machines_meta=machines_meta,
        target_client=config["client"],
        month_key=month,
    )

    if args.write_json:
        out = Path(args.write_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(json_safe(report), ensure_ascii=False, indent=2) + "\n")

    subject = build_subject(config["subject_prefix"], config["client_label"], month)
    text_body = build_text_body(report, config["client_label"])
    preview_html = build_html_body(report, machine_report, config["client_label"], config["logo_url"], interactive=True, dark_mode_css=True)
    html_body = build_html_body(report, machine_report, config["client_label"], config["logo_url"], interactive=False, dark_mode_css=False)
    write_text(config["preview_path"], preview_html)

    summary = {
        "status": "preview_built" if args.dry_run else ("disabled" if not config["enabled"] else "ready"),
        "month": month,
        "subject": subject,
        "preview_path": str(config["preview_path"]),
        "totals": report["totals"],
        "machine_count": len(machine_report["machine_sections"]),
    }

    if args.dry_run:
        print(json.dumps(json_safe(summary), ensure_ascii=False, indent=2))
        return 0

    if not config["enabled"]:
        print(json.dumps(json_safe({**summary, "status": "disabled"}), ensure_ascii=False, indent=2))
        return 0

    api_key = normalize_text(os.getenv("RESEND_API_KEY"))
    if not api_key:
        raise SystemExit("missing RESEND_API_KEY")
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
    print(json.dumps(json_safe({**summary, "status": "sent", "send_result": send_result}), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
