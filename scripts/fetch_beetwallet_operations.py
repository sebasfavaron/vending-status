#!/usr/bin/env python3
import json
import math
import os
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

ALL_TIME_FROM = '2000-01-01'
ALL_TIME_TO = '2100-01-01'

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / '.env'
DATA_DIR = ROOT / 'data'
# BEET_OPERATIONS_OUT writes somewhere else: the 5-minute Ballbox feed fetches only the
# last days and must not replace the all-time file the monthly report reads.
TARGET = Path(os.environ.get('BEET_OPERATIONS_OUT') or DATA_DIR / 'beetwallet_operations.json')


def load_env(path: Path):
    env = {}
    if not path.exists():
        return env
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        env[key.strip()] = value.strip()
    return env


def getenv(name: str, default=None):
    local = ENV.get(name)
    if local not in (None, ''):
        return local
    return os.environ.get(name, default)


def post_json(url: str, payload: dict, headers=None):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Content-Type', 'application/json')
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    # Beetwallet times out intermittently (f-097-adjacent: 15 isolated exit-1 runs/day,
    # every one self-healing on the next 5-min cycle). One retry absorbs that instead of
    # failing the whole feed and paging Sebas for something gone by the next cycle.
    last_err = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except TimeoutError as exc:
            last_err = exc
            if attempt == 0:
                time.sleep(5)
    raise last_err


def accounts():
    """Operator accounts to read: BEET_USERNAME/BEET_PASSWORD, then BEET_USERNAME_2.._9.

    Each club whose own Mercado Pago collects has its own BeetWallet operator account, and an
    account only sees its own stations (Tortuguitas, BW059E1AF4F8, left the Adidas account).
    """
    found = []
    for suffix in [''] + [f'_{n}' for n in range(2, 10)]:
        user = getenv(f'BEET_USERNAME{suffix}')
        password = getenv(f'BEET_PASSWORD{suffix}')
        if user and password:
            found.append((user, password))
    return found


def fetch_account(api: str, user: str, password: str, desde: str, hasta: str):
    login_resp = post_json(f'{api}/login-operadores', {
        'action': 'login-operadores',
        'usuario': user,
        'clave': password,
    })
    token = login_resp.get('token')
    if not token:
        raise RuntimeError(f'No pude obtener token para {user}: {login_resp.get("message") or login_resp.get("error") or "sin token"}')

    def fetch_page(page: int):
        return post_json(
            f'{api}/partners/getOperations',
            {
                'filter': {
                    'desde': desde,
                    'hasta': hasta,
                    'page': page,
                    'success': True,
                },
                'withTotals': True,
            },
            {'Authorization': token},
        )

    first = fetch_page(0)
    total_count = int(first.get('count') or 0)
    pages = max(1, math.ceil(total_count / 10)) if total_count else 1
    operations = list(first.get('data') or [])
    for page in range(1, pages):
        operations.extend((fetch_page(page).get('data') or []))

    user_data = None
    try:
        req = urllib.request.Request(f'{api}/partners/getUserData', method='GET')
        req.add_header('Authorization', token)
        with urllib.request.urlopen(req, timeout=60) as resp:
            user_data = json.loads(resp.read().decode('utf-8'))
    except Exception:
        user_data = None
    return operations, total_count, pages, user_data


def merge_operations(batches):
    """Concatenates every account's operations in API order, once per `_id`."""
    seen = set()
    merged = []
    for operations in batches:
        for op in operations:
            key = op.get('_id')
            if key is not None and key in seen:
                continue
            seen.add(key)
            merged.append(op)
    return merged


def main():
    desde = sys.argv[1] if len(sys.argv) > 1 else getenv('DEFAULT_FROM', ALL_TIME_FROM)
    hasta = sys.argv[2] if len(sys.argv) > 2 else getenv('DEFAULT_TO', ALL_TIME_TO)

    api = getenv('BEET_API_BASE', 'https://api.beetwallet.com').rstrip('/')
    found = accounts()
    if not found or not getenv('BEET_USERNAME'):
        print('Falta BEET_USERNAME o BEET_PASSWORD en .env', file=sys.stderr)
        sys.exit(1)

    batches = []
    total_count = 0
    pages = 0
    user_data = None
    failed = []
    for index, (user, password) in enumerate(found):
        try:
            operations, count, account_pages, account_data = fetch_account(api, user, password, desde, hasta)
        except Exception as exc:
            # The main account failing stops the run, as before. An extra account failing must
            # not hide the main account's sales: it is reported and the run goes on without it.
            if index == 0:
                print(str(exc), file=sys.stderr)
                sys.exit(1)
            print(f'Beetwallet: cuenta extra {user} sin datos: {exc}', file=sys.stderr)
            failed.append(user)
            continue
        batches.append(operations)
        total_count += count
        pages += account_pages
        if index == 0:
            user_data = account_data
    operations = merge_operations(batches)

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    # Write then rename: a reader (report, exporter) never sees a half-written file.
    partial = TARGET.with_name(TARGET.name + '.partial')
    partial.write_text(json.dumps({
        'meta': {
            'desde': desde,
            'hasta': hasta,
            'count': len(operations),
            'source_count': total_count,
            'pages': pages,
            'accounts': len(found) - len(failed),
            'accounts_failed': failed,
            'generated_at': date.today().isoformat(),
        },
        'operations': operations,
        'user_data': user_data,
    }, ensure_ascii=False, indent=2))
    partial.replace(TARGET)
    print(TARGET)


if __name__ == '__main__':
    ENV = load_env(ENV_PATH)
    main()
