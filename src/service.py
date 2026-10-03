"""业务用例编排：权限、处置链两阶段执行、环境失效与审计。

三类对象（审计时间线、故障记录、备缆余量）经由 repository 的同一事务落库；
每个业务动作先登记 prepared 意图（提交点=检查点），再完成第二阶段。
中途崩溃后凭幂等键 resume 续做；已完成的动作重放只返回原结果，不重复占缆。
"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import (
    Actor,
    Conflict,
    NotFound,
    PermissionDenied,
    ValidationError,
    text,
)
from .repository import Repository
from .rules import DomainRules


class SimulatedCrash(Exception):
    """测试用：意图已落盘、第二阶段未执行，模拟断在半路。"""


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        # 测试钩子：置 True 后下一个动作在意图提交后、完成前"崩溃"
        self.crash_after_begin = False

    # ---- 身份与权限 ----------------------------------------------------------

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    # ---- 环境台账（海况 / 备缆余量） ----------------------------------------

    def environment(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_environment()

    def update_environment(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_env(actor.role):
            raise PermissionDenied("角色无权更新海况或备缆台账")
        values = self.rules.validate_environment(payload or {})
        current = self.repository.get_environment()
        values.setdefault("sea_state", int(current["sea_state"]))
        values.setdefault("spare_total_km", float(current["spare_total_km"]))
        values.setdefault("vessel_available", bool(current["vessel_available"]))
        values.setdefault("permit_valid", bool(current["permit_valid"]))
        return self.repository.apply_environment(values, actor.user_id, self.rules.invalidate_plan)

    # ---- 创建故障记录 / 后到版本挂起 ----------------------------------------

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        reported = self.rules.validate_create(payload or {})

        existing = self.repository.get_by_reference(reference)
        if existing is None:
            env = self.repository.get_environment()
            prepared = self.rules.build_plan(reported, env, int(env["version"]), float(env["available_spare_km"]))
            self.rules.check_create_conflicts(reference, prepared, self.repository.list_records(limit=500))
            try:
                record = self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)
                return {"status": "created", "record": record, "review": None}
            except Conflict:
                # 并发窗口：两位值班员同时提交，另一个事务先插了同reference
                existing = self.repository.get_by_reference(reference)

        # 两位值班员同报一条故障：后到版本只留待复核，绝不盖掉已生效状态
        review = self.repository.add_review(existing["id"], reference, reported, actor.user_id)
        return {"status": "parked_for_review", "record": existing, "review": review}

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    # ---- 待复核版本 ----------------------------------------------------------

    def list_reviews(self, actor: Actor, status: str = "pending") -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if status not in ("pending", "accepted", "rejected"):
            raise ValidationError("status只能是pending/accepted/rejected")
        return self.repository.list_reviews(status)

    def resolve_review(self, actor: Actor, review_id: int, approve: bool, note: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review(actor.role):
            raise PermissionDenied("角色无权复核后到版本")
        review = self.repository.get_review(review_id)
        rebuilt = None
        if approve:
            record = self.repository.get(review["record_id"])
            env = self.repository.get_environment()
            # 采纳即按当前海况/备缆重算，同样不允许旧依据直接生效
            rebuilt = self.rules.build_plan(
                self.rules.validate_create(review["payload"]), env, int(env["version"]),
                float(env["available_spare_km"]),
            )
            # 保留链上已产生的管理信息
            for key in ("repair_manager",):
                if key in record["payload"]:
                    rebuilt[key] = record["payload"][key]
        return self.repository.resolve_review(review_id, approve, actor.user_id, note or "", rebuilt)

    # ---- 处置链：执行 / 恢复 / 重放 -----------------------------------------

    @staticmethod
    def _default_idem_key(record_id: int, action: str, expected_version: int) -> str:
        return "r%s-v%s-%s" % (record_id, expected_version, action)

    def execute(
        self,
        actor: Actor,
        record_id: int,
        expected_version: int,
        action: str,
        data: Optional[Dict[str, Any]] = None,
        idem_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValidationError("expected_version必须是整数")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        idem_key = (idem_key or "").strip() or self._default_idem_key(record_id, action, expected_version)
        data = data or {}

        existing = self.repository.step_by_key(idem_key)
        if existing is not None:
            if existing["record_id"] != int(record_id) or existing["action"] != action:
                raise Conflict("幂等键已用于其他处置动作")
            if existing["status"] == "superseded":
                raise Conflict("该处置动作已作废，请重新提交")
            # prepared → 断点续做；done → 重放原结果（不重复占缆）
            outcome = self.repository.complete_step(existing["id"])
            return self._outcome(outcome, resumed=existing["status"] == "prepared")

        record = self.repository.get(record_id)
        if int(record["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")

        env = self.repository.get_environment()
        spare_info = {"available_spare_km": float(env["available_spare_km"]), "env_version": int(env["version"])}
        new_state, new_payload, summary, spare_op = self.rules.apply_action(record, action, data, spare_info, env)
        prepared = {
            "from": record["state"], "to": new_state, "payload": new_payload,
            "summary": summary, "spare_op": spare_op,
        }
        try:
            step = self.repository.begin_step(
                record_id, idem_key, action, actor.user_id, expected_version, data, prepared,
            )
        except Conflict:
            # 并发的相同请求抢先登记了同一幂等键：转为重放/恢复，不报错、不重复占缆
            race = self.repository.step_by_key(idem_key)
            if race is None or race["record_id"] != int(record_id) or race["action"] != action:
                raise
            if race["status"] == "superseded":
                raise Conflict("该处置动作已作废，请重新提交")
            return self._outcome(self.repository.complete_step(race["id"]), resumed=race["status"] == "prepared")

        if self.crash_after_begin:
            # 意图检查点已提交，但第二阶段未落库——实际网络中断时就停在这里
            self.crash_after_begin = False
            raise SimulatedCrash("处置在第二阶段前中断，可用同一idem_key恢复")

        outcome = self.repository.complete_step(step["id"])
        return self._outcome(outcome, resumed=False)

    def resume(self, actor: Actor, record_id: Optional[int] = None, idem_key: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if idem_key:
            step = self.repository.step_by_key(text({"idem_key": idem_key}, "idem_key"))
            if step is None:
                raise NotFound("没有匹配的处置检查点")
        elif record_id is not None:
            step = self.repository.open_step(int(record_id))
            if step is None:
                raise NotFound("该记录没有中断在半路的处置")
        else:
            raise ValidationError("需要idem_key或record_id")
        if step["status"] == "done":
            outcome = self.repository.complete_step(step["id"])
            return self._outcome(outcome, resumed=False)
        if step["status"] == "superseded":
            raise Conflict("该处置动作已作废，请重新提交")
        outcome = self.repository.complete_step(step["id"])
        return self._outcome(outcome, resumed=True)

    @staticmethod
    def _outcome(outcome: Dict[str, Any], resumed: bool) -> Dict[str, Any]:
        return {
            "record": outcome["record"],
            "replayed": bool(outcome["replayed"]),
            "resumed": bool(resumed) and not outcome["replayed"],
            "step": {
                "seq": outcome["step"]["seq"],
                "idem_key": outcome["step"]["idem_key"],
                "action": outcome["step"]["action"],
                "status": outcome["step"]["status"],
            },
        }

    def chain(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        steps = self.repository.chain_steps(record_id)
        return {
            "record_id": record_id,
            "state": record["state"],
            "version": record["version"],
            "basis_status": record["payload"].get("basis_status"),
            "basis_env_version": record["payload"].get("basis_env_version"),
            "steps": steps,
            "spare_reservation": self.repository.spare_reservation(record_id),
        }

    # ---- 审计与统计 ----------------------------------------------------------

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 向后兼容：旧调用方式 -------------------------------------------------

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.execute(actor, record_id, expected_version, action, data)["record"]
