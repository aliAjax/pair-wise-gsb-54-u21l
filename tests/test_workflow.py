import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
FLOW = [('approve', 'repair_manager', {'repair_manager': 'RM-2'}, 'approved'), ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'vessel_name': 'CS-1'}, 'mobilized'), ('survey', 'cable_engineer', {'survey_complete': True, 'fault_location_km': 128}, 'surveyed'), ('splice', 'cable_engineer', {'splice_loss_db': 0.12, 'spare_used_km': 16}, 'spliced'), ('test', 'noc_operator', {'end_to_end_loss_db': 0.3}, 'tested'), ('restore', 'noc_operator', {'traffic_restored': True, 'restore_capacity_gbps': 400}, 'restored')]


def create(service, actor, reference, data=None):
    return service.create(actor, reference, data or CREATE_DATA)["record"]


def execute(service, actor, record, action, role, data=None, idem_key=None):
    outcome = service.execute(
        actor, record["id"], record["version"], action, data or {}, idem_key,
    )
    return outcome["record"]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = create(self.service, Actor("creator", "noc_operator"), "CABLE-30001")
        self.assertEqual(record["state"], "detected")
        for action, role, data, expected_state in FLOW:
            record = execute(self.service, Actor("operator", role), record, action, role, data)
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "noc_operator"), record["id"])
        self.assertEqual(len(timeline), len(FLOW) + 1)
        self.assertEqual(timeline[-1]["action"], FLOW[-1][0])

    def test_chain_groups_record_audit_and_spare(self):
        record = create(self.service, Actor("creator", "noc_operator"), "CABLE-30002")
        for action, role, data, _ in FLOW:
            record = execute(self.service, Actor("operator", role), record, action, role, data)
        chain = self.service.chain(Actor("creator", "noc_operator"), record["id"])
        self.assertEqual([step["action"] for step in chain["steps"]], [item[0] for item in FLOW])
        self.assertTrue(all(step["status"] == "done" for step in chain["steps"]))
        # 动员占用、恢复归还，余量最终完整
        self.assertEqual(chain["spare_reservation"]["status"], "released")
        env = self.service.environment(Actor("creator", "noc_operator"))
        self.assertEqual(env["held_spare_km"], 0)
        self.assertEqual(env["available_spare_km"], env["spare_total_km"])
