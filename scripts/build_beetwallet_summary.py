#!/usr/bin/env python3
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / 'data' / 'beetwallet_operations.json'
STATIONS_PATH = ROOT / 'config' / 'beetwallet_stations.json'
SLOTS_PATH = ROOT / 'config' / 'beetwallet_slots.json'
OUT_JSON = ROOT / 'data' / 'beetwallet_summary.json'


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def parse_dt(value):
    if not value:
        return None
    value = value.replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def pick_station_data(op):
    estacion = op.get('estacion')
    if isinstance(estacion, dict):
        station_id = estacion.get('_id') or estacion.get('id') or estacion.get('uid') or 'unknown'
        station_name = estacion.get('nombre') or estacion.get('uid') or station_id
        return str(station_id), str(station_name)
    if estacion:
        station_id = str(estacion)
        return station_id, station_id
    for key in ('estacionId', 'stationId'):
        value = op.get(key)
        if value:
            value = str(value)
            return value, value
    venta = op.get('venta') or {}
    maquina = venta.get('maquina') or {}
    if isinstance(maquina, dict):
        station_id = maquina.get('_id') or maquina.get('id') or maquina.get('estacion') or 'unknown'
        station_name = maquina.get('nombre') or station_id
        return str(station_id), str(station_name)
    return 'unknown', 'unknown'


def pick_slot(op):
    venta = op.get('venta') or {}
    value = venta.get('seleccion')
    return str(value) if value not in (None, '') else 'unknown'


def pick_amount(op):
    value = op.get('monto')
    if value is None:
        value = op.get('amount', 0)
    try:
        return float(value)
    except Exception:
        return 0.0


def pick_when(op):
    for key in ('fecha', 'createdAt', 'updatedAt', 'date'):
        dt = parse_dt(str(op.get(key))) if op.get(key) else None
        if dt:
            return dt
    venta = op.get('venta') or {}
    for key in ('fecha', 'createdAt'):
        dt = parse_dt(str(venta.get(key))) if venta.get(key) else None
        if dt:
            return dt
    return None


def main():
    payload = load_json(DATA_PATH, None)
    if not payload:
        raise SystemExit(f'No existe {DATA_PATH}')

    station_names = load_json(STATIONS_PATH, {})
    slot_names = load_json(SLOTS_PATH, {})
    operations = payload.get('operations') or []

    by_day = defaultdict(lambda: {'ventas': 0, 'monto_total': 0.0})
    by_station_slot = defaultdict(lambda: {'ventas': 0, 'monto_total': 0.0})
    by_station = defaultdict(lambda: {'ventas': 0, 'monto_total': 0.0})
    normalized = []

    for op in operations:
        station_id, detected_station_name = pick_station_data(op)
        slot = pick_slot(op)
        amount = pick_amount(op)
        when = pick_when(op)
        day = when.date().isoformat() if when else 'sin_fecha'
        station_name = station_names.get(station_id, detected_station_name)
        slot_label = (slot_names.get(station_id) or {}).get(slot, f'Slot {slot}')

        by_day[day]['ventas'] += 1
        by_day[day]['monto_total'] += amount
        by_station[station_id]['ventas'] += 1
        by_station[station_id]['monto_total'] += amount
        by_station_slot[(station_id, slot)]['ventas'] += 1
        by_station_slot[(station_id, slot)]['monto_total'] += amount

        normalized.append({
            'station_id': station_id,
            'station_name': station_name,
            'slot': slot,
            'slot_label': slot_label,
            'amount': amount,
            'day': day,
            'timestamp': when.isoformat() if when else None,
        })

    station_label_by_id = {}
    for item in normalized:
        station_label_by_id.setdefault(item['station_id'], item['station_name'])

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({
        'meta': payload.get('meta') or {},
        'totals': {
            'ventas': len(normalized),
            'monto_total': sum(x['amount'] for x in normalized),
        },
        'by_day': [{'day': day, **vals} for day, vals in sorted(by_day.items())],
        'by_station': [
            {
                'station_id': station_id,
                'station_name': station_label_by_id.get(station_id, station_names.get(station_id, station_id)),
                **vals,
            }
            for station_id, vals in sorted(by_station.items(), key=lambda item: station_label_by_id.get(item[0], station_names.get(item[0], item[0])))
        ],
        'by_station_slot': [
            {
                'station_id': station_id,
                'station_name': station_label_by_id.get(station_id, station_names.get(station_id, station_id)),
                'slot': slot,
                'slot_label': (slot_names.get(station_id) or {}).get(slot, f'Slot {slot}'),
                **vals,
            }
            for (station_id, slot), vals in sorted(by_station_slot.items(), key=lambda item: (station_label_by_id.get(item[0][0], station_names.get(item[0][0], item[0][0])), item[0][1]))
        ],
    }, ensure_ascii=False, indent=2))
    print(OUT_JSON)


if __name__ == '__main__':
    main()
