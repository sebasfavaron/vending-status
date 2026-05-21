#!/usr/bin/env python3
import base64
import json
import os
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE = "https://os.ourvend.com"
H5_BASE = "https://oshw.ourvend.com"
OUT = Path("site/status.json")
TIMEOUT = 30


def getenv_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"missing env: {name}")
    return value


def encrypt_password(session: requests.Session, base: str, password: str) -> str:
    pub = session.post(f"{base}/Account/GetPubKey", timeout=TIMEOUT).text.strip()
    pem = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(textwrap.wrap(pub, 64)) + "\n-----END PUBLIC KEY-----\n"
    key = serialization.load_pem_public_key(pem.encode())
    encrypted = key.encrypt(password.encode(), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode()


def login_pc(session: requests.Session, username: str, password: str) -> None:
    user_pwd = encrypt_password(session, BASE, password)
    resp = session.post(
        f"{BASE}/Account/Login",
        data={"userAccount": username, "userPwd": user_pwd, "LoginUrl": "Account"},
        timeout=TIMEOUT,
    )
    if not resp.text.startswith("ok,"):
        raise SystemExit(f"pc login failed: {resp.text[:200]}")


def login_h5(session: requests.Session, username: str, password: str) -> None:
    user_pwd = encrypt_password(session, H5_BASE, password)
    resp = session.post(
        f"{H5_BASE}/Account/HLoginWay",
        data={"Account": username, "Pwd": user_pwd},
        timeout=TIMEOUT,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise SystemExit(f"h5 login failed: {data}")


def humanize(value):
    mapping = {
        None: None,
        "": None,
        "默认机组": "grupo por defecto",
        "正常": "normal",
        "无": "none",
        "关闭": "closed",
        "打开": "open",
        "开启": "enabled",
        "异常": "error",
        "缺货": "out_of_stock",
    }
    return mapping.get(value, value)


def map_network(value):
    mapping = {
        0: {"label": "online", "ui_inference": "green/normal icon"},
        1: {"label": "offline", "ui_inference": "x/alert icon"},
        2: {"label": "warning", "ui_inference": "yellow warning icon"},
        3: {"label": "error", "ui_inference": "red alert icon"},
        4: {"label": "unknown", "ui_inference": "alternate icon"},
    }
    return mapping.get(value, {"label": f"unknown:{value}", "ui_inference": "unmapped"})


def map_door(value):
    return {0: "closed", 1: "open", 2: "closed"}.get(value, f"unknown:{value}")


def map_peripheral(value):
    return {
        1: "open",
        2: "closed",
        3: "enabled",
        4: "disabled",
        5: "normal",
        6: "error",
        7: "none",
        8: "out_of_stock",
    }.get(value, f"unknown:{value}")


def fetch_pc(session: requests.Session) -> dict:
    headers = {"Referer": f"{BASE}/OperateMonitor/Index", "X-Requested-With": "XMLHttpRequest"}
    session.get(f"{BASE}/OperateMonitor/Index", timeout=TIMEOUT)
    session.post(f"{BASE}/OperateMonitor/VerificationPwd", headers=headers, timeout=TIMEOUT)
    groups_resp = session.post(f"{BASE}/AssetsManage/GetStringMachineGroup", headers=headers, timeout=TIMEOUT)
    session.post(f"{BASE}/OperateMonitor/getSession", headers=headers, timeout=TIMEOUT)
    status_resp = session.get(
        f"{BASE}/OperateMonitor/ListJson/?firstload=0&_search=false&rows=50&page=1&sidx=MiNoline&sord=desc",
        headers=headers,
        timeout=TIMEOUT,
    )

    sale_headers = {"Referer": f"{BASE}/SaleMonitor/Index", "X-Requested-With": "XMLHttpRequest"}
    session.get(f"{BASE}/SaleMonitor/Index", timeout=TIMEOUT)
    session.post(f"{BASE}/SaleMonitor/getSession", headers=sale_headers, timeout=TIMEOUT)
    sales_resp = session.post(
        f"{BASE}/SaleMonitor/ListJson",
        headers=sale_headers,
        data={"MiGroup": "", "MachineID": "", "boxId": "", "page": 1, "rows": 50, "sidx": "ID", "sord": "desc"},
        timeout=TIMEOUT,
    )
    return {"groups": groups_resp.json(), "status": status_resp.json(), "sales": sales_resp.json()}


def fetch_h5(session: requests.Session) -> dict:
    session.get(f"{H5_BASE}/wxapp/index", timeout=TIMEOUT)
    return {
        "dopm": session.post(f"{H5_BASE}/WapApp/DOPM", timeout=TIMEOUT).text.strip(),
        "money": session.post(f"{H5_BASE}/WapApp/DOPM_Money", timeout=TIMEOUT).text.strip(),
        "groups": session.post(f"{H5_BASE}/WapApp/Group", timeout=TIMEOUT).json(),
        "network_count": session.post(f"{H5_BASE}/WapApp/NMCount", timeout=TIMEOUT).text.strip(),
        "network_rows": session.post(f"{H5_BASE}/WapApp/NMData", data={"Page": 1}, timeout=TIMEOUT).json(),
        "stock_counts": session.post(f"{H5_BASE}/WapApp/getjzShowOne", data={"Page": 1}, timeout=TIMEOUT).json(),
        "stock_rows": session.post(f"{H5_BASE}/WapApp/MSOne", data={"Page": 1}, timeout=TIMEOUT).json(),
    }


def normalize(pc: dict, h5: dict) -> dict:
    rows = pc["status"].get("rows", [])
    sales_rows = {row.get("MId"): row for row in pc.get("sales", {}).get("rows", [])}
    h5_network = {row.get("MID"): row for row in h5.get("network_rows", [])}
    stock_by_machine = {str(row.get("SiMachineId")): row for row in h5.get("stock_rows", []) if row.get("SiMachineId") is not None}
    machines = []
    for row in rows:
        sale_row = sales_rows.get(row.get("MId"), {})
        h5_net = h5_network.get(row.get("MId"), {})
        stock_row = stock_by_machine.get(str(row.get("MId")), {})
        door = row.get("Door")
        mi_noline = row.get("MiNoline")
        machines.append({
            "machine_id": row.get("MId"),
            "alias": row.get("MiAlias") or row.get("MId"),
            "group_name": humanize(row.get("MGroupName") or row.get("MgName")),
            "network": {"raw": mi_noline, **map_network(mi_noline)},
            "temperature_raw": row.get("MiInsideTemp"),
            "door": {"raw": door, "label": humanize(map_door(door))},
            "drop_sensor": {"raw": row.get("Motots"), "label": humanize(map_peripheral(row.get("Motots")))},
            "bill_acceptor": humanize(row.get("Note")),
            "coin_device": humanize(row.get("Coin")),
            "slot_status": humanize(row.get("Selection")),
            "card_reader": {"raw": row.get("Reader"), "label": humanize(map_peripheral(row.get("Reader")))},
            "used_flow_month": row.get("TotalFlow"),
            "version": row.get("Versions") or None,
            "address": row.get("MiAddress") or None,
            "sales_today": {
                "amount": sale_row.get("SaleAmount") or "0",
                "quantity": sale_row.get("SaleQuantity") or "0",
                "cash_amount": sale_row.get("CashAmount") or "0",
                "failed_amount": sale_row.get("FailAmount") or "0",
            },
            "freshness": {
                "last_upload_time": h5_net.get("LastUploadTime") or None,
                "status": "unknown" if not h5_net.get("LastUploadTime") else "known",
            },
            "stock": {
                "out_of_stock": bool((stock_row.get("SiQueHuo") or 0) not in [0, "0", None]),
                "raw": stock_row,
            },
            "visible_actions": [
                "view status",
                "view stock",
                "view sales",
                "view alerts",
                "remote shipment available in vendor UI",
                "slot edit visible in vendor UI",
                "upgrade visible in vendor UI",
            ],
        })
    dopm = (h5.get("dopm") or "").split("|")
    money = (h5.get("money") or "||").split("|")
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "machine_count": len(machines),
        "online_count": sum(1 for m in machines if m["network"]["label"] == "online"),
        "offline_count": sum(1 for m in machines if m["network"]["label"] == "offline"),
        "group_count": len(pc.get("groups", [])),
        "groups": pc.get("groups", []),
        "overview": {
            "stockout_machine_count": h5.get("stock_counts", {}).get("jz", "0"),
            "stockout_slot_count": h5.get("stock_counts", {}).get("hz", "0"),
            "stockout_commodity_count": h5.get("stock_counts", {}).get("sz", "0"),
            "abnormal_network_count": dopm[1] if len(dopm) > 1 else "0",
            "faulty_machine_count": dopm[2] if len(dopm) > 2 else "0",
            "sales_today_total": money[0] if len(money) > 0 and money[0] else "0",
            "sales_today_cash": money[1] if len(money) > 1 and money[1] else "0",
            "sales_today_non_cash": money[2] if len(money) > 2 and money[2] else "0",
        },
        "machines": machines,
    }


def main() -> None:
    username = getenv_required("OURVEND_USERNAME")
    password = getenv_required("OURVEND_PASSWORD")
    pc_session = requests.Session()
    h5_session = requests.Session()
    pc_session.headers.update({"User-Agent": "gh-pages vending-status/0.1"})
    h5_session.headers.update({"User-Agent": "gh-pages vending-status/0.1"})
    login_pc(pc_session, username, password)
    login_h5(h5_session, username, password)
    data = normalize(fetch_pc(pc_session), fetch_h5(h5_session))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(json.dumps({"ok": True, "machines": data["machine_count"]}))


if __name__ == "__main__":
    main()
