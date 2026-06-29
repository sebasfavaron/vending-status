#!/usr/bin/env python3
import argparse
import base64
import json
import os
import sys
import textwrap
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE = "https://os.ourvend.com"
TIMEOUT = 30
OUT = Path("runtime/probes")


def getenv_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"missing env: {name}")
    return value


def encrypt_password(session: requests.Session, password: str) -> str:
    pub = session.post(f"{BASE}/Account/GetPubKey", timeout=TIMEOUT).text.strip()
    pem = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(textwrap.wrap(pub, 64)) + "\n-----END PUBLIC KEY-----\n"
    key = serialization.load_pem_public_key(pem.encode())
    encrypted = key.encrypt(password.encode(), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode()


def login_pc(session: requests.Session, username: str, password: str) -> None:
    user_pwd = encrypt_password(session, password)
    resp = session.post(
        f"{BASE}/Account/Login",
        data={"userAccount": username, "userPwd": user_pwd, "LoginUrl": "Account"},
        timeout=TIMEOUT,
    )
    if not resp.text.startswith("ok,"):
        raise SystemExit(f"pc login failed: {resp.text[:200]}")


def bootstrap(session: requests.Session) -> None:
    headers = {"Referer": f"{BASE}/OperateMonitor/Index", "X-Requested-With": "XMLHttpRequest"}
    session.get(f"{BASE}/OperateMonitor/Index", timeout=TIMEOUT)
    session.post(f"{BASE}/OperateMonitor/VerificationPwd", headers=headers, timeout=TIMEOUT)
    session.post(f"{BASE}/AssetsManage/GetStringMachineGroup", headers=headers, timeout=TIMEOUT)
    session.post(f"{BASE}/OperateMonitor/getSession", headers=headers, timeout=TIMEOUT)


def dump(name: str, payload: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def probe(session: requests.Session, method: str, path: str, *, data=None, params=None, referer=None) -> dict:
    headers = {"X-Requested-With": "XMLHttpRequest"}
    if referer:
        headers["Referer"] = f"{BASE}{referer}"
    url = f"{BASE}{path}"
    resp = session.request(method, url, headers=headers, data=data, params=params, timeout=TIMEOUT)
    body = resp.text[:4000]
    result = {
        "method": method,
        "path": path,
        "status_code": resp.status_code,
        "content_type": resp.headers.get("content-type"),
        "body_prefix": body,
    }
    try:
        result["json"] = resp.json()
    except Exception:
        pass
    return result


def screenshot_probe(session: requests.Session, machine_id: str) -> dict:
    candidates = [
        {"path": "/AssetsManage/Screenshot", "data": {"MachineID": machine_id}, "referer": "/AssetsManage/Index"},
        {"path": "/AssetsManage/Screenshot", "data": {"MId": machine_id}, "referer": "/AssetsManage/Index"},
        {"path": "/AssetsManage/Screenshot", "params": {"MachineID": machine_id}, "referer": "/AssetsManage/Index"},
    ]
    return {
        "machine_id": machine_id,
        "results": [probe(session, "POST" if c.get("data") is not None else "GET", c["path"], data=c.get("data"), params=c.get("params"), referer=c.get("referer")) for c in candidates],
    }


def commodity_probe(session: requests.Session) -> dict:
    candidates = [
        {"path": "/CommodityInfo/Index", "referer": "/CommodityInfo/Index"},
        {"path": "/CommodityInfo/AuditList", "referer": "/CommodityInfo/Index"},
        {"path": "/CommodityType/ListJson", "referer": "/CommodityType/Index"},
    ]
    results = []
    for c in candidates:
        session.get(f"{BASE}{c['referer']}", timeout=TIMEOUT)
        results.append(probe(session, "GET", c["path"], referer=c["referer"]))
    return {"results": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["screenshot", "commodity"])
    parser.add_argument("--machine-id", default="2601070188")
    args = parser.parse_args()

    username = getenv_required("OURVEND_USERNAME")
    password = getenv_required("OURVEND_PASSWORD")
    session = requests.Session()
    session.headers.update({"User-Agent": "vending-status probe/0.1"})
    login_pc(session, username, password)
    bootstrap(session)

    if args.action == "screenshot":
        payload = screenshot_probe(session, args.machine_id)
        out = dump(f"screenshot-{args.machine_id}", payload)
    else:
        payload = commodity_probe(session)
        out = dump("commodity-surface", payload)

    print(json.dumps({"ok": True, "out": str(out)}))


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)
