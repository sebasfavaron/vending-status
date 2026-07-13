"""Unit tests for scripts/export_ballbox_import.py.

Uses small, scrubbed, structurally-faithful fixtures (fake ids, no real PII)
instead of the real local artifacts, and calls the payload-builder functions
directly (no file I/O) so these tests need no local secrets/data to run.

Run with:
    python3 -m unittest tests/test_export_ballbox_import.py -v
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
for path in (str(SCRIPTS_DIR), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from export_ballbox_import import (  # noqa: E402
    build_inventory_input,
    build_machines_input,
    build_transactions_input,
    canonical_json,
    is_door_open,
    is_online,
    pick_occurred_at,
    sha256_hex,
    to_price_minor,
)


class StatusFieldTests(unittest.TestCase):
    def test_is_online_true(self):
        self.assertTrue(is_online({"network": {"label": "online"}}))

    def test_is_online_false(self):
        self.assertFalse(is_online({"network": {"label": "offline"}}))

    def test_is_online_unknown_when_label_missing(self):
        self.assertIsNone(is_online({"network": {}}))
        self.assertIsNone(is_online(None))

    def test_is_door_open(self):
        self.assertTrue(is_door_open({"door": {"label": "open"}}))
        self.assertFalse(is_door_open({"door": {"label": "closed"}}))
        self.assertIsNone(is_door_open({}))


class IdempotencyKeyTests(unittest.TestCase):
    def test_same_payload_produces_same_key(self):
        payload = {"b": 2, "a": 1, "nested": {"z": 1, "y": 2}}
        self.assertEqual(sha256_hex(canonical_json(payload)), sha256_hex(canonical_json(payload)))

    def test_key_order_does_not_affect_key(self):
        payload_a = {"a": 1, "b": 2}
        payload_b = {"b": 2, "a": 1}
        self.assertEqual(sha256_hex(canonical_json(payload_a)), sha256_hex(canonical_json(payload_b)))

    def test_different_content_produces_different_key(self):
        key_1 = sha256_hex(canonical_json({"a": 1}))
        key_2 = sha256_hex(canonical_json({"a": 2}))
        self.assertNotEqual(key_1, key_2)


class TransactionInputTests(unittest.TestCase):
    def test_builds_transaction_with_raw_ars_amount(self):
        operations_payload = {
            "operations": [
                {
                    "_id": "fake_op_1",
                    "fecha": "2026-07-01T10:00:00.000Z",
                    "fecha_finalizado": "2026-07-01T10:00:10.000Z",
                    "monto": 9000,
                    "status": "finalizado",
                    "estacion": {"_id": "fake_station_1"},
                    "venta": {"seleccion": 5},
                }
            ]
        }

        transactions = build_transactions_input(operations_payload)

        self.assertEqual(len(transactions), 1)
        tx = transactions[0]
        self.assertEqual(tx["externalId"], "fake_op_1")
        self.assertEqual(tx["externalStationId"], "fake_station_1")
        self.assertEqual(tx["slotNo"], "5")
        # Raw ARS pesos, NOT multiplied by 100 — Ballbox owns that conversion.
        self.assertEqual(tx["amount"], 9000)
        self.assertEqual(tx["occurredAt"], "2026-07-01T10:00:10.000Z")
        self.assertEqual(tx["status"], "finalizado")
        self.assertEqual(tx["raw"], operations_payload["operations"][0])

    def test_falls_back_to_fecha_when_fecha_finalizado_missing(self):
        op = {"_id": "fake_op_2", "fecha": "2026-07-01T10:00:00.000Z", "monto": 100, "estacion": {"_id": "s1"}}
        self.assertEqual(pick_occurred_at(op), "2026-07-01T10:00:00.000Z")

    def test_skips_malformed_operations_without_crashing(self):
        operations_payload = {
            "operations": [
                {"_id": "missing_station", "fecha": "2026-07-01T10:00:00.000Z", "monto": 100, "estacion": {}},
                {"_id": None, "fecha": "2026-07-01T10:00:00.000Z", "monto": 100, "estacion": {"_id": "s1"}},
                {"_id": "missing_amount", "fecha": "2026-07-01T10:00:00.000Z", "estacion": {"_id": "s1"}},
                {"_id": "missing_fecha", "monto": 100, "estacion": {"_id": "s1"}},
            ]
        }

        transactions = build_transactions_input(operations_payload)

        self.assertEqual(transactions, [])

    def test_handles_no_slot_selection(self):
        operations_payload = {
            "operations": [
                {
                    "_id": "fake_op_no_slot",
                    "fecha": "2026-07-01T10:00:00.000Z",
                    "monto": 100,
                    "status": "finalizado",
                    "estacion": {"_id": "fake_station_1"},
                    "venta": {},
                }
            ]
        }

        transactions = build_transactions_input(operations_payload)

        self.assertIsNone(transactions[0]["slotNo"])


class InventoryInputTests(unittest.TestCase):
    def test_standalone_slot_is_its_own_anchor(self):
        machine_id = "fake_machine_1"
        view = {
            "slots": [
                {"machine_id": machine_id, "slot_no": "1", "name": "Agua", "price": "1000", "capacity": "10", "quantity": "5"},
            ]
        }

        inventory = build_inventory_input(
            machine_id,
            view,
            slot_names_by_machine={},
            merged_slots_by_machine={},
            product_profiles={},
            machine_slot_profiles={},
            fallback_generated_at="2026-07-01T00:00:00.000Z",
        )

        self.assertEqual(len(inventory["slots"]), 1)
        slot = inventory["slots"][0]
        self.assertEqual(slot["slotNo"], "1")
        self.assertTrue(slot["isMergedAnchor"])
        self.assertIsNone(slot["mergedAnchorSlotNo"])
        self.assertEqual(slot["priceMinor"], 100000)
        self.assertEqual(slot["currency"], "ARS")

    def test_merged_group_expands_to_one_row_per_physical_slot(self):
        machine_id = "fake_machine_2"
        view = {
            "slots": [
                {"machine_id": machine_id, "slot_no": "21", "name": "Tubo", "price": "9000", "capacity": "5", "quantity": "3"},
                {"machine_id": machine_id, "slot_no": "22", "name": "Tubo", "price": "9000", "capacity": "5", "quantity": "2"},
            ]
        }
        merged_slots_by_machine = {machine_id: {"21": {"member_slots": ["21", "22"]}}}

        inventory = build_inventory_input(
            machine_id,
            view,
            slot_names_by_machine={},
            merged_slots_by_machine=merged_slots_by_machine,
            product_profiles={},
            machine_slot_profiles={},
            fallback_generated_at="2026-07-01T00:00:00.000Z",
        )

        slots_by_no = {slot["slotNo"]: slot for slot in inventory["slots"]}
        self.assertEqual(set(slots_by_no.keys()), {"21", "22"})
        self.assertTrue(slots_by_no["21"]["isMergedAnchor"])
        self.assertIsNone(slots_by_no["21"]["mergedAnchorSlotNo"])
        self.assertFalse(slots_by_no["22"]["isMergedAnchor"])
        self.assertEqual(slots_by_no["22"]["mergedAnchorSlotNo"], "21")
        # Both physical slots share the group's resolved product identity.
        self.assertEqual(slots_by_no["21"]["productName"], slots_by_no["22"]["productName"])

    def test_returns_none_when_no_view_available(self):
        result = build_inventory_input(
            "fake_machine_3",
            None,
            slot_names_by_machine={},
            merged_slots_by_machine={},
            product_profiles={},
            machine_slot_profiles={},
            fallback_generated_at="2026-07-01T00:00:00.000Z",
        )
        self.assertIsNone(result)

    def test_price_minor_is_none_when_price_undefined(self):
        machine_id = "fake_machine_4"
        # A vendor-reported placeholder price (e.g. ">=100"-style sentinel handled
        # upstream) should not be fabricated into a fake priceMinor value.
        view = {"slots": [{"machine_id": machine_id, "slot_no": "1", "name": "Agua", "price": "", "capacity": "10", "quantity": "5"}]}

        inventory = build_inventory_input(
            machine_id,
            view,
            slot_names_by_machine={},
            merged_slots_by_machine={},
            product_profiles={},
            machine_slot_profiles={},
            fallback_generated_at="2026-07-01T00:00:00.000Z",
        )

        slot = inventory["slots"][0]
        self.assertFalse(slot["priceDefined"])
        self.assertIsNone(slot["priceMinor"])
        self.assertIsNone(slot["currency"])


class MachinesInputTests(unittest.TestCase):
    def test_emits_a_row_even_when_ballbox_link_is_unknown_here(self):
        # The exporter never checks whether Ballbox has a MachineExternalLink for
        # this machine — it always emits the row and lets Ballbox quarantine
        # centrally if unresolved. That keeps "fail loud, not silent" in one place.
        meta_rows = [{"machine_id": "fake_unknown_machine", "station_id": "fake_unknown_station", "slug": "fake-slug"}]

        machines = build_machines_input(
            status={"machines": []},
            adidas={},
            meta_rows=meta_rows,
            views_by_slug={},
            slot_mapping={},
            fallback_generated_at="2026-07-01T00:00:00.000Z",
        )

        self.assertEqual(len(machines), 1)
        self.assertEqual(machines[0]["externalMachineId"], "fake_unknown_machine")
        self.assertEqual(machines[0]["externalStationId"], "fake_unknown_station")
        self.assertNotIn("status", machines[0])
        self.assertNotIn("inventory", machines[0])

    def test_omits_external_station_id_when_metadata_has_none(self):
        meta_rows = [{"machine_id": "fake_machine_no_station", "slug": "fake-slug"}]

        machines = build_machines_input(
            status={"machines": []},
            adidas={},
            meta_rows=meta_rows,
            views_by_slug={},
            slot_mapping={},
            fallback_generated_at="2026-07-01T00:00:00.000Z",
        )

        self.assertNotIn("externalStationId", machines[0])

    def test_includes_status_when_available(self):
        meta_rows = [{"machine_id": "fake_machine_1", "station_id": "fake_station_1", "slug": "fake-slug"}]
        status = {
            "machines": [
                {
                    "machine_id": "fake_machine_1",
                    "network": {"label": "online"},
                    "door": {"label": "closed"},
                    "temperature_raw": "--",
                    "freshness": {"last_upload_time": "2026-07-01T00:05:00.000Z"},
                }
            ]
        }

        machines = build_machines_input(
            status=status,
            adidas={},
            meta_rows=meta_rows,
            views_by_slug={},
            slot_mapping={},
            fallback_generated_at="2026-07-01T00:00:00.000Z",
        )

        self.assertIn("status", machines[0])
        self.assertTrue(machines[0]["status"]["online"])
        self.assertFalse(machines[0]["status"]["doorOpen"])
        self.assertEqual(machines[0]["status"]["sourceGeneratedAt"], "2026-07-01T00:05:00.000Z")


class PriceMinorTests(unittest.TestCase):
    def test_converts_ars_pesos_to_minor_units(self):
        self.assertEqual(to_price_minor(9000), 900000)
        self.assertEqual(to_price_minor("9000"), 900000)
        self.assertIsNone(to_price_minor(None))


if __name__ == "__main__":
    unittest.main()
