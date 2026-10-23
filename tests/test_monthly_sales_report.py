"""Who send_monthly_sales_report.py emails in each mode.

Fixtures are fake (no real addresses or sales). Resend is mocked; nothing leaves the machine.

Run with:
    python3 -m unittest tests/test_monthly_sales_report.py -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

TMP = tempfile.TemporaryDirectory()
os.environ["MONTHLY_REPORT_STATE_DIR"] = str(Path(TMP.name) / "state")
os.environ["RESEND_API_KEY"] = "test-key"

import send_monthly_sales_report as report  # noqa: E402

CLIENT = ["client@example.com"]
REVIEW = ["review@example.com"]


def write_fixtures(base: Path) -> Path:
    (base / "data").mkdir(parents=True, exist_ok=True)
    (base / "data" / "ops.json").write_text(json.dumps({"operations": []}))
    (base / "mapping.json").write_text(json.dumps({"machines": {}}))
    (base / "machines.json").write_text(json.dumps([]))
    config = {
        "enabled": True,
        "recipient_emails": CLIENT,
        "review_recipient_emails": REVIEW,
        "operations_path": str(base / "data" / "ops.json"),
        "inventory_path": str(base / "missing-inventory.json"),
        "mapping_path": str(base / "mapping.json"),
        "machines_path": str(base / "machines.json"),
        "preview_path": str(base / "preview.html"),
    }
    path = base / "config.json"
    path.write_text(json.dumps(config))
    return path


class MonthlySalesReportRecipients(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir=TMP.name))
        self.config = write_fixtures(self.base)
        for log in (report.SENT_LOG_PATH, report.REVIEWED_LOG_PATH):
            log.unlink(missing_ok=True)

    def run_main(self, *extra):
        argv = ["send_monthly_sales_report.py", "--config", str(self.config), "--month", "2026-10", *extra]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(report, "send_telegram"), \
             mock.patch.object(report.requests, "post") as post, \
             mock.patch("builtins.print"):
            post.return_value.json.return_value = {"id": "resend-test-id"}
            try:
                code = report.main()
            except SystemExit as exc:
                code = exc.code
        return code, [call.kwargs["json"]["to"] for call in post.call_args_list]

    def test_dry_run_sends_nothing(self):
        code, sent_to = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(sent_to, [])

    def test_default_mode_emails_review_list_only(self):
        code, sent_to = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(sent_to, [REVIEW])
        self.assertTrue(report.REVIEWED_LOG_PATH.exists())
        self.assertFalse(report.SENT_LOG_PATH.exists())

    def test_client_send_refused_without_a_review(self):
        code, sent_to = self.run_main("--confirm-send")
        self.assertNotEqual(code, 0)
        self.assertEqual(sent_to, [])

    def test_client_send_after_review_then_not_twice(self):
        self.run_main()
        code, sent_to = self.run_main("--confirm-send")
        self.assertEqual(code, 0)
        self.assertEqual(sent_to, [CLIENT])
        code, sent_to = self.run_main("--confirm-send")
        self.assertEqual(code, 0)
        self.assertEqual(sent_to, [])


if __name__ == "__main__":
    unittest.main()
