import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, PlanInvalidated, RecoveryRequired
from src.service import SimulatedCrash


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
APPROVE = ('approve', 'repair_manager', {'repair_manager': 'RM-2'})
MOBILIZE = ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'vessel_name': 'CS-1'})


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.actor = Actor("creator", "noc_operator")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference="CABLE-30001"):
        return self.service.create(self.actor, reference, CREATE_DATA)

    def _approve(self, record):
        return self.service.execute(Actor("rm", "repair_manager"), record["id"], record["version"],
                                    APPROVE[0], APPROVE[2])

    def test_permission_denied_for_outsider(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "CABLE-30001", CREATE_DATA)

    def test_stale_version_is_rejected(self):
        record = self._create()["record"]
        record = self._approve(record)["record"]
        with self.assertRaises(Conflict):
            self.service.execute(Actor("vm", "vessel_master"), record["id"], record["version"] - 1,
                                 MOBILIZE[0], MOBILIZE[2])

    # ---- 诉求1：两位值班员同报一条故障，后到版本只留待复核 --------------------

    def test_duplicate_submission_parks_not_overwrites(self):
        first = self._create()
        self.assertEqual(first["status"], "created")
        # 后到一版：不同值班员，内容有出入
        second_data = dict(CREATE_DATA, depth_m=2100.0)
        second = self.service.create(Actor("creator2", "noc_operator"), "CABLE-30001", second_data)
        self.assertEqual(second["status"], "parked_for_review")
        self.assertIsNotNone(second["review"])
        self.assertEqual(second["review"]["status"], "pending")
        # 已生效记录原封不动（深度仍是第一版），版本没有被顶掉
        current = self.service.get_record(self.actor, first["record"]["id"])
        self.assertEqual(current["version"], 1)
        self.assertEqual(current["payload"]["depth_m"], 1800.0)
        # 审计时间线明确记录"后到版本已挂起"
        timeline = self.service.timeline(self.actor, current["id"])
        self.assertEqual(timeline[-1]["action"], "revision_parked")
        # 拒绝采纳后记录不变
        reviews = self.service.list_reviews(self.actor)
        self.assertEqual(len(reviews), 1)
        self.service.resolve_review(Actor("rm", "repair_manager"), reviews[0]["id"], False, "第一版为准")
        current = self.service.get_record(self.actor, current["id"])
        self.assertEqual(current["version"], 1)

    def test_duplicate_concurrent_submissions(self):
        barrier = threading.Barrier(2)
        results = []

        def submit(actor_id):
            barrier.wait()
            results.append(self.service.create(Actor(actor_id, "noc_operator"), "CABLE-X1", CREATE_DATA))

        t1 = threading.Thread(target=submit, args=("op-a",))
        t2 = threading.Thread(target=submit, args=("op-b",))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = sorted(item["status"] for item in results)
        self.assertEqual(statuses, ["created", "parked_for_review"])
        records = self.service.list_records(self.actor)
        self.assertEqual(len(records), 1)

    # ---- 诉求2：海况/备缆变化 → 未执行失效重算；执行中沿用原依据 -------------

    def test_environment_change_invalidates_open_plan(self):
        record = self._create()["record"]
        result = self.service.update_environment(Actor("rm", "repair_manager"), {"sea_state": 7})
        self.assertIn(record["id"], result["invalidated_record_ids"])
        record = self.service.get_record(self.actor, record["id"])
        self.assertEqual(record["payload"]["basis_status"], "invalidated")
        with self.assertRaises(PlanInvalidated):
            self._approve(record)
        # replan 后按新依据重算（海况7 → 不可行），重新成为有效版本
        outcome = self.service.execute(Actor("rm", "repair_manager"), record["id"], record["version"], "replan", {})
        record = outcome["record"]
        self.assertEqual(record["payload"]["basis_status"], "active")
        self.assertFalse(record["payload"]["repair_feasible"])
        timeline = self.service.timeline(self.actor, record["id"])
        self.assertIn("basis_invalidated", [event["action"] for event in timeline])

    def test_executing_plan_keeps_original_basis(self):
        record = self._create()["record"]
        record = self._approve(record)["record"]
        outcome = self.service.execute(Actor("vm", "vessel_master"), record["id"], record["version"], MOBILIZE[0], MOBILIZE[2])
        record = outcome["record"]
        self.assertEqual(record["payload"]["basis_status"], "frozen")
        # 海况剧变：执行中的方案不失效，备缆占用也不释放
        self.service.update_environment(Actor("rm", "repair_manager"), {"sea_state": 9})
        record = self.service.get_record(self.actor, record["id"])
        self.assertEqual(record["payload"]["basis_status"], "frozen")
        self.assertEqual(record["payload"]["basis_sea_state"], 3)
        chain = self.service.chain(self.actor, record["id"])
        self.assertEqual(chain["spare_reservation"]["status"], "held")

    # ---- 诉求3：断在半路 → 从上次完整处置恢复续做 ----------------------------

    def test_crash_after_begin_then_resume(self):
        record = self._create()["record"]
        record = self._approve(record)["record"]
        self.service.crash_after_begin = True
        with self.assertRaises(SimulatedCrash):
            self.service.execute(Actor("vm", "vessel_master"), record["id"], record["version"], MOBILIZE[0], MOBILIZE[2])
        # 检查点已存在：换一个动作/幂等键不能另起炉灶，必须先恢复
        with self.assertRaises(RecoveryRequired):
            self.service.execute(Actor("vm", "vessel_master"), record["id"], record["version"],
                                 MOBILIZE[0], MOBILIZE[2], "mobilize-retry")
        # 从断点恢复续做：动员生效、备缆占用一次
        outcome = self.service.resume(Actor("vm2", "vessel_master"), record_id=record["id"])
        self.assertTrue(outcome["resumed"])
        record = outcome["record"]
        self.assertEqual(record["state"], "mobilized")
        env = self.service.environment(self.actor)
        self.assertEqual(env["held_spare_km"], 15.75)
        chain = self.service.chain(self.actor, record["id"])
        self.assertTrue(all(step["status"] == "done" for step in chain["steps"]))
        # 恢复后再次重放同一动作：幂等返回，不重复占缆
        replay = self.service.execute(Actor("vm", "vessel_master"), record["id"], 2, MOBILIZE[0], MOBILIZE[2])
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.service.environment(self.actor)["held_spare_km"], 15.75)

    # ---- 诉求4：重放同一动作不能重复占用备缆 ---------------------------------

    def test_replay_same_action_does_not_double_hold(self):
        record = self._create()["record"]
        record = self._approve(record)["record"]
        idem = "mobilize-once"
        first = self.service.execute(Actor("vm", "vessel_master"), record["id"], record["version"],
                                    MOBILIZE[0], MOBILIZE[2], idem)
        self.assertFalse(first["replayed"])
        # 网络抖动重发同一请求（带同一幂等键）：返回原结果，不重复占缆
        replay = self.service.execute(Actor("vm", "vessel_master"), record["id"], record["version"],
                                      MOBILIZE[0], MOBILIZE[2], idem)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["record"]["version"], first["record"]["version"])
        env = self.service.environment(self.actor)
        self.assertEqual(env["held_spare_km"], 15.75)
        # 不带幂等键时，默认键含(记录,版本,动作)，重放同样幂等
        other_data = dict(CREATE_DATA, segment="S9", start_km=200.0, end_km=215.0)
        another = self.service.create(self.actor, "CABLE-30002", other_data)["record"]
        another = self._approve(another)["record"]
        a1 = self.service.execute(Actor("vm", "vessel_master"), another["id"], another["version"], MOBILIZE[0], MOBILIZE[2])
        a2 = self.service.execute(Actor("vm", "vessel_master"), another["id"], another["version"], MOBILIZE[0], MOBILIZE[2])
        self.assertTrue(a2["replayed"])
        self.assertEqual(self.service.environment(self.actor)["held_spare_km"], 31.5)

    def test_spare_capacity_blocks_mobilize(self):
        from src.domain import ValidationError
        record = self._create("CABLE-TIGHT")["record"]
        self.service.update_environment(Actor("rm", "repair_manager"), {"spare_total_km": 10.0})
        record = self.service.get_record(self.actor, record["id"])
        # 依据变化先失效，重算后备缆不足 → 方案不可行，动员在规则阶段即被拒绝
        record = self.service.execute(Actor("rm", "repair_manager"), record["id"], record["version"], "replan", {})["record"]
        record = self._approve(record)["record"]
        with self.assertRaises(ValidationError):
            self.service.execute(Actor("vm", "vessel_master"), record["id"], record["version"],
                                 MOBILIZE[0], MOBILIZE[2])
        self.assertEqual(self.service.environment(self.actor)["held_spare_km"], 0)
