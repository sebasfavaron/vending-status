#!/usr/bin/env python3
import json
import math
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path

ALL_TIME_FROM = '2000-01-01'
ALL_TIME_TO = '2100-01-01'

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / '.env'
DATA_DIR = ROOT / 'data'
TARGET = DATA_DIR / 'beetwallet_operations.json'


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
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))


def main():
    desde = sys.argv[1] if len(sys.argv) > 1 else getenv('DEFAULT_FROM', ALL_TIME_FROM)
    hasta = sys.argv[2] if len(sys.argv) > 2 else getenv('DEFAULT_TO', ALL_TIME_TO)

    api = getenv('BEET_API_BASE', 'https://api.beetwallet.com').rstrip('/')
    user = getenv('BEET_USERNAME')
    password = getenv('BEET_PASSWORD')

    if not user or not password:
        print('Falta BEET_USERNAME o BEET_PASSWORD en .env', file=sys.stderr)
        sys.exit(1)

    login_resp = post_json(f'{api}/login-operadores', {
        'action': 'login-operadores',
        'usuario': user,
        'clave': password,
    })
    token = login_resp.get('token')
    if not token:
        print(json.dumps(login_resp, indent=2, ensure_ascii=False), file=sys.stderr)
        print('No pude obtener token', file=sys.stderr)
        sys.exit(1)

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

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps({
        'meta': {
            'desde': desde,
            'hasta': hasta,
            'count': len(operations),
            'source_count': total_count,
            'pages': pages,
            'generated_at': date.today().isoformat(),
        },
        'operations': operations,
        'user_data': user_data,
    }, ensure_ascii=False, indent=2))
    print(TARGET)


if __name__ == '__main__':
    ENV = load_env(ENV_PATH)
    main()
