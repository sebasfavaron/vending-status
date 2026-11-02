"""The lessee (arrendatario) keys, and the legacy `client` keys they replaced.

Configs are gitignored local files (and a GitHub secret tarball), so every reader takes
`lessee` / `lessee_label` first and falls back to `client` / `client_label`.

Run with:
    python3 -m unittest tests/test_lessee_keys.py -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("MONTHLY_REPORT_STATE_DIR", str(Path(TMP.name) / "state"))

import send_low_stock_alerts as stock  # noqa: E402
import send_monthly_sales_report as monthly  # noqa: E402
import send_sales_report_email as sales_email  # noqa: E402
from lessee import get_lessee, get_lessee_label  # noqa: E402


def machines(key: str) -> list[dict]:
    return [
        {"machine_id": "m1", "station_id": "s1", "label": "Uno", key: "adidas"},
        {"machine_id": "m2", "station_id": "s2", "label": "Dos", key: "adidas"},
        {"machine_id": "m3", "station_id": "s3", "label": "Otra", key: "otro"},
    ]


class LesseeHelper(unittest.TestCase):
    def test_new_key(self):
        self.assertEqual(get_lessee({"lessee": "adidas"}), "adidas")
        self.assertEqual(get_lessee_label({"lessee_label": "Adidas"}), "Adidas")

    def test_legacy_key(self):
        self.assertEqual(get_lessee({"client": "adidas"}), "adidas")
        self.assertEqual(get_lessee_label({"client_label": "Adidas"}), "Adidas")

    def test_new_key_wins(self):
        row = {"lessee": "nuevo", "client": "viejo", "lessee_label": "Nuevo", "client_label": "Viejo"}
        self.assertEqual(get_lessee(row), "nuevo")
        self.assertEqual(get_lessee_label(row), "Nuevo")

    def test_missing(self):
        self.assertIsNone(get_lessee({}))
        self.assertIsNone(get_lessee(None))
        self.assertIsNone(get_lessee_label({"label": "x"}))


class ConfigLoaders(unittest.TestCase):
    def write(self, payload: dict) -> Path:
        path = Path(tempfile.mkdtemp(dir=TMP.name)) / "config.json"
        path.write_text(json.dumps(payload))
        return path

    def check(self, load_config):
        for keys in (("lessee", "lessee_label"), ("client", "client_label")):
            with self.subTest(load_config=load_config.__module__, keys=keys):
                config = load_config(self.write({keys[0]: "nike", keys[1]: "Nike"}))
                self.assertEqual(config["lessee"], "nike")
                self.assertEqual(config["lessee_label"], "Nike")
        with self.subTest(load_config=load_config.__module__, keys="defaults"):
            config = load_config(self.write({}))
            self.assertEqual(config["lessee"], "adidas")
            self.assertEqual(config["lessee_label"], "Adidas")

    def test_monthly_sales_report(self):
        self.check(monthly.load_config)

    def test_sales_email(self):
        self.check(sales_email.load_config)

    def test_stock_alerts(self):
        self.check(stock.load_config)


class MachineFilters(unittest.TestCase):
    def test_sales_report_filters_machines_by_lessee(self):
        for key in ("lessee", "client"):
            with self.subTest(key=key):
                report = sales_email.collect_sales_report(
                    operations_payload={"operations": []},
                    inventory_payload={},
                    mapping_payload={},
                    machines_meta=machines(key),
                    target_lessee="adidas",
                    month_key="2026-10",
                )
                self.assertEqual(len(report["machine_sections"]), 2)

    def test_low_stock_filters_machines_by_lessee(self):
        for key in ("lessee", "client"):
            with self.subTest(key=key):
                inventory = {"data": {"machines": [
                    {"machine_id": "m1", key: "Adidas Padel", "slots": [{"slot_no": "1", "quantity": 0}]},
                    {"machine_id": "m3", key: "Otro", "slots": [{"slot_no": "1", "quantity": 0}]},
                ]}}
                rows, _, processable = stock.collect_low_slots(inventory, {"machines": {}}, "adidas", 2)
                self.assertEqual(processable, {"m1"})
                self.assertEqual([row["machine_id"] for row in rows], ["m1"])


class MonthlyReportDryRun(unittest.TestCase):
    """End to end: the same report with old-key and new-key config files."""

    def run_dry(self, key: str, label_key: str) -> dict:
        base = Path(tempfile.mkdtemp(dir=TMP.name))
        (base / "ops.json").write_text(json.dumps({"operations": []}))
        (base / "mapping.json").write_text(json.dumps({"machines": {}}))
        (base / "machines.json").write_text(json.dumps(machines(key)))
        config = base / "config.json"
        config.write_text(json.dumps({
            "enabled": True,
            key: "adidas",
            label_key: "Adidas",
            "operations_path": str(base / "ops.json"),
            "inventory_path": str(base / "missing-inventory.json"),
            "mapping_path": str(base / "mapping.json"),
            "machines_path": str(base / "machines.json"),
            "preview_path": str(base / "preview.html"),
        }))
        argv = ["send_monthly_sales_report.py", "--config", str(config), "--month", "2026-10", "--dry-run"]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(monthly, "send_telegram"), redirect_stdout(out):
            self.assertEqual(monthly.main(), 0)
        return json.loads(out.getvalue())

    def test_old_and_new_keys_build_the_same_report(self):
        new = self.run_dry("lessee", "lessee_label")
        old = self.run_dry("client", "client_label")
        self.assertEqual(new["status"], "preview_built")
        self.assertEqual(new["machine_count"], 2)
        self.assertEqual(new["subject"], "Ballbox · Adidas: resumen ventas 2026-10")
        new.pop("preview_path")
        old.pop("preview_path")
        self.assertEqual(new, old)


if __name__ == "__main__":
    unittest.main()
