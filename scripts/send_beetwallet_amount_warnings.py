#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPERATIONS_PATH = ROOT / "data" / "beetwallet_operations.json"
DEFAULT_STATIONS_PATH = ROOT / "config" / "beetwallet_stations.json"
DEFAULT_SLOTS_PATH = ROOT / "config" / "beetwallet_slots.json"
DEFAULT_STATE_PATH = Path("/home/sebas/runtime/ballbox/state/beetwallet_non_round_amount_alert_state.json")
TELEGRAM_NOTIFY = Path("/home/sebas/.agents/skills/telegram-notify/telegram-notify")
EPSILON = Decimal("0.000001")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def resolve_path(value: str | None, default: Path) -> Path:
    if not value:
        return default
    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT / path


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def parse_amount(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    text = str(value).strip().replace(",", ".")
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def amount_is_multiple_of_1000(amount: Decimal) -> bool:
    remainder = amount % Decimal("1000")
    return remainder <= EPSILON or abs(remainder - Decimal("1000")) <= EPSILON


def pick_station_id(op: dict[str, Any]) -> str:
    estacion = op.get("estacion")
    if isinstance(estacion, dict):
        for key in ("_id", "id", "uid"):
            if estacion.get(key):
                return str(estacion[key])
    for key in ("estacionId", "stationId"):
        if op.get(key):
            return str(op[key])
    return "unknown"


def pick_station_name(op: dict[str, Any], station_names: dict[str, str]) -> str:
    station_id = pick_station_id(op)
    mapped = normalize_text(station_names.get(station_id))
    if mapped:
        return mapped
    estacion = op.get("estacion")
    if isinstance(estacion, dict):
        for key in ("nombre", "uid"):
            if estacion.get(key):
                return str(estacion[key])
    return station_id


def pick_slot(op: dict[str, Any]) -> str:
    venta = op.get("venta") or {}
    value = venta.get("seleccion")
    return str(value) if value not in (None, "") else "unknown"


def pick_slot_label(op: dict[str, Any], slot_names: dict[str, dict[str, str]]) -> str:
    station_id = pick_station_id(op)
    slot = pick_slot(op)
    mapped = normalize_text((slot_names.get(station_id) or {}).get(slot))
    return mapped or f"Slot {slot}"


def pick_timestamp(op: dict[str, Any]) -> str:
    for key in ("fecha_finalizado", "fecha", "createdAt", "updatedAt", "date"):
        value = normalize_text(op.get(key))
        if value:
            return value
    venta = op.get("venta") or {}
    for key in ("approved", "created", "fecha", "createdAt"):
        value = normalize_text(venta.get(key))
        if value:
            return value
    return "sin_fecha"


def pick_key(op: dict[str, Any]) -> str:
    for key in ("_id", "transactionId", "secuencia"):
        value = op.get(key)
        if value not in (None, ""):
            return str(value)
    return "|".join([
        pick_station_id(op),
        pick_slot(op),
        pick_timestamp(op),
        str(op.get("monto")),
    ])


def collect_anomalies(operations: list[dict[str, Any]], station_names: dict[str, str], slot_names: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for op in operations:
        if normalize_text(op.get("tipo")) not in ("", "venta"):
            continue
        amount = parse_amount(op.get("monto"))
        if amount_is_multiple_of_1000(amount):
            continue
        rows.append({
            "key": pick_key(op),
            "operation_id": normalize_text(op.get("_id")) or None,
            "transaction_id": normalize_text(op.get("transactionId")) or None,
            "timestamp": pick_timestamp(op),
            "station_id": pick_station_id(op),
            "station_name": pick_station_name(op, station_names),
            "slot": pick_slot(op),
            "slot_label": pick_slot_label(op, slot_names),
            "amount": str(amount.normalize()),
            "raw_status": normalize_text(op.get("status")),
        })
    rows.sort(key=lambda row: (row["timestamp"], row["station_name"], row["slot"], row["key"]))
    return rows


def build_message(new_rows: list[dict[str, Any]], total_count: int, generated_at: str | None) -> str:
    head = f"Ballbox warning: {len(new_rows)} compra/s nueva/s con monto no múltiplo de 1000"
    lines = [head]
    for row in new_rows[:5]:
        lines.append(
            f"- {row['station_name']} · {row['slot_label']} · ${row['amount']} · {row['timestamp']}"
        )
    if len(new_rows) > 5:
        lines.append(f"- ... {len(new_rows) - 5} más")
    lines.append(f"Total anomalías visibles: {total_count}")
    if generated_at:
        lines.append(f"Snapshot: {generated_at}")
    return "\n".join(lines)


def send_telegram(message: str) -> None:
    subprocess.run([str(TELEGRAM_NOTIFY), message], check=True, stdout=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Telegram warnings for new Beetwallet purchases whose amount is not a multiple of 1000.")
    parser.add_argument("--operations-path", help="Override operations JSON path")
    parser.add_argument("--stations-path", help="Override stations config path")
    parser.add_argument("--slots-path", help="Override slots config path")
    parser.add_argument("--state-path", help="Override state JSON path")
    parser.add_argument("--dry-run", action="store_true", help="Compute anomalies and print summary without sending Telegram or writing state")
    parser.add_argument("--baseline-now", action="store_true", help="Overwrite state with current anomalies and do not send alerts")
    args = parser.parse_args()

    operations_path = resolve_path(args.operations_path, DEFAULT_OPERATIONS_PATH)
    stations_path = resolve_path(args.stations_path, DEFAULT_STATIONS_PATH)
    slots_path = resolve_path(args.slots_path, DEFAULT_SLOTS_PATH)
    state_path = resolve_path(args.state_path, DEFAULT_STATE_PATH)

    payload = load_json(operations_path, None)
    if not payload:
        print(f"missing operations: {operations_path}", file=sys.stderr)
        return 2

    operations = payload.get("operations") or []
    station_names = load_json(stations_path, {}) or {}
    slot_names = load_json(slots_path, {}) or {}
    anomalies = collect_anomalies(operations, station_names, slot_names)
    current_keys = {row["key"] for row in anomalies}
    generated_at = (payload.get("meta") or {}).get("generated_at")

    state_exists = state_path.exists()
    state = load_json(state_path, None) or {}
    known = set(state.get("known_anomaly_keys") or [])

    if args.baseline_now or not state_exists:
        baseline_state = {
            "version": 1,
            "created_at": state.get("created_at") or now_iso(),
            "updated_at": now_iso(),
            "baseline_mode": "manual" if args.baseline_now else "auto_first_run",
            "known_anomaly_keys": sorted(current_keys),
            "last_snapshot_generated_at": generated_at,
            "last_anomaly_count": len(anomalies),
        }
        result = {
            "status": "baseline_only",
            "dry_run": bool(args.dry_run),
            "baseline_mode": baseline_state["baseline_mode"],
            "operations_path": str(operations_path),
            "state_path": str(state_path),
            "anomaly_count": len(anomalies),
            "anomalies": anomalies,
        }
        if args.dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        write_json(state_path, baseline_state)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    new_rows = [row for row in anomalies if row["key"] not in known]
    result = {
        "status": "would_send" if new_rows else "no_new_anomalies",
        "dry_run": bool(args.dry_run),
        "operations_path": str(operations_path),
        "state_path": str(state_path),
        "anomaly_count": len(anomalies),
        "new_anomaly_count": len(new_rows),
        "new_anomalies": new_rows,
    }

    if args.dry_run:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if new_rows:
        send_telegram(build_message(new_rows, len(anomalies), generated_at))

    next_state = {
        "version": 1,
        "created_at": state.get("created_at") or now_iso(),
        "updated_at": now_iso(),
        "baseline_mode": state.get("baseline_mode") or "auto_first_run",
        "known_anomaly_keys": sorted(known | current_keys),
        "last_snapshot_generated_at": generated_at,
        "last_anomaly_count": len(anomalies),
    }
    write_json(state_path, next_state)
    print(json.dumps({**result, "status": "sent" if new_rows else "no_new_anomalies"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
