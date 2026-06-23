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

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "monthly_sales_report.json"
DEFAULT_SECRET_ENV_PATHS = [
    Path("/home/sebas/runtime/secrets/resend.env"),
    Path("/home/sebas/runtime/secrets/ballbox-alerts.env"),
    ROOT / ".env",
]
RESEND_API_URL = "https://api.resend.com/emails"
TIMEOUT = 30

ANOMALY_LABELS = {
    "suspicious_low_amount": "monto sospechosamente bajo",
    "price_mismatch": "precio cobrado distinto al esperado",
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
        "operations_path": ROOT / normalize_text(config.get("operations_path") or "data/beetwallet_operations.json"),
        "mapping_path": ROOT / normalize_text(config.get("mapping_path") or "config/adidas_slot_products.json"),
        "machines_path": ROOT / normalize_text(config.get("machines_path") or "config/machines_metadata.json"),
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
    for row in included_sales:
        machine_key = row["machine_label"]
        by_machine[machine_key]["ventas"] += 1
        by_machine[machine_key]["monto_total"] += row["amount"]
        product_key = row["product"]
        by_product[product_key]["ventas"] += 1
        by_product[product_key]["monto_total"] += row["amount"]
        by_product[product_key]["machines"].add(row["machine_label"])
        by_product[product_key]["price_statuses"].add(row["price_status"])

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
        }
        for product, vals in sorted(by_product.items(), key=lambda item: item[1]["monto_total"], reverse=True)
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

    return {
        "month": month,
        "generated_at": now_iso(),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "totals": totals,
        "by_machine": by_machine_rows,
        "by_product": by_product_rows,
        "excluded_sales": excluded_rows,
        "anomalies": anomaly_rows,
        "price_evidence": price_evidence_rows,
        "machine_notes": machine_notes,
        "sales": sorted(included_sales, key=lambda row: row["timestamp"], reverse=True),
    }


def build_subject(prefix: str, client_label: str, month: str) -> str:
    return f"{prefix} · {client_label}: resumen ventas {month}"


def build_text_body(report: dict[str, Any], client_label: str) -> str:
    totals = report["totals"]
    lines = [
        f"Resumen mensual interno de ventas — {client_label}",
        "",
        f"Período: {report['month']}",
        f"Ventas tomadas: {totals['ventas']}",
        f"Monto total: {money(totals['monto_total'])}",
        f"Clasificadas por producto: {totals['classified_count']} ventas / {money(totals['classified_amount'])}",
        f"Excluidas por test/manual: {totals['excluded_count']}",
        f"Anomalías visibles: {totals['anomaly_count']}",
        "",
        "Por máquina:",
    ]
    for row in report["by_machine"]:
        lines.append(f"- {row['machine_label']}: {row['ventas']} ventas · {money(row['monto_total'])}")
    lines.extend(["", "Por producto:"])
    for row in report["by_product"]:
        status = ", ".join(row["price_statuses"])
        lines.append(f"- {row['product']}: {row['ventas']} ventas · {money(row['monto_total'])} · {status}")
    if report["excluded_sales"]:
        lines.extend(["", "Excluidas:"])
        for row in report["excluded_sales"]:
            lines.append(f"- {row['machine_label']} slot {row['slot']} · {money(row['amount'])} · {row['reason']}")
    if report["anomalies"]:
        lines.extend(["", "Anomalías:"])
        for row in report["anomalies"][:10]:
            extra = f" (esperado {money(float(row['expected_price']))})" if row.get("expected_price") is not None else ""
            lines.append(f"- {row['machine_label']} · {row['product']} · slot {row['slot']} · {money(row['amount'])} · {row.get('kind_label', row['kind'])}{extra}")
    return "\n".join(lines)


