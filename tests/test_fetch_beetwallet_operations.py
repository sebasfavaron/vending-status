"""Unit tests for the second-account guards in scripts/fetch_beetwallet_operations.py.

Run with:
    python3 -m unittest discover -s tests
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fetch_beetwallet_operations as fetcher  # noqa: E402


class SecondAccountGuardTests(unittest.TestCase):
    def run_main(self, prefix, optional, environ):
        with mock.patch.object(fetcher, "ENV", {}, create=True), mock.patch.object(
            fetcher, "ACCOUNT_PREFIX", prefix
        ), mock.patch.object(fetcher, "OPTIONAL", optional), mock.patch.dict(
            os.environ, environ, clear=True
        ), mock.patch.object(sys, "argv", ["fetch"]), mock.patch.object(
            fetcher, "post_json", side_effect=AssertionError("must not log in")
        ):
            with self.assertRaises(SystemExit) as raised:
                fetcher.main()
        return raised.exception.code

    def test_second_account_without_credentials_is_skipped(self):
        code = self.run_main("BEET2_", True, {"BEET_OPERATIONS_OUT": "/tmp/x.json", "BEET_USERNAME": "a", "BEET_PASSWORD": "b"})
        self.assertEqual(code, 0)

    def test_second_account_never_writes_the_first_accounts_file(self):
        code = self.run_main("BEET2_", True, {"BEET2_USERNAME": "a", "BEET2_PASSWORD": "b"})
        self.assertEqual(code, 2)

    def test_first_account_without_credentials_still_fails(self):
        code = self.run_main("BEET_", False, {})
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
