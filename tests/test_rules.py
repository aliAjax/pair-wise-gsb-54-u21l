import unittest

from src.domain import ValidationError
from src.rules import DomainRules


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
ENV = {'sea_state': 3, 'vessel_available': True, 'permit_valid': True}
FLOW = [('approve', 'repair_manager', {'repair_manager': 'RM-2'}, 'approved'), ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}, 'mobilized'), ('survey', 'cable_engineer', {'survey_complete': True, 'fault_location_km': 128}, 'surveyed'), ('splice', 'cable_engineer', {'splice_loss_db': 0.12, 'spare_used_km': 16}, 'spliced'), ('test', 'noc_operator', {'end_to_end_loss_db': 0.3}, 'tested'), ('restore', 'noc_operator', {'traffic_restored': True, 'restore_capacity_gbps': 400}, 'restored')]


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_build_plan_from_environment(self):
        reported = self.rules.validate_create(CREATE_DATA)
        plan = self.rules.build_plan(reported, ENV, env_version=2, available_spare_km=80.0)
        self.assertEqual(plan["repair_distance_km"], 15.0)
        self.assertEqual(plan["required_spare_km"], 15.75)
        self.assertEqual(plan["spare_length_km"], 80.0)
        self.assertEqual(plan["basis_env_version"], 2)
        self.assertEqual(plan["basis_status"], "active")
        self.assertTrue(plan["repair_feasible"])

    def test_action_calculation(self):
        action, role, data, expected_state = FLOW[0]
        plan = self.rules.build_plan(self.rules.validate_create(CREATE_DATA), ENV, 1, 80.0)
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": plan}
        state, payload, summary, spare_op = self.rules.apply_action(record, action, data)
        self.assertEqual(state, expected_state)
        self.assertEqual(payload["repair_manager"], "RM-2")
        self.assertEqual(spare_op, {})

    def test_mobilize_holds_spare_and_freezes_basis(self):
        plan = self.rules.build_plan(self.rules.validate_create(CREATE_DATA), ENV, 1, 80.0)
        plan["repair_manager"] = "RM-2"
        record = {"id": 1, "state": "approved", "payload": plan}
        state, payload, summary, spare_op = self.rules.apply_action(
            record, "mobilize", {"weather_window_hours": 40, "vessel_name": "CS-1"},
            spare_info={"available_spare_km": 80.0, "env_version": 1},
        )
        self.assertEqual(state, "mobilized")
        self.assertEqual(spare_op, {"hold": 15.75})
        self.assertEqual(payload["basis_status"], "frozen")

    def test_environment_change_invalidates_only_open_plan(self):
        plan = self.rules.build_plan(self.rules.validate_create(CREATE_DATA), ENV, 1, 80.0)
        invalid, changed = self.rules.invalidate_plan(plan, 2)
        self.assertTrue(changed)
        self.assertEqual(invalid["basis_status"], "invalidated")
        frozen = dict(plan, basis_status="frozen")
        _, changed_frozen = self.rules.invalidate_plan(frozen, 2)
        self.assertFalse(changed_frozen)

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["sea_state"] = 12
        with self.assertRaises(ValidationError):
            self.rules.validate_create(invalid)
