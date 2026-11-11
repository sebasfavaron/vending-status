"""Several BeetWallet operator accounts feed one operations file.

Run with:
    python3 -m unittest tests/test_fetch_beetwallet_accounts.py -v
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

import fetch_beetwallet_operations as fetch  # noqa: E402


def op(op_id, station):
    return {"_id": op_id, "fecha": "2026-11-07T17:05:00Z", "estacion": {"_id": station}}


class AccountsTest(unittest.TestCase):
    def setUp(self):
        fetch.ENV = {}

    def test_reads_main_then_numbered_accounts_and_skips_incomplete_ones(self):
        env = {"BEET_USERNAME": "adidas", "BEET_PASSWORD": "a",
               "BEET_USERNAME_2": "tortuguitas", "BEET_PASSWORD_2": "t",
               "BEET_USERNAME_3": "sin-clave"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(fetch.accounts(), [("adidas", "a"), ("tortuguitas", "t")])

    def test_merge_keeps_each_operation_once(self):
        merged = fetch.merge_operations([[op("1", "s"), op("2", "s")], [op("2", "s"), op("3", "t")]])
        self.assertEqual([o["_id"] for o in merged], ["1", "2", "3"])


class MainTest(unittest.TestCase):
    def run_main(self, results, env):
        out = Path(tempfile.mkdtemp()) / "ops.json"

        def fake_fetch(api, user, password, desde, hasta):
            result = results[user]
            if isinstance(result, Exception):
                raise result
            return result, len(result), 1, {"user": user}

        fetch.ENV = {}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(fetch, "TARGET", out), \
                mock.patch.object(fetch, "fetch_account", side_effect=fake_fetch), \
                mock.patch.object(sys, "argv", ["fetch"]):
            fetch.main()
        return json.loads(out.read_text())

    ENV = {"BEET_USERNAME": "adidas", "BEET_PASSWORD": "a",
           "BEET_USERNAME_2": "tortuguitas", "BEET_PASSWORD_2": "t"}

    def test_operations_of_every_account_land_in_the_file(self):
        data = self.run_main({"adidas": [op("1", "sheraton")], "tortuguitas": [op("9", "tortuguitas")]}, self.ENV)
        self.assertEqual(sorted(o["_id"] for o in data["operations"]), ["1", "9"])
        self.assertEqual(data["meta"]["accounts"], 2)
        self.assertEqual(data["user_data"], {"user": "adidas"})

    def test_a_failing_extra_account_does_not_hide_the_main_one(self):
        data = self.run_main({"adidas": [op("1", "sheraton")], "tortuguitas": RuntimeError("clave")}, self.ENV)
        self.assertEqual([o["_id"] for o in data["operations"]], ["1"])
        self.assertEqual(data["meta"]["accounts_failed"], ["tortuguitas"])

    def test_a_failing_main_account_stops_the_run(self):
        with self.assertRaises(SystemExit):
            self.run_main({"adidas": RuntimeError("clave"), "tortuguitas": []}, self.ENV)


if __name__ == "__main__":
    unittest.main()
