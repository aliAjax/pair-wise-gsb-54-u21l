"""处置链一致性测试：待复核、失效重算、断点恢复与幂等重放。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


NOC = Actor("noc-1", "noc_operator")
NOC2 = Actor("noc-2", "noc_operator")
RM = Actor("rm-1", "repair_manager")
VM = Actor("vm-1", "vessel_master")
CE = Actor("ce-1", "cable_engineer")
MET = Actor("met-1", "metocean_officer")
DEPOT = Actor("depot-1", "depot_keeper")

APPROVE = {"repair_manager": "RM-2"}
MOBILIZE = {"weather_window_hours": 40, "available_spare_km": 18, "vessel_name": "CS-1"}
SURVEY = {"survey_complete": True, "fault_location_km": 128}
SPLICE = {"splice_loss_db": 0.12, "spare_used_km": 16}


def make_data(segment="S3", start=120.0, end=135.0):
    return {"cable": "SEA-1", "segment": segment, "start_km": start, "end_km": end, "depth_m": 1800.0, "sea_state": 3, "vessel_available": True, "spare_length_km": 20.0, "permit_valid": True, "capacity_gbps": 400}


class DisposalChainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _get(self, record_id):
        return self.service.get_record(NOC, record_id)

    def _inventory(self):
        return self.service.inventory(NOC)

    def test_concurrent_submit_keeps_pending_review(self):
        record = self.service.create(NOC, "CABLE-40001", make_data())
        changed = make_data()
        changed["depth_m"] = 2000.0
        later = self.service.create(NOC2, "CABLE-40001", changed)
        self.assertEqual(later["status"], "pending_review")
        self.assertEqual(later["record_id"], record["id"])
        # 已生效状态不被后到版本覆盖
        current = self._get(record["id"])
        self.assertEqual(current["version"], 1)
        self.assertEqual(current["payload"]["depth_m"], 1800.0)
        revisions = self.service.list_revisions(NOC, record["id"])
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["status"], "pending_review")
        # 普通值班员无权复核
        with self.assertRaises(PermissionDenied):
            self.service.review_revision(NOC, record["id"], revisions[0]["id"], "accept")
        # 复核通过：替换申报内容并留痕
        result = self.service.review_revision(RM, record["id"], revisions[0]["id"], "accept", "以第二版水深为准")
        self.assertEqual(result["record"]["payload"]["depth_m"], 2000.0)
        self.assertEqual(result["record"]["version"], 2)
        actions = [event["action"] for event in self.service.timeline(NOC, record["id"])]
        self.assertIn("revision_submitted", actions)
        self.assertIn("revision_accepted", actions)
        # 重复复核被拒绝
        with self.assertRaises(Conflict):
            self.service.review_revision(RM, record["id"], revisions[0]["id"], "accept")
        # 记录推进后，后到版本只能留档或拒绝
        approved = self.service.act(RM, record["id"], 2, "approve", APPROVE)
        third = self.service.create(NOC2, "CABLE-40001", make_data())
        with self.assertRaises(Conflict):
            self.service.review_revision(RM, record["id"], third["revision_id"], "accept")
        rejected = self.service.review_revision(RM, record["id"], third["revision_id"], "reject", "已批准方案不变")
        self.assertEqual(rejected["revision"]["status"], "rejected")
        self.assertEqual(self._get(record["id"])["state"], approved["state"])

    def test_sea_state_invalidates_pending_but_keeps_executing(self):
        record_a = self.service.create(NOC, "CABLE-40011", make_data("S3", 120.0, 135.0))
        record_b = self.service.create(NOC, "CABLE-40012", make_data("S4", 140.0, 155.0))
        record_c = self.service.create(NOC, "CABLE-40013", make_data("S5", 160.0, 175.0))
        record_b = self.service.act(RM, record_b["id"], record_b["version"], "approve", APPROVE)
        record_c = self.service.act(RM, record_c["id"], record_c["version"], "approve", APPROVE)
        record_c = self.service.act(VM, record_c["id"], record_c["version"], "mobilize", MOBILIZE)
        # 海况转劣：未执行方案失效重算，执行中方案沿用原依据
        result = self.service.update_environment(MET, sea_state=8, reason="台风外围影响")
        self.assertEqual({item["record_id"] for item in result["reevaluated"]}, {record_a["id"], record_b["id"]})
        a_now = self._get(record_a["id"])
        self.assertEqual(a_now["payload"]["plan_status"], "invalidated")
        self.assertEqual(a_now["payload"]["sea_state"], 8)
        b_now = self._get(record_b["id"])
        self.assertEqual(b_now["payload"]["plan_status"], "invalidated")
        self.assertEqual(b_now["state"], "detected")  # 已批准方案失效退回
        c_now = self._get(record_c["id"])
        self.assertEqual(c_now["state"], "mobilized")
        self.assertEqual(c_now["payload"]["sea_state"], 3)  # 执行中沿用原依据
        self.assertEqual(c_now["payload"]["plan_status"], "valid")
        c_actions = [event["action"] for event in self.service.timeline(NOC, record_c["id"])]
        self.assertIn("basis_kept", c_actions)
        b_actions = [event["action"] for event in self.service.timeline(NOC, record_b["id"])]
        self.assertIn("plan_invalidated", b_actions)
        # B 的预留被释放，只剩执行中 C 的预留
        self.assertEqual(self._inventory()["reserved_km"], 15.75)
        # 失效方案不能批准
        with self.assertRaises(ValidationError):
            self.service.act(RM, record_a["id"], a_now["version"], "approve", APPROVE)
        # 海况恢复：未执行方案重算后重新生效
        self.service.update_environment(MET, sea_state=3, reason="风浪减弱")
        self.assertEqual(self._get(record_a["id"])["payload"]["plan_status"], "valid")
        b_restored = self._get(record_b["id"])
        self.assertEqual(b_restored["payload"]["plan_status"], "valid")
        self.assertEqual(b_restored["state"], "detected")
        reapproved = self.service.act(RM, record_b["id"], b_restored["version"], "approve", APPROVE)
        self.assertEqual(reapproved["state"], "approved")

    def test_spare_total_change_triggers_recalculation(self):
        record_a = self.service.create(NOC, "CABLE-40021", make_data("S3"))
        record_a = self.service.act(RM, record_a["id"], record_a["version"], "approve", APPROVE)
        record_b = self.service.create(NOC, "CABLE-40022", make_data("S4", 140.0, 155.0))
        self.assertEqual(record_b["payload"]["plan_status"], "valid")
        # 备缆总量核减到仅够已预留：B 失效
        self.service.update_environment(DEPOT, spare_total_km=16.0, reason="盘点核减")
        self.assertEqual(self._get(record_b["id"])["payload"]["plan_status"], "invalidated")
        # 总量不能低于已占用
        with self.assertRaises(ValidationError):
            self.service.update_environment(DEPOT, spare_total_km=10.0, reason="非法核减")
        # 补缆到库：B 重算后恢复
        self.service.update_environment(DEPOT, spare_total_km=100.0, reason="补缆到库")
        self.assertEqual(self._get(record_b["id"])["payload"]["plan_status"], "valid")

    def test_reservation_blocks_second_approval_and_release_revalidates(self):
        self.service.update_environment(DEPOT, spare_total_km=20.0, reason="小库容场景")
        record_a = self.service.create(NOC, "CABLE-40031", make_data("S3"))
        record_a = self.service.act(RM, record_a["id"], record_a["version"], "approve", APPROVE)
        self.assertEqual(self._inventory()["reserved_km"], 15.75)
        # 余量不足，B 创建即失效，批准被拒绝
        record_b = self.service.create(NOC, "CABLE-40032", make_data("S4", 140.0, 155.0))
        self.assertEqual(record_b["payload"]["plan_status"], "invalidated")
        with self.assertRaises(ValidationError):
            self.service.act(RM, record_b["id"], record_b["version"], "approve", APPROVE)
        # A 取消释放备缆，B 自动重算恢复
        self.service.act(RM, record_a["id"], record_a["version"], "cancel", {"cancel_reason": "海况改期"})
        b_now = self._get(record_b["id"])
        self.assertEqual(b_now["payload"]["plan_status"], "valid")
        approved_b = self.service.act(RM, record_b["id"], b_now["version"], "approve", APPROVE)
        self.assertEqual(approved_b["state"], "approved")
        self.assertEqual(self._inventory()["reserved_km"], 15.75)

    def test_idempotent_replay_does_not_double_reserve(self):
        record = self.service.create(NOC, "CABLE-40041", make_data())
        first = self.service.act(RM, record["id"], 1, "approve", APPROVE, action_id="approve-1")
        self.assertEqual(first["version"], 2)
        self.assertEqual(self._inventory()["reserved_km"], 15.75)
        # 网络抖动后重放同一动作：返回存档结果，备缆不重复占用
        replay = self.service.act(RM, record["id"], 1, "approve", APPROVE, action_id="approve-1")
        self.assertEqual(replay["version"], 2)
        self.assertEqual(self._inventory()["reserved_km"], 15.75)
        # 同一 action_id 携带不同请求被拒绝
        with self.assertRaises(Conflict):
            self.service.act(RM, record["id"], 1, "approve", {"repair_manager": "RM-9"}, action_id="approve-1")
        # 记录推进后重放仍返回存档结果，不影响当前状态
        self.service.act(VM, record["id"], 2, "mobilize", MOBILIZE, action_id="mobilize-1")
        replay2 = self.service.act(RM, record["id"], 1, "approve", APPROVE, action_id="approve-1")
        self.assertEqual(replay2["version"], 2)
        self.assertEqual(self._get(record["id"])["state"], "mobilized")
        self.assertEqual(self._inventory()["reserved_km"], 15.75)

    def test_crash_recovery_and_resume(self):
        record = self.service.create(NOC, "CABLE-40051", make_data())
        # 模拟断在半路：动作日志已落 started，业务效果未落库
        self.service.repository.journal_start("act-x", record["id"], "approve", "rm-1", {"action": "approve", "expected_version": 1, "data": APPROVE})
        report = self.service.recover(RM)
        self.assertIn("act-x", report["rolled_back"])
        self.assertEqual(self._get(record["id"])["state"], "detected")  # 停在上次完整处置
        # 从上次完整处置续做：同一 action_id 正常执行
        resumed = self.service.act(RM, record["id"], 1, "approve", APPROVE, action_id="act-x")
        self.assertEqual(resumed["state"], "approved")
        self.assertEqual(self._inventory()["reserved_km"], 15.75)
        replay = self.service.act(RM, record["id"], 1, "approve", APPROVE, action_id="act-x")
        self.assertEqual(replay["version"], resumed["version"])
        self.assertEqual(self._inventory()["reserved_km"], 15.75)
        # 规则校验失败的动作不留垃圾，记录保持上次完整处置
        with self.assertRaises(ValidationError):
            self.service.act(VM, record["id"], 2, "mobilize", {"weather_window_hours": 1, "available_spare_km": 18, "vessel_name": "CS-1"}, action_id="mob-bad")
        self.assertIsNone(self.service.repository.journal_entry("mob-bad"))
        self.assertEqual(self._get(record["id"])["state"], "approved")
        # 版本冲突的写入整体回滚，日志标记 rolled_back，可换新键续做
        with self.assertRaises(Conflict):
            self.service.act(VM, record["id"], 99, "mobilize", MOBILIZE, action_id="mob-stale")
        self.assertEqual(self.service.repository.journal_entry("mob-stale")["status"], "rolled_back")
        done = self.service.act(VM, record["id"], 2, "mobilize", MOBILIZE, action_id="mob-good")
        self.assertEqual(done["state"], "mobilized")

    def test_splice_consumes_spare_and_timeline_carries_inventory(self):
        record = self.service.create(NOC, "CABLE-40061", make_data())
        record = self.service.act(RM, record["id"], record["version"], "approve", APPROVE, action_id="a-1")
        self.assertEqual(self._inventory()["reserved_km"], 15.75)
        record = self.service.act(VM, record["id"], record["version"], "mobilize", MOBILIZE, action_id="m-1")
        record = self.service.act(CE, record["id"], record["version"], "survey", SURVEY, action_id="s-1")
        record = self.service.act(CE, record["id"], record["version"], "splice", SPLICE, action_id="p-1")
        inventory = self._inventory()
        self.assertEqual(inventory["reserved_km"], 0.0)
        self.assertEqual(inventory["used_km"], 16.0)
        self.assertEqual(inventory["available_km"], 1000.0 - 16.0)
        timeline = self.service.timeline(NOC, record["id"])
        splice_event = [event for event in timeline if event["action"] == "splice"][0]
        self.assertEqual(splice_event["details"]["action_id"], "p-1")
        self.assertEqual(splice_event["details"]["inventory"]["used_km"], 16.0)
        self.assertIn("snapshot_id", splice_event["details"])

    def test_environment_roles(self):
        with self.assertRaises(PermissionDenied):
            self.service.update_environment(NOC, sea_state=4, reason="越权")
        with self.assertRaises(PermissionDenied):
            self.service.update_environment(MET, spare_total_km=500.0, reason="越权")
        with self.assertRaises(PermissionDenied):
            self.service.update_environment(DEPOT, sea_state=4, reason="越权")
        with self.assertRaises(ValidationError):
            self.service.update_environment(MET, reason="空变化")
        result = self.service.update_environment(MET, sea_state=4, reason="例行更新")
        self.assertEqual(result["environment"]["sea_state"], 4)
        result = self.service.update_environment(DEPOT, spare_total_km=1200.0, reason="补缆")
        self.assertEqual(result["inventory"]["total_km"], 1200.0)


if __name__ == "__main__":
    unittest.main()
