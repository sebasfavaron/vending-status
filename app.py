#!/usr/bin/env python3
import base64
import json
import os
import secrets
import sqlite3
import subprocess
import textwrap
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

BASE = "https://os.ourvend.com"
TIMEOUT = 30
ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime" / "vending-web"
CACHE_DIR = RUNTIME / "cache"
DB_PATH = RUNTIME / "app.db"

ADIDAS_MACHINE_CONFIG = [
    {
        "slug": "sheraton",
        "label": "Adidas Sheraton",
        "site": "Lasaigues Sheraton Retiro",
        "machine_id": "2601070191",
        "status_note": "TCN id confirmado por usuario hoy.",
    },
    {
        "slug": "unicenter",
        "label": "Adidas Unicenter",
        "site": "Lasaigues Unicenter",
        "machine_id": "2601070188",
        "status_note": "TCN id confirmado en ballbox-db.",
    },
    {
        "slug": "almagro",
        "label": "Adidas Almagro",
        "site": "Lasaigues Almagro · Quintino Bocayuva 943",
        "machine_id": "2601070184",
        "status_note": "TCN id agregado por handoff orquestador. Confirmar visibilidad vendor en refresh periódico.",
    },
]
BEETWALLET_SUMMARY_PATH = ROOT / "data" / "beetwallet_summary.json"
MANUAL_REFRESH_LOG = Path("/home/sebas/runtime/ballbox/logs/public-refresh.log")

def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env_file(Path("/home/sebas/runtime/secrets/ourvend.env"))

app = Flask(__name__)
app.secret_key = os.getenv("VENDING_WEB_SECRET_KEY", "dev-secret-change-me")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