def build_html_body(report: dict[str, Any], client_label: str, logo_url: str | None) -> str:
    totals = report["totals"]
    metric_cards = [
        ("Ventas", str(totals["ventas"])),
        ("Monto total", money(totals["monto_total"])),
        ("Excluidas", str(totals["excluded_count"])),
        ("Anomalías", str(totals["anomaly_count"])),
    ]
    cards_html = "".join(
        f"<td style='width:25%;padding:0 6px 12px 6px'><div style='border:1px solid #e9ecf1;border-radius:18px;background:#ffffff;padding:16px'><div style='font-size:12px;color:#667085;text-transform:uppercase;letter-spacing:.08em'>{escape(label)}</div><div style='margin-top:8px;font-size:24px;font-weight:700;color:#111827'>{escape(value)}</div></div></td>"
        for label, value in metric_cards
    )
    machine_rows = "".join(
        f"<tr><td style='padding:10px 0;border-bottom:1px solid #eef2f6;color:#111827'>{escape(row['machine_label'])}</td><td style='padding:10px 0;border-bottom:1px solid #eef2f6;color:#111827;text-align:right'>{row['ventas']}</td><td style='padding:10px 0;border-bottom:1px solid #eef2f6;color:#111827;text-align:right'>{escape(money(row['monto_total']))}</td></tr>"
        for row in report["by_machine"]
    )
    product_rows = "".join(
        f"<tr><td style='padding:10px 0;border-bottom:1px solid #eef2f6;color:#111827'>{escape(row['product'])}</td><td style='padding:10px 0;border-bottom:1px solid #eef2f6;color:#111827;text-align:right'>{row['ventas']}</td><td style='padding:10px 0;border-bottom:1px solid #eef2f6;color:#111827;text-align:right'>{escape(money(row['monto_total']))}</td><td style='padding:10px 0;border-bottom:1px solid #eef2f6;color:#667085'>{escape(', '.join(row['price_statuses']))}</td></tr>"
        for row in report["by_product"]
    )
    excluded_html = ""
    if report["excluded_sales"]:
        excluded_html = "<h3 style='margin:28px 0 10px 0;font-size:18px;color:#111827'>Excluidas</h3><ul style='padding-left:18px;color:#344054'>" + "".join(
            f"<li>{escape(row['machine_label'])} · slot {escape(row['slot'])} · {escape(money(row['amount']))} · {escape(row['reason'])}</li>"
            for row in report["excluded_sales"]
        ) + "</ul>"
    anomalies_html = ""
    if report["anomalies"]:
        anomalies_html = "<h3 style='margin:28px 0 10px 0;font-size:18px;color:#111827'>Anomalías visibles</h3><ul style='padding-left:18px;color:#344054'>" + "".join(
            f"<li>{escape(row['machine_label'])} · {escape(row['product'])} · slot {escape(row['slot'])} · {escape(money(row['amount']))} · {escape(row.get('kind_label', row['kind']))}</li>"
            for row in report["anomalies"][:12]
        ) + "</ul>"
    evidence_html = ""
    if report["price_evidence"]:
        evidence_html = "<h3 style='margin:28px 0 10px 0;font-size:18px;color:#111827'>Evidencia de precio por ventas</h3><ul style='padding-left:18px;color:#344054'>" + "".join(
            f"<li>{escape(row['product'])}: {row['count']} ventas · {escape(money(row['amount_total']))} · máquinas {escape(', '.join(row['machines']))}</li>"
            for row in report["price_evidence"]
        ) + "</ul>"
    logo_html = f"<img src='{escape(logo_url)}' alt='Ballbox' width='170' style='display:block;width:170px;max-width:100%;height:auto;border:0;margin:0 0 18px 0' />" if logo_url else ""
    return f"""
<html>
  <body style="margin:0;padding:0;background:#f5f7fb;color:#111827;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f5f7fb;padding:28px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:720px;background:#0f140f;border-radius:28px;overflow:hidden;">
            <tr>
              <td style="padding:32px 28px 18px 28px;background:radial-gradient(circle at top left, rgba(196,214,0,0.24), rgba(15,20,15,0) 40%), linear-gradient(180deg, #111711 0%, #0f140f 100%);">
                {logo_html}
                <div style="display:inline-block;padding:7px 12px;border-radius:999px;background:rgba(196,214,0,0.16);border:1px solid rgba(196,214,0,0.25);color:#d7f53b;font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;">Resumen mensual interno</div>
                <h1 style="margin:18px 0 10px 0;font-size:32px;line-height:1.1;color:#ffffff;">Ventas clasificadas {escape(client_label)}</h1>
                <p style="margin:0;font-size:16px;line-height:1.6;color:rgba(255,255,255,0.78);">Período {escape(report['month'])}. Beetwallet + mapping manual, con exclusiones explícitas y anomalías visibles.</p>
              </td>
            </tr>
            <tr>
              <td style="padding:0 28px 30px 28px;background:#f5f7fb;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:20px;"><tr>{cards_html}</tr></table>
                <h3 style="margin:18px 0 10px 0;font-size:18px;color:#111827">Por máquina</h3>
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #e9ecf1;border-radius:18px;padding:0 16px;">
                  <tr><th align='left' style='padding:14px 0;border-bottom:1px solid #eef2f6;color:#667085;font-size:12px;text-transform:uppercase'>Máquina</th><th align='right' style='padding:14px 0;border-bottom:1px solid #eef2f6;color:#667085;font-size:12px;text-transform:uppercase'>Ventas</th><th align='right' style='padding:14px 0;border-bottom:1px solid #eef2f6;color:#667085;font-size:12px;text-transform:uppercase'>Monto</th></tr>
                  {machine_rows}
                </table>
                <h3 style="margin:28px 0 10px 0;font-size:18px;color:#111827">Por producto</h3>
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #e9ecf1;border-radius:18px;padding:0 16px;">
                  <tr><th align='left' style='padding:14px 0;border-bottom:1px solid #eef2f6;color:#667085;font-size:12px;text-transform:uppercase'>Producto</th><th align='right' style='padding:14px 0;border-bottom:1px solid #eef2f6;color:#667085;font-size:12px;text-transform:uppercase'>Ventas</th><th align='right' style='padding:14px 0;border-bottom:1px solid #eef2f6;color:#667085;font-size:12px;text-transform:uppercase'>Monto</th><th align='left' style='padding:14px 0;border-bottom:1px solid #eef2f6;color:#667085;font-size:12px;text-transform:uppercase'>Precio</th></tr>
                  {product_rows}
                </table>
                {evidence_html}
                {excluded_html}
                {anomalies_html}
                <p style="margin:28px 0 0 0;font-size:12px;line-height:1.7;color:#667085;">Este mail es interno. No corrige montos cobrados: muestra la verdad de Beetwallet y marca anomalías donde el mapping o el precio esperado todavía no están consolidados.</p>
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
    args = parser.parse_args()

    config = load_config(Path(args.config))
    month = normalize_text(args.month) or config["default_month"]
    report = classify_sales(config, month)

    if args.write_json:
        out = Path(args.write_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(json_safe(report), ensure_ascii=False, indent=2) + "\n")

    if args.dry_run:
        print(json.dumps(json_safe(report), ensure_ascii=False, indent=2))
        return 0

    if not config["enabled"]:
        print(json.dumps(json_safe({"status": "disabled", "month": month, "report": report}), ensure_ascii=False, indent=2))
        return 0

    api_key = normalize_text(os.getenv("RESEND_API_KEY"))
    if not api_key:
        raise SystemExit("missing RESEND_API_KEY")
    subject = build_subject(config["subject_prefix"], config["client_label"], month)
    text_body = build_text_body(report, config["client_label"])
    html_body = build_html_body(report, config["client_label"], config["logo_url"])
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
    print(json.dumps(json_safe({"status": "sent", "month": month, "send_result": send_result, "totals": report["totals"]}), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
