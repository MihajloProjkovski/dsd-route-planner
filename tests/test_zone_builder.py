import unittest
from io import BytesIO

import pandas as pd

from zone_builder import (
    ZoneInputError,
    build_zone_proposal,
    make_input_template,
    make_zone_workbook,
)
from route_planner import _assign_daily_zone_affinities


class BuildZoneProposalTests(unittest.TestCase):
    def setUp(self):
        self.history = pd.DataFrame([
            {
                "delivery_date": "2026-05-04",
                "shipment_id": "trip-1",
                "customer_id": "C1",
                "vehicle_id": "V1",
                "vehicle_type": "Van",
                "quantity_kg": 30,
                "cases": 2,
            },
            {
                "delivery_date": "2026-05-04",
                "shipment_id": "trip-1",
                "customer_id": "C1",
                "vehicle_id": "V1",
                "vehicle_type": "Van",
                "quantity_kg": 20,
                "cases": 1,
            },
            {
                "delivery_date": "2026-05-04",
                "shipment_id": "trip-2",
                "customer_id": "C1",
                "vehicle_id": "V2",
                "vehicle_type": "Furgon",
                "quantity_kg": 40,
                "cases": 2,
            },
            {
                "delivery_date": "2026-05-05",
                "shipment_id": "trip-3",
                "customer_id": "C2",
                "vehicle_id": "K1",
                "vehicle_type": "Kamion",
                "quantity_kg": 500,
                "cases": 10,
            },
        ])
        self.customers = pd.DataFrame([
            {"customer_id": "C1", "customer_name": "One", "latitude": 42.01, "longitude": 21.41},
            {"customer_id": "C2", "customer_name": "Two", "latitude": 42.03, "longitude": 21.45},
        ])

    def test_aggregates_lines_per_customer_and_shipment_but_keeps_repeat_shipments(self):
        updated, summary, metadata = build_zone_proposal(
            self.history, self.customers, target_zone_count=2
        )
        self.assertEqual(metadata["delivery_rows"], 4)
        self.assertEqual(metadata["stops"], 3)
        customer = updated.set_index("customer_code").loc["C1"]
        self.assertEqual(customer["visits"], 2)
        self.assertEqual(customer["total_kg"], 90)
        self.assertEqual(customer["historical_vehicle_types"], "Furgon,Van")
        self.assertEqual(len(summary), 2)

    def test_output_is_deterministic_for_same_input(self):
        first = build_zone_proposal(self.history, self.customers, target_zone_count=2)
        second = build_zone_proposal(self.history, self.customers, target_zone_count=2)
        pd.testing.assert_frame_equal(first[0], second[0])
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[2], second[2])

    def test_conflicting_vehicle_types_in_one_shipment_are_rejected(self):
        conflict = self.history.copy()
        conflict.loc[1, "vehicle_type"] = "Kamion"
        with self.assertRaisesRegex(ZoneInputError, "one vehicle type"):
            build_zone_proposal(conflict, self.customers, target_zone_count=2)

    def test_month_and_weekday_patterns_are_included_in_summary(self):
        _, summary, _ = build_zone_proposal(
            self.history, self.customers, target_zone_count=2
        )
        self.assertIn("avg_Monday_stops_per_day", summary[0])
        self.assertIn("avg_May_stops_per_day", summary[0])

    def test_missing_shipment_id_is_rejected(self):
        invalid = self.history.copy()
        invalid.loc[0, "shipment_id"] = ""
        with self.assertRaisesRegex(ZoneInputError, "shipment/customer/vehicle IDs"):
            build_zone_proposal(invalid, self.customers, target_zone_count=2)

    def test_history_is_not_treated_as_a_hard_vehicle_restriction(self):
        updated, _, _ = build_zone_proposal(
            self.history, self.customers, target_zone_count=2
        )
        customer = updated.set_index("customer_code").loc["C1"]
        self.assertEqual(customer["historical_vehicle_types"], "Furgon,Van")
        self.assertEqual(customer["eligible_vehicles"], "Furgon,Kamion,Van")

    def test_explicit_vehicle_restriction_is_preserved(self):
        customers = self.customers.copy()
        customers["allowed_vehicle_types"] = ["Van", ""]
        updated, _, _ = build_zone_proposal(self.history, customers, target_zone_count=2)
        customer = updated.set_index("customer_code").loc["C1"]
        self.assertEqual(customer["eligible_vehicles"], "Van")

    def test_daily_vehicle_zone_affinity_is_assigned_from_workload(self):
        stops = pd.DataFrame([
            {
                "zone": "Van_01", "kg": 100,
                "eligible_vehicles": "Kamion,Van", "preferred_vehicle": "Van",
            },
            {
                "zone": "Kamion_01", "kg": 1000,
                "eligible_vehicles": "Kamion,Van", "preferred_vehicle": "Kamion",
            },
        ])
        vehicles = pd.DataFrame([
            {
                "vehicle_name": "V1", "vehicle_type": "Van",
                "capacity_kg": 3200, "max_trips_per_day": 2,
            },
            {
                "vehicle_name": "K1", "vehicle_type": "Kamion",
                "capacity_kg": 6000, "max_trips_per_day": 2,
            },
        ])
        assigned = _assign_daily_zone_affinities(stops, vehicles).set_index("vehicle_name")
        self.assertEqual(assigned.loc["V1", "zone"], "Van_01")
        self.assertEqual(assigned.loc["K1", "zone"], "Kamion_01")

    def test_cancelled_lines_are_excluded_from_stops(self):
        history = self.history.copy()
        history["delivery_status"] = "delivered"
        history.loc[0, "delivery_status"] = "cancelled"
        _, _, metadata = build_zone_proposal(history, self.customers, target_zone_count=2)
        self.assertEqual(metadata["delivery_rows"], 3)
        self.assertEqual(metadata["stops"], 3)

    def test_input_template_and_approved_workbook_have_expected_sheets(self):
        template = pd.ExcelFile(BytesIO(make_input_template()))
        self.assertEqual(set(template.sheet_names), {"DeliveryHistory", "Customers"})
        updated, summaries, metadata = build_zone_proposal(
            self.history, self.customers, target_zone_count=2
        )
        output = pd.ExcelFile(BytesIO(make_zone_workbook(updated, summaries, metadata)))
        self.assertEqual(
            set(output.sheet_names), {"Customers", "Zone Summary", "Build Info"}
        )


if __name__ == "__main__":
    unittest.main()
