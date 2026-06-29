#!/usr/bin/env python3
import argparse
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "stock_alerts.json"
DEFAULT_INVENTORY_PATH = ROOT / "site" / "ballbox" / "data" / "inventory.json"
DEFAULT_MAPPING_PATH = ROOT / "config" / "adidas_slot_products.json"
DEFAULT_STATE_PATH = Path("/home/sebas/runtime/ballbox/state/adidas_low_stock_alert_state.json")
DEFAULT_SECRET_ENV_PATHS = [
    Path("/home/sebas/runtime/secrets/resend.env"),
    Path("/home/sebas/runtime/secrets/ballbox-alerts.env"),
    ROOT / ".env",
]
DEFAULT_LOGO_URL = "https://ballbox.app/images/ballbox-logo-full.png"
RESEND_API_URL = "https://api.resend.com/emails"
TIMEOUT = 30


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def resolve_path(value: str | None, default_path: Path) -> Path:
    if not value:
        return default_path
    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT / path


def parse_number(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", ".")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_client(value: Any) -> str:
    return " ".join(normalize_text(value).lower().split())


def slot_sort_key(value: Any) -> tuple[int, str]:
    text = normalize_text(value)
    try:
        return (0, f"{int(text):08d}")
    except ValueError:
        return (1, text)


def money(value: Any) -> str:
    amount = parse_number(value)
    return f"${amount:,.0f}".replace(",", ".")


def low_stock_key(machine_id: str, slot_no: str) -> str:
    return f"{machine_id}:{slot_no}"


def is_processable_machine(machine: dict[str, Any]) -> bool:
    status = normalize_text(machine.get("inventory_status"))
    if status and status != "real":
        return False
    if machine.get("fetch_error"):
        return False
    slots = machine.get("slots") or []
    return isinstance(slots, list)


def resolve_product_label(slot: dict[str, Any], mapping: dict[str, Any]) -> str:
    name = normalize_text(slot.get("name"))
    if name:
        return name
    machine_id = normalize_text(slot.get("machine_id"))
    slot_no = normalize_text(slot.get("slot_no"))
    mapped = normalize_text((((mapping.get("machines") or {}).get(machine_id) or {}).get(slot_no)))
    if mapped:
        return mapped
    return f"Slot {slot_no}"


def load_config(path: Path) -> dict[str, Any]:
    config = load_json(path, {}) or {}
    client = normalize_text(config.get("client") or "adidas") or "adidas"
    client_label = normalize_text(config.get("client_label") or client.title()) or client.title()
    return {
        "enabled": bool(config.get("enabled", False)),
        "client": client,
        "client_label": client_label,
        "threshold": int(config.get("threshold", 2)),
        "provider": normalize_text(config.get("provider") or "resend") or "resend",
        "sender_name": normalize_text(config.get("sender_name") or "Ballbox"),
        "sender_email": normalize_text(config.get("sender_email") or "communications@ballbox.app"),
        "reply_to": normalize_text(config.get("reply_to") or ""),
        "recipient_emails": [normalize_text(item) for item in config.get("recipient_emails") or [] if normalize_text(item)],
        "inventory_path": resolve_path(config.get("inventory_path"), DEFAULT_INVENTORY_PATH),
        "mapping_path": resolve_path(config.get("mapping_path"), DEFAULT_MAPPING_PATH),
        "state_path": resolve_path(config.get("state_path"), DEFAULT_STATE_PATH),
        "subject_prefix": normalize_text(config.get("subject_prefix") or "Ballbox"),
        "logo_url": normalize_text(config.get("logo_url") or DEFAULT_LOGO_URL),
    }


def collect_low_slots(inventory: dict[str, Any], mapping: dict[str, Any], target_client: str, threshold: int) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    machines = ((inventory.get("data") or {}).get("machines") or []) if inventory else []
    client_norm = normalize_client(target_client)
    low_rows: list[dict[str, Any]] = []
    processable_machine_ids: set[str] = set()
    current_low_keys: set[str] = set()

    for machine in machines:
        machine_client = normalize_client(machine.get("client"))
        if client_norm and client_norm not in machine_client:
            continue
        machine_id = normalize_text(machine.get("machine_id"))
        if not machine_id or not is_processable_machine(machine):
            continue
        processable_machine_ids.add(machine_id)
        slots = machine.get("slots") or []
        for slot in slots:
            quantity = int(parse_number(slot.get("quantity")))
            if quantity > threshold:
                continue
            slot_no = normalize_text(slot.get("slot_no"))
            if not slot_no:
                continue
            capacity = int(parse_number(slot.get("capacity")))
            row = {
                "key": low_stock_key(machine_id, slot_no),
                "machine_id": machine_id,
                "machine_label": normalize_text(machine.get("label")) or machine_id,
                "site": normalize_text(machine.get("site")),
                "slot_no": slot_no,
                "product_label": resolve_product_label(slot, mapping),
                "quantity": quantity,
                "capacity": capacity,
                "price": normalize_text(slot.get("price")),
                "source_generated_at": inventory.get("generated_at"),
            }
            low_rows.append(row)
            current_low_keys.add(row["key"])

    low_rows.sort(key=lambda row: (normalize_text(row["machine_label"]), slot_sort_key(row["slot_no"])))
    return low_rows, current_low_keys, processable_machine_ids


def build_subject(prefix: str, client_label: str, count: int) -> str:
    noun = "alerta" if count == 1 else "alertas"
    return f"{prefix} · {client_label}: {count} {noun} de reposición"


def build_text_body(alerts: list[dict[str, Any]], threshold: int, generated_at: str | None, client_label: str) -> str:
    lines = [
        f"Hola,",
        "",
        f"Vimos algunos productos con poco stock en {client_label}.",
        "",
        "Para revisar:",
    ]
    for row in alerts:
        price = money(row["price"]) if row.get("price") else "sin precio"
        capacity = f" de {row['capacity']}" if row.get("capacity") else ""
        lines.extend(
            [
                f"- {row['product_label']}",
                f"  {row['machine_label']} · {row['site'] or 'sitio sin nombre'}",
                f"  Posición {row['slot_no']} · quedan {row['quantity']}{capacity} · precio {price}",
            ]
        )
    footer = [
        "",
        "— Equipo Ballbox",
        "",
        f"Nota: este aviso se dispara cuando quedan {threshold} unidades o menos.",
        "No se vuelve a avisar hasta que esa posición se repone y vuelve a bajar.",
    ]
    if generated_at:
        footer.append(f"Snapshot: {generated_at}")
    lines.extend(footer)
    return "\n".join(lines)


def build_html_body(alerts: list[dict[str, Any]], threshold: int, generated_at: str | None, client_label: str, logo_url: str | None) -> str:
    count = len(alerts)
    title = "Hay un producto para reponer" if count == 1 else f"Hay {count} productos para reponer"
    subtitle = f"Detectamos posiciones con poco stock en {client_label}."
    items = []
    for row in alerts:
        price = money(row["price"]) if row.get("price") else "sin precio"
        capacity = f" de {row['capacity']}" if row.get("capacity") else ""
        items.append(
            "<tr><td style='padding:0 0 14px 0'>"
            "<div style='border:1px solid #e9ecf1;border-radius:18px;padding:18px 18px 16px;background:#ffffff'>"
            f"<div style='font-size:18px;line-height:1.3;font-weight:700;color:#111827;margin:0 0 8px 0'>{escape(row['product_label'])}</div>"
            f"<div style='font-size:14px;line-height:1.5;color:#475467;margin:0 0 10px 0'>{escape(row['machine_label'])} · {escape(row['site'] or 'sitio sin nombre')}</div>"
            f"<div style='font-size:14px;line-height:1.5;color:#111827'><strong>Posición {escape(row['slot_no'])}</strong> · quedan <strong>{row['quantity']}{escape(capacity)}</strong> · precio {escape(price)}</div>"
            "</div>"
            "</td></tr>"
        )
    logo_html = ""
    if logo_url:
        logo_html = (
            f"<img src='{escape(logo_url)}' alt='Ballbox' width='170' style='display:block;width:170px;max-width:100%;height:auto;border:0;margin:0 0 18px 0' />"
        )
    note_parts = [
        f"Este aviso se dispara cuando quedan {threshold} unidades o menos.",
        "No vuelve a avisar hasta que esa posición se repone y vuelve a bajar.",
    ]
    if generated_at:
        note_parts.append(f"Snapshot: {generated_at}")
    note_html = "<br />".join(escape(part) for part in note_parts)
    return f"""
<html>
  <head>
    <meta name="color-scheme" content="light dark" />
    <meta name="supported-color-schemes" content="light dark" />
    <style>
      u + .body .gmail-blend-screen {{ background:#000; mix-blend-mode:screen; }}
      u + .body .gmail-blend-difference {{ background:#000; mix-blend-mode:difference; }}
    </style>
  </head>
  <body style="margin:0;padding:0;background:#f5f7fb;color:#111827;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
    <u></u>
    <div class="body" style="margin:0;padding:0;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f5f7fb;padding:28px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#0f140f;border-radius:28px;overflow:hidden;">
            <tr>
              <td style="padding:32px 28px 10px 28px;background:radial-gradient(circle at top left, rgba(196,214,0,0.24), rgba(15,20,15,0) 40%), linear-gradient(180deg, #111711 0%, #0f140f 100%);">
                {logo_html}
                <div class="gmail-blend-screen">
                  <div class="gmail-blend-difference">
                    <div style="display:inline-block;padding:7px 12px;border-radius:999px;background:rgba(196,214,0,0.16);border:1px solid rgba(196,214,0,0.25);color:#d7f53b;font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;-webkit-text-fill-color:#d7f53b;">Alerta de reposición</div>
                    <h1 style="margin:18px 0 10px 0;font-size:32px;line-height:1.1;color:#ffffff;-webkit-text-fill-color:#ffffff;">{escape(title)}</h1>
                    <p style="margin:0 0 26px 0;font-size:16px;line-height:1.6;color:rgba(255,255,255,0.78);-webkit-text-fill-color:rgba(255,255,255,0.78);">{escape(subtitle)}</p>
                  </div>
                </div>
              </td>
            </tr>
            <tr>
              <td style="padding:0 28px 30px 28px;background:#f5f7fb;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:-4px;">
                  <tr><td style="padding:24px 0 16px 0;font-size:15px;line-height:1.6;color:#344054;">Hola, te dejamos el detalle para revisar:</td></tr>
                  {''.join(items)}
                  <tr>
                    <td style="padding:0;font-size:12px;line-height:1.7;color:#667085;">
                      {note_html}
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:20px 0 0 0;font-size:13px;line-height:1.6;color:#344054;">— Equipo Ballbox</td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
    </div>
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


def apply_state_changes(previous_state: dict[str, Any], *, current_alerts: list[dict[str, Any]], current_low_keys: set[str], processable_machine_ids: set[str], newly_low_keys: set[str], sent_at: str | None) -> dict[str, Any]:
    next_state = deepcopy(previous_state or {})
    alerts_map = dict(next_state.get("alerts") or {})

    current_by_key = {row["key"]: row for row in current_alerts}
    active_keys = {
        key
        for key, entry in alerts_map.items()
        if entry.get("active") and normalize_text(entry.get("machine_id")) in processable_machine_ids
    }

    for key in sorted(active_keys - current_low_keys):
        entry = dict(alerts_map.get(key) or {})
        entry["active"] = False
        entry["last_seen_quantity"] = None
        entry["last_product_label"] = entry.get("last_product_label")
        entry["last_recovered_at"] = sent_at or now_iso()
        alerts_map[key] = entry

    for key, row in current_by_key.items():
        entry = dict(alerts_map.get(key) or {})
        entry.update(
            {
                "machine_id": row["machine_id"],
                "machine_label": row["machine_label"],
                "slot_no": row["slot_no"],
                "last_product_label": row["product_label"],
                "last_seen_quantity": row["quantity"],
                "last_seen_capacity": row["capacity"],
                "active": True,
            }
        )
        if sent_at and key in newly_low_keys:
            entry["last_alert_sent_at"] = sent_at
        alerts_map[key] = entry

    next_state["version"] = 1
    next_state["updated_at"] = sent_at or now_iso()
    next_state["alerts"] = alerts_map
    return next_state


for env_path in DEFAULT_SECRET_ENV_PATHS:
    load_env_file(env_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send deduplicated Adidas low-stock alerts from local inventory JSON.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to stock alert config JSON")
    parser.add_argument("--inventory-path", help="Override inventory JSON path")
    parser.add_argument("--mapping-path", help="Override slot mapping JSON path")
    parser.add_argument("--state-path", help="Override dedupe state JSON path")
    parser.add_argument("--dry-run", action="store_true", help="Compute alerts and print summary without sending mail or writing state")
    args = parser.parse_args()

    config = load_config(resolve_path(args.config, DEFAULT_CONFIG_PATH))
    if args.inventory_path:
        config["inventory_path"] = resolve_path(args.inventory_path, config["inventory_path"])
    if args.mapping_path:
        config["mapping_path"] = resolve_path(args.mapping_path, config["mapping_path"])
    if args.state_path:
        config["state_path"] = resolve_path(args.state_path, config["state_path"])

    inventory = load_json(config["inventory_path"], None)
    if not inventory:
        print(f"missing inventory: {config['inventory_path']}", file=sys.stderr)
        return 2
    if inventory.get("ok") is False or inventory.get("stale"):
        print("inventory snapshot not usable for alerts", file=sys.stderr)
        return 2

    mapping = load_json(config["mapping_path"], {"machines": {}}) or {"machines": {}}
    state = load_json(config["state_path"], {"version": 1, "alerts": {}}) or {"version": 1, "alerts": {}}

    low_rows, current_low_keys, processable_machine_ids = collect_low_slots(
        inventory,
        mapping,
        config["client"],
        config["threshold"],
    )

    previous_alerts = state.get("alerts") or {}
    newly_low = [row for row in low_rows if not (previous_alerts.get(row["key"]) or {}).get("active")]
    active_processable_keys = {
        key
        for key, entry in previous_alerts.items()
        if entry.get("active") and normalize_text(entry.get("machine_id")) in processable_machine_ids
    }
    recovered_keys = sorted(active_processable_keys - current_low_keys)

    summary = {
        "enabled": config["enabled"],
        "dry_run": bool(args.dry_run),
        "threshold": config["threshold"],
        "inventory_path": str(config["inventory_path"]),
        "mapping_path": str(config["mapping_path"]),
        "state_path": str(config["state_path"]),
        "current_low_count": len(low_rows),
        "new_low_count": len(newly_low),
        "recovered_count": len(recovered_keys),
        "new_alerts": newly_low,
        "recovered_keys": recovered_keys,
    }

    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if not config["enabled"]:
        print(json.dumps({**summary, "status": "disabled"}, ensure_ascii=False, indent=2))
        return 0

    if not config["recipient_emails"]:
        print("no recipients configured", file=sys.stderr)
        return 2

    send_result = None
    sent_at = None
    if newly_low:
        if config["provider"] != "resend":
            print(f"unsupported provider: {config['provider']}", file=sys.stderr)
            return 2
        api_key = normalize_text(os.getenv("RESEND_API_KEY"))
        if not api_key:
            print("missing RESEND_API_KEY", file=sys.stderr)
            return 2
        generated_at = inventory.get("generated_at")
        subject = build_subject(config["subject_prefix"], config["client_label"], len(newly_low))
        text_body = build_text_body(newly_low, config["threshold"], generated_at, config["client_label"])
        html_body = build_html_body(newly_low, config["threshold"], generated_at, config["client_label"], config["logo_url"])
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
        sent_at = now_iso()

    next_state = apply_state_changes(
        state,
        current_alerts=low_rows,
        current_low_keys=current_low_keys,
        processable_machine_ids=processable_machine_ids,
        newly_low_keys={row["key"] for row in newly_low},
        sent_at=sent_at,
    )
    write_json(config["state_path"], next_state)

    print(
        json.dumps(
            {
                **summary,
                "status": "sent" if newly_low else "no_new_alerts",
                "provider": config["provider"],
                "send_result": send_result,
                "sent_at": sent_at,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