SCHEMA = """
create table if not exists products (
  id integer primary key autoincrement,
  product_id text not null unique,
  name text not null,
  price text not null default '0',
  type text not null default '',
  introduction text not null default '',
  image_url text not null default '',
  image_detail_url text not null default '',
  created_at text not null,
  updated_at text not null
);
create table if not exists ads (
  id integer primary key autoincrement,
  ad_key text not null unique,
  title text not null,
  goods_ad_url text not null,
  image_url text not null default '',
  active integer not null default 1,
  created_at text not null,
  updated_at text not null
);
create table if not exists machine_ads (
  machine_id text not null,
  ad_id integer not null,
  primary key (machine_id, ad_id)
);
create table if not exists machine_products (
  machine_id text not null,
  product_id integer not null,
  slot_no text not null default '',
  capacity text not null default '',
  quantity text not null default '',
  primary key (machine_id, product_id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{name}.json"


def load_cache(name: str, max_age_seconds: int):
    path = cache_path(name)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > max_age_seconds:
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def save_cache(name: str, payload):
    path = cache_path(name)
    path.write_text(json.dumps(payload, ensure_ascii=False))
    return payload


def money(value):
    try:
        amount = float(value)
    except Exception:
        return str(value)
    return f"${amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_all(sql: str, params=()):
    return get_db().execute(sql, params).fetchall()


def query_one(sql: str, params=()):
    return get_db().execute(sql, params).fetchone()


def execute(sql: str, params=()):
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        password = os.getenv("VENDING_WEB_PASSWORD", "").strip()
        if not password:
            return fn(*args, **kwargs)
        if session.get("ok"):
            return fn(*args, **kwargs)
        return redirect(url_for("login", next=request.path))
    return wrapper


@app.route("/ballbox")
def ballbox_root_no_slash():
    return redirect("/ballbox/", code=302)


@app.route("/ballbox-adidas-inventory/")
def legacy_ballbox_adidas_inventory():
    return redirect("/ballbox/inventory/?client=adidas", code=302)


@app.route("/ballbox-beetwallet-sales/")
def legacy_ballbox_beetwallet_sales():
    return redirect("/ballbox/sales/?client=adidas", code=302)


@app.route("/login", methods=["GET", "POST"])
def login():
    password = os.getenv("VENDING_WEB_PASSWORD", "").strip()
    if not password:
        return redirect(url_for("home"))
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), password):
            session["ok"] = True
            return redirect(request.form.get("next") or url_for("home"))
        flash("Clave inválida")
    return render_template("login.html", next=request.args.get("next", url_for("home")))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def machines():
    status_path = ROOT / "site" / "status.json"
    if not status_path.exists():
        return []
    import json
    data = json.loads(status_path.read_text())
    return data.get("machines", [])


def machine_ids():
    return [m.get("machine_id") for m in machines() if m.get("machine_id")]


def to_int(value, default=0):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def build_inventory_summary(slots):
    total_slots = len(slots)
    total_quantity = sum(to_int(row.get("quantity")) for row in slots)
    total_capacity = sum(to_int(row.get("capacity")) for row in slots)
    empty_slots = sum(1 for row in slots if to_int(row.get("quantity")) <= 0)
    low_slots = sum(1 for row in slots if 0 < to_int(row.get("quantity")) <= 2)
    suspicious_capacity_slots = sum(1 for row in slots if to_int(row.get("capacity")) >= 100)
    return {
        "total_slots": total_slots,
        "total_quantity": total_quantity,
        "total_capacity": total_capacity,
        "empty_slots": empty_slots,
        "low_slots": low_slots,
        "suspicious_capacity_slots": suspicious_capacity_slots,
    }


def load_beetwallet_summary():
    if not BEETWALLET_SUMMARY_PATH.exists():
        return None
    return json.loads(BEETWALLET_SUMMARY_PATH.read_text())


def get_vendor_products_cached(max_age_seconds: int = 180):
    cached = load_cache("vendor_products", max_age_seconds)
    if cached is not None:
        return cached
    payload = fetch_vendor_products(vendor_login())
    return save_cache("vendor_products", payload)


def adidas_inventory_views(max_age_seconds: int = 180):
    cached = load_cache("adidas_inventory_views", max_age_seconds)
    if cached is not None:
        return cached.get("views", []), cached.get("error")
    machine_map = {str(m.get("machine_id")): m for m in machines() if m.get("machine_id")}
    views = []
    error = None
    session_obj = None
    try:
        session_obj = vendor_login()
    except Exception as exc:
        error = str(exc)
    for item in ADIDAS_MACHINE_CONFIG:
        machine_id = item.get("machine_id")
        base = {
            **item,
            "machine": machine_map.get(str(machine_id)) if machine_id else None,
            "slots": [],
            "summary": None,
            "vendor_visible": False,
            "inventory_status": "missing_machine_id" if not machine_id else "pending_fetch",
            "fetch_error": None,
        }
        if machine_id and session_obj is not None:
            try:
                slots = fetch_machine_slots(session_obj, machine_id).get("slots", [])
                base["slots"] = slots
                base["summary"] = build_inventory_summary(slots)
                base["vendor_visible"] = len(slots) > 0
                base["inventory_status"] = "real" if slots else "no_slots_returned"
            except Exception as exc:
                base["fetch_error"] = str(exc)
                base["inventory_status"] = "fetch_error"
        views.append(base)
    save_cache("adidas_inventory_views", {"views": views, "error": error})
    return views, error


def encrypt_password(session_obj: requests.Session, password: str) -> str:
    pub = session_obj.post(f"{BASE}/Account/GetPubKey", timeout=TIMEOUT).text.strip()
    pem = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(textwrap.wrap(pub, 64)) + "\n-----END PUBLIC KEY-----\n"
    key = serialization.load_pem_public_key(pem.encode())
    encrypted = key.encrypt(password.encode(), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode()


def vendor_login() -> requests.Session:
    username = os.getenv("OURVEND_USERNAME", "").strip()
    password = os.getenv("OURVEND_PASSWORD", "").strip()
    if not username or not password:
        raise RuntimeError("missing OURVEND_USERNAME/OURVEND_PASSWORD")
    s = requests.Session()
    s.headers.update({"User-Agent": "vending-web/0.1"})
    user_pwd = encrypt_password(s, password)
    resp = s.post(
        f"{BASE}/Account/Login",
        data={"userAccount": username, "userPwd": user_pwd, "LoginUrl": "Account"},
        timeout=TIMEOUT,
    )
    if not resp.text.startswith("ok,"):
        raise RuntimeError(f"pc login failed: {resp.text[:200]}")
    headers = {"Referer": f"{BASE}/OperateMonitor/Index", "X-Requested-With": "XMLHttpRequest"}
    s.get(f"{BASE}/OperateMonitor/Index", timeout=TIMEOUT)
    s.post(f"{BASE}/OperateMonitor/VerificationPwd", headers=headers, timeout=TIMEOUT)
    s.post(f"{BASE}/AssetsManage/GetStringMachineGroup", headers=headers, timeout=TIMEOUT)
    s.post(f"{BASE}/OperateMonitor/getSession", headers=headers, timeout=TIMEOUT)
    return s


def fetch_assets_rows(session_obj: requests.Session):
    headers = {"Referer": f"{BASE}/AssetsManage/Index", "X-Requested-With": "XMLHttpRequest"}
    resp = session_obj.post(
        f"{BASE}/AssetsManage/ListJson",
        headers=headers,
        data={"rows": "50", "page": "1", "sidx": "MId", "sord": "desc"},
        timeout=TIMEOUT,
    )
    return resp.json().get("rows", [])


def vendor_post(session_obj: requests.Session, path: str, *, referer: str, data=None):
    headers = {"Referer": f"{BASE}{referer}", "X-Requested-With": "XMLHttpRequest"}
    resp = session_obj.post(f"{BASE}{path}", headers=headers, data=data or {}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp


def parse_vendor_date(raw):
    if not raw or not isinstance(raw, str) or not raw.startswith("/Date("):
        return raw
    try:
        millis = int(raw.removeprefix("/Date(").removesuffix(")/").split("+")[0])
    except ValueError:
        return raw
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()


def fetch_vendor_products(session_obj: requests.Session, product_type: str = "0", rows: str = "200"):
    categories = vendor_post(
        session_obj,
        "/CommodityType/ListJson",
        referer="/CommodityInfo/Index",
        data={"rows": "200", "page": "1", "sidx": "CtID", "sord": "desc"},
    ).json().get("rows", [])
    manufacturers = vendor_post(
        session_obj,
        "/CommodityInfo/GetManufacturer",
        referer="/CommodityInfo/Index",
        data={},
    ).json()
    payload = vendor_post(
        session_obj,
        "/CommodityInfo/ListJson",
        referer="/CommodityInfo/Index",
        data={"rows": rows, "page": "1", "sidx": "PrID", "sord": "desc", "Type": product_type, "PrType": "0", "PrCode": "", "PrName": ""},
    ).json()
    products = []
    for row in payload.get("rows", []):
        products.append({
            "vendor_product_id": row.get("PrID"),
            "product_id": row.get("PrCode") or "",
            "name": row.get("PrName") or "",
            "price": row.get("PrRetailPrice"),
            "cost_price": row.get("PrCostPrice"),
            "type": row.get("CiType") or "",
            "manufacturer": row.get("CiManufacturer") or "",
            "specification": row.get("PrSpecification") or "",
            "image_url": f"https://ourvend-image.oss-cn-qingdao.aliyuncs.com/Regular/{row.get('PrImgUrl')}" if row.get("PrImgUrl") else "",
            "created_at": parse_vendor_date(row.get("CreateDate")),
            "status": row.get("PStatus"),
            "status_label": {0: "pending", 1: "approved", 2: "rejected"}.get(row.get("PStatus"), "unknown"),
            "source": "vendor",
        })
    return {"products": products, "categories": categories, "manufacturers": manufacturers, "raw": payload}


def fetch_machine_slots(session_obj: requests.Session, machine_id: str):
    cabinets = vendor_post(session_obj, "/Selection/GetCabinetList", referer="/Selection/Index", data={"MachineID": machine_id, "boxId": "0"}).json()
    raw = vendor_post(session_obj, "/Selection/SoltInfo", referer="/Selection/Index", data={"MachineID": machine_id, "boxId": "0"}).json()
    slots = []
    for row in (raw[1] if isinstance(raw, list) and len(raw) > 1 else []):
        slots.append({
            "machine_id": row.get("SiMachineId") or machine_id,
            "slot_no": row.get("SiCoilId") or "",
            "product_id": row.get("SiBarCode") or "",
            "name": row.get("PrName") or "",
            "price": row.get("SiCustomPrice") or row.get("SiPrice") or "",
            "capacity": row.get("SiCapacity") or "",
            "quantity": row.get("SiExtantQuantity") or "",
            "image_url": f"https://ourvend-image.oss-cn-qingdao.aliyuncs.com/Regular/{row.get('PrImgUrl')}" if row.get("PrImgUrl") else "",
            "work_status": row.get("SiWorkStatus") or "",
            "download_state": row.get("DsiIsDownLoad") or "",
            "source": "vendor",
        })
    return {"cabinets": cabinets, "slots": slots, "raw": raw}


def fetch_screenshot(machine_id: str):
    s = vendor_login()
    headers = {"Referer": f"{BASE}/AssetsManage/Index", "X-Requested-With": "XMLHttpRequest"}
    before_rows = fetch_assets_rows(s)
    before_row = next((r for r in before_rows if r.get("MId") == machine_id), None)
    before_url = (before_row or {}).get("Url") or ""
    attempts = [
        ("POST", {"mid": machine_id}, None, "UI-real payload"),
        ("POST", {"MachineID": machine_id}, None, "legacy guess: MachineID"),
        ("POST", {"MId": machine_id}, None, "legacy guess: MId"),
        ("GET", None, {"MachineID": machine_id}, "legacy GET guess"),
    ]
    out = []
    for method, data, params, label in attempts:
        resp = s.request(method, f"{BASE}/AssetsManage/Screenshot", headers=headers, data=data, params=params, timeout=TIMEOUT)
        item = {
            "label": label,
            "method": method,
            "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type"),
            "body": resp.text[:4000],
            "ok": False,
            "summary": None,
            "before_url": before_url,
            "after_url": None,
        }
        try:
            item["json"] = resp.json()
            msg = item["json"].get("msg")
            code = item["json"].get("code")
            item["ok"] = code == 0
            if code == 0 and label == "UI-real payload":
                for _ in range(6):
                    rows = fetch_assets_rows(s)
                    row = next((r for r in rows if r.get("MId") == machine_id), None)
                    url = (row or {}).get("Url") or ""
                    if url:
                        item["after_url"] = url
                        break
                    time.sleep(3)
            if msg == "机器离线":
                item["summary"] = "Vendor respondió: máquina offline para screenshot."
            elif msg == "机器号不能为空":
                item["summary"] = "Vendor respondió: faltó el nombre correcto del parámetro de máquina."
            elif code == 0 and item["after_url"]:
                item["summary"] = "Vendor aceptó la orden y devolvió URL de screenshot en la grilla."
            elif code == 0:
                item["summary"] = "Vendor aceptó la orden, pero la URL todavía no apareció en la grilla."
            elif msg:
                item["summary"] = f"Vendor respondió: {msg}"
        except Exception:
            if resp.status_code == 403:
                item["summary"] = "Vendor devolvió 403. Ese shape no tiene permiso."
        out.append(item)
    return out


@app.route("/")
def home():
    return redirect("/ballbox/", code=302)


@app.route("/products")
def products_index():
    return redirect("/ballbox/products/", code=302)


@app.route("/products/new", methods=["GET", "POST"])
@require_login
def products_new():
    error = None
    payload = {"ProductName": "", "ProductCode": "", "PrRetailPrice": "", "PrCostPrice": "", "CiType": "", "Manufacturers": "", "PrSpecification": "", "PrContent": "", "PrAdultLimit": "0", "PrAliAdultLimit": "0", "PrPromotionPrice": "", "PrMemberPrice": "", "PrDiscount": "", "PrTaxRate": "", "QualityPeriod": "", "ImgPath": ""}
    try:
        vendor = vendor_login()
        meta = fetch_vendor_products(vendor)
        if request.method == "POST":
            payload.update({k: request.form.get(k, "") for k in payload})
            resp = vendor_post(vendor, "/CommodityInfo/AddCI", referer="/CommodityInfo/Index", data=payload)
            flash(f"Producto enviado a OurVend: {resp.text[:120]}")
            return redirect(url_for("products_index"))
        return render_template("product_form.html", title="Nuevo producto", subtitle="Alta real en OurVend /CommodityInfo/AddCI", categories=meta["categories"], manufacturers=meta["manufacturers"], form=payload, error=error)
    except Exception as exc:
        error = str(exc)
        return render_template("product_form.html", title="Nuevo producto", subtitle="Alta real en OurVend /CommodityInfo/AddCI", categories=[], manufacturers=[], form=payload, error=error)


@app.route("/products/<vendor_product_id>/edit", methods=["GET", "POST"])
@require_login
def products_edit(vendor_product_id):
    error = None
    form = {"PrID": vendor_product_id, "ProductName": "", "ProductCode": "", "PrRetailPrice": "", "PrCostPrice": "", "CiType": "", "Manufacturers": "", "PrSpecification": "", "PrContent": "", "PrAdultLimit": "0", "PrAliAdultLimit": "0", "PrPromotionPrice": "", "PrMemberPrice": "", "PrDiscount": "", "PrTaxRate": "", "QualityPeriod": "", "ImgPath": ""}
    try:
        vendor = vendor_login()
        meta = fetch_vendor_products(vendor)
        current = next((p for p in meta["products"] if str(p["vendor_product_id"]) == str(vendor_product_id)), None)
        if current:
            form.update({"ProductName": current["name"], "ProductCode": current["product_id"], "PrRetailPrice": current["price"] or "", "PrCostPrice": current["cost_price"] or "", "CiType": current["type"] or "", "PrSpecification": current["specification"] or ""})
        if request.method == "POST":
            form.update({k: request.form.get(k, "") for k in form})
            resp = vendor_post(vendor, "/CommodityInfo/EditCI", referer="/CommodityInfo/Index", data=form)
            flash(f"Producto actualizado en OurVend: {resp.text[:120]}")
            return redirect(url_for("products_index"))
        return render_template("product_form.html", title=f"Editar producto {vendor_product_id}", subtitle="Edición real en OurVend /CommodityInfo/EditCI", categories=meta["categories"], manufacturers=meta["manufacturers"], form=form, error=error)
    except Exception as exc:
        error = str(exc)
        return render_template("product_form.html", title=f"Editar producto {vendor_product_id}", subtitle="Edición real en OurVend /CommodityInfo/EditCI", categories=[], manufacturers=[], form=form, error=error)


@app.post("/products/<vendor_product_id>/delete")
@require_login
def products_delete(vendor_product_id):
    vendor = vendor_login()
    resp = vendor_post(vendor, "/CommodityInfo/Delete", referer="/CommodityInfo/Index", data={"PrID": vendor_product_id})
    flash(f"Producto borrado en OurVend: {resp.text[:120]}")
    return redirect(url_for("products_index"))


@app.route("/ads")
@require_login
def ads_index():
    return render_template("ads.html", ads=[], error=None, unsupported_reason="Ads reales todavía en recon. La app ya no usa sqlite fake como verdad.")


@app.route("/ads/new", methods=["GET", "POST"])
@app.route("/ads/<path:_unsupported>/edit", methods=["GET", "POST"])
@app.post("/ads/<path:_unsupported>/delete")
@require_login
def ads_write_blocked(_unsupported=None):
    flash("Ads write path bloqueado hasta confirmar endpoints reales de OurVend.")
    return redirect(url_for("ads_index"))


@app.route("/adidas-inventory")
def adidas_inventory():
    return redirect("/ballbox/inventory/?client=adidas", code=302)


@app.route("/adidas-sales")
def adidas_sales():
    return redirect("/ballbox/sales/?client=adidas", code=302)


@app.route("/machines")
def machines_index():
    return redirect("/ballbox/machines/", code=302)


@app.post("/machines/<machine_id>/products")
@app.post("/machines/<machine_id>/products/<path:_unsupported>/delete")
@app.post("/machines/<machine_id>/ads")
@app.post("/machines/<machine_id>/ads/<path:_unsupported>/delete")
@require_login
def machine_link_write_blocked(machine_id, _unsupported=None):
    flash(f"Machine link write path for {machine_id} bloqueado hasta migrar write endpoint real.")
    return redirect(url_for("machines_index"))


@app.route("/screenshots/<machine_id>")
@require_login
def screenshots(machine_id):
    result = None
    error = None
    if request.args.get("run") == "1":
        try:
            result = fetch_screenshot(machine_id)
        except Exception as exc:
            error = str(exc)
    return render_template("screenshot.html", machine_id=machine_id, result=result, error=error, machines=machines())


@app.route("/api/machines/<machine_id>/feed")
def api_machine_feed(machine_id):
    try:
        slots_payload = fetch_machine_slots(vendor_login(), machine_id)
    except Exception as exc:
        return jsonify({"ok": False, "machine_id": machine_id, "generated_at": now_iso(), "error": str(exc)}), 502
    slots = slots_payload["slots"]
    return jsonify({
        "ok": True,
        "machine_id": machine_id,
        "generated_at": now_iso(),
        "products": slots,
        "ads": [],
        "ads_status": "blocked_unconfirmed",
        "source": "vendor",
    })


@app.route("/api/manual-refresh", methods=["GET", "POST"])
def api_manual_refresh():
    MANUAL_REFRESH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with MANUAL_REFRESH_LOG.open("ab") as fh:
        proc = subprocess.Popen(
            ["bash", "-lc", "cd /home/sebas/work/projects/vending-status && ./scripts/refresh_ballbox_public.sh"],
            stdout=fh,
            stderr=fh,
            start_new_session=True,
        )
    return jsonify({"ok": True, "started": True, "pid": proc.pid, "log": str(MANUAL_REFRESH_LOG), "started_at": now_iso(), "method": request.method})


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "db": str(DB_PATH), "beetwallet_summary": str(BEETWALLET_SUMMARY_PATH)})


if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5081")), debug=False)
