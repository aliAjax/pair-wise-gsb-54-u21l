"""业务用例编排、权限检查与审计。

处置链一致性：记录状态、审计时间线、备缆库存与基线快照在同一事务内提交；
动作先落日志再执行，重放返回存档结果，崩溃后从上次完整处置续做。
"""
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, choice, integer, number, text
from .repository import Repository
from .rules import EXECUTING_STATES, PENDING_STATES, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    # ---------- 记录创建与后到版本 ----------

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        existing = self.repository.find_by_reference(reference)
        if existing is not None:
            return self._stash_revision(existing, prepared, actor)
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        stamped = self._stamp_basis(prepared)
        try:
            return self.repository.create(reference, self.rules.INITIAL_STATE, stamped, actor.user_id)
        except Conflict:
            # 并发下同 reference 被抢先创建：后到的一版转入待复核，不覆盖已生效状态
            existing = self.repository.find_by_reference(reference)
            return self._stash_revision(existing, prepared, actor)

    def _stamp_basis(self, prepared: Dict[str, Any]) -> Dict[str, Any]:
        basis = self.repository.current_basis()
        stamped = self.rules.recompute_plan(prepared, basis["sea_state"], basis["spare_available_km"])
        stamped["basis_id"] = basis["id"]
        stamped["plan_status"] = "valid" if stamped["repair_feasible"] else "invalidated"
        return stamped

    def _stash_revision(self, existing: Dict[str, Any], prepared: Dict[str, Any], actor: Actor) -> Dict[str, Any]:
        revision = self.repository.add_revision(existing["id"], prepared, actor.user_id)
        self.audit.note(
            existing["id"],
            actor.user_id,
            "revision_submitted",
            {"revision_id": revision["id"], "summary": "同一故障记录的后到提交，已留待复核，未覆盖已生效状态", "effective_version": existing["version"]},
        )
        return {
            "status": "pending_review",
            "record_id": existing["id"],
            "revision_id": revision["id"],
            "effective_version": existing["version"],
            "effective_state": existing["state"],
        }

    def list_revisions(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_revisions(record_id)

    def review_revision(self, actor: Actor, record_id: int, revision_id: int, decision: str, note: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review(actor.role):
            raise PermissionDenied("角色无权复核后到版本")
        decision = choice({"decision": decision}, "decision", ["accept", "reject"])
        record = self.repository.get(record_id)
        revision = self.repository.get_revision(record_id, revision_id)
        if revision["status"] != "pending_review":
            raise Conflict("该版本已复核")
        if decision == "reject":
            with self.repository.tx() as connection:
                self.repository.tx_finish_revision(connection, revision_id, "rejected", actor.user_id, note)
                self.repository.tx_insert_audit(
                    connection, record_id, "revision_rejected", actor.user_id, record["version"],
                    {"revision_id": revision_id, "note": note, "summary": "后到版本复核不通过，已留档"},
                )
            return {"revision": self.repository.get_revision(record_id, revision_id), "record": self.repository.get(record_id)}
        if record["state"] != "detected":
            raise Conflict("记录已推进到处置链后续环节，后到版本只能留档或拒绝")
        others = [item for item in self.repository.list_records(limit=500) if item["id"] != record_id]
        self.rules.check_create_conflicts(revision["payload"], others)
        stamped = self._stamp_basis(revision["payload"])
        with self.repository.tx() as connection:
            updated = self.repository.tx_update_record(connection, record_id, record["version"], "detected", stamped, actor.user_id)
            self.repository.tx_finish_revision(connection, revision_id, "accepted", actor.user_id, note)
            self.repository.tx_insert_audit(
                connection, record_id, "revision_accepted", actor.user_id, updated["version"],
                {"revision_id": revision_id, "note": note, "basis_id": stamped["basis_id"], "plan_status": stamped["plan_status"], "summary": "后到版本复核通过，替换原申报内容"},
            )
        return {"revision": self.repository.get_revision(record_id, revision_id), "record": updated}

    # ---------- 处置动作（幂等 + 单事务处置链） ----------

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any], action_id: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        data = data or {}
        action_id = (action_id or "").strip() or uuid.uuid4().hex
        request = {"action": action, "expected_version": int(expected_version), "data": data}
        entry = self.repository.journal_entry(action_id)
        if entry is not None:
            if entry["request"] != request:
                raise Conflict("action_id与原始请求不一致")
            if entry["status"] == "committed":
                # 重放同一动作：直接返回存档结果，不重复执行、不重复占用备缆
                return entry["result"]
        record = self.repository.get(record_id)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data)
        self.repository.journal_start(action_id, record_id, action, actor.user_id, request)
        try:
            with self.repository.tx() as connection:
                inventory_changed = self._apply_inventory(connection, action, record_id, new_payload, action_id, data)
                updated = self.repository.tx_update_record(connection, record_id, int(expected_version), new_state, new_payload, actor.user_id)
                snapshot = None
                reevaluated: List[Dict[str, Any]] = []
                if inventory_changed:
                    snapshot, reevaluated = self._reevaluate_locked(connection, actor, cause="action:%s" % action, record_id=record_id)
                details: Dict[str, Any] = {
                    "summary": summary,
                    "input": data,
                    "from": record["state"],
                    "to": new_state,
                    "action_id": action_id,
                    "basis_id": new_payload.get("basis_id"),
                    "plan_status": new_payload.get("plan_status"),
                }
                if snapshot is not None:
                    details["snapshot_id"] = snapshot["id"]
                    details["inventory"] = {
                        "total_km": snapshot["spare_total_km"],
                        "reserved_km": snapshot["spare_reserved_km"],
                        "used_km": snapshot["spare_used_km"],
                        "available_km": snapshot["spare_available_km"],
                    }
                if reevaluated:
                    details["reevaluated"] = reevaluated
                self.repository.tx_insert_audit(connection, record_id, action, actor.user_id, updated["version"], details, action_id=action_id)
                self.repository.tx_journal_finish(connection, action_id, "committed", updated)
        except Exception:
            self.repository.journal_finish(action_id, "rolled_back", None)
            raise
        return updated

    def _apply_inventory(self, connection: Any, action: str, record_id: int, new_payload: Dict[str, Any], action_id: str, data: Dict[str, Any]) -> bool:
        """动作对应的备缆占用：批准锁定、接续核销、取消退回。"""
        if action == "approve":
            self.repository.tx_reserve(connection, record_id, action_id, float(new_payload["required_spare_km"]))
            return True
        if action == "splice":
            self.repository.tx_consume(connection, record_id, float(data.get("spare_used_km", 0)))
            return True
        if action == "cancel":
            return self.repository.tx_release(connection, record_id) > 0
        return False

    # ---------- 失效重算 ----------

    def _reevaluate_locked(self, connection: Any, actor: Actor, cause: str, reason: str = "", record_id: Optional[int] = None, note_executing: bool = False) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """海况或备缆余量变化后：未执行方案失效重算，执行中方案沿用原依据。

        必须在 tx() 内调用；返回 (新基线快照, 发生实质变化的记录摘要)。
        """
        environment = self.repository.tx_environment(connection)
        pendings = self.repository.tx_list_records_by_states(connection, PENDING_STATES, exclude_id=record_id)
        plans = []
        for pending in pendings:
            self.repository.tx_release(connection, pending["id"])
            available = self.repository.tx_inventory(connection)["available_km"]
            new_payload = self.rules.recompute_plan(pending["payload"], environment["sea_state"], available)
            new_state = pending["state"]
            if new_payload["repair_feasible"]:
                new_payload["plan_status"] = "valid"
                if new_state == "approved":
                    try:
                        self.repository.tx_reserve(connection, pending["id"], "reeval", new_payload["required_spare_km"])
                    except ValidationError:
                        new_payload["plan_status"] = "invalidated"
                        new_state = "detected"
            else:
                new_payload["plan_status"] = "invalidated"
                if new_state == "approved":
                    new_state = "detected"
            plans.append((pending, new_state, new_payload))
        snapshot = self.repository.tx_insert_snapshot(connection, actor.user_id, cause, reason, record_id=record_id)
        changed: List[Dict[str, Any]] = []
        for pending, new_state, new_payload in plans:
            old_payload = pending["payload"]
            if not self.rules.plan_material_changed(old_payload, new_payload, pending["state"], new_state):
                continue
            new_payload["basis_id"] = snapshot["id"]
            updated = self.repository.tx_update_record(connection, pending["id"], pending["version"], new_state, new_payload, actor.user_id)
            old_status = old_payload.get("plan_status", "valid")
            new_status = new_payload["plan_status"]
            if new_status == "invalidated" and old_status != "invalidated":
                event = "plan_invalidated"
                summary = "海况或备缆余量变化，未执行方案失效重算"
            elif new_status == "valid" and old_status == "invalidated":
                event = "plan_revalidated"
                summary = "海况或备缆余量恢复，方案重算后重新生效"
            else:
                event = "plan_recalculated"
                summary = "海况或备缆余量变化，未执行方案已按最新依据重算"
            details = {
                "summary": summary,
                "cause": cause,
                "reason": reason,
                "state_from": pending["state"],
                "state_to": new_state,
                "plan_status_from": old_status,
                "plan_status_to": new_status,
                "basis_id": snapshot["id"],
                "snapshot_id": snapshot["id"],
                "repair_feasible": new_payload["repair_feasible"],
                "sea_state": new_payload["sea_state"],
                "estimated_repair_hours": new_payload["estimated_repair_hours"],
                "inventory": {
                    "total_km": snapshot["spare_total_km"],
                    "reserved_km": snapshot["spare_reserved_km"],
                    "used_km": snapshot["spare_used_km"],
                    "available_km": snapshot["spare_available_km"],
                },
            }
            self.repository.tx_insert_audit(connection, pending["id"], event, actor.user_id, updated["version"], details)
            changed.append({"record_id": pending["id"], "state": new_state, "plan_status": new_status, "event": event})
        if note_executing:
            for executing in self.repository.tx_list_records_by_states(connection, EXECUTING_STATES, exclude_id=record_id):
                self.repository.tx_insert_audit(
                    connection,
                    executing["id"],
                    "basis_kept",
                    actor.user_id,
                    executing["version"],
                    {
                        "summary": "海况或备缆余量变化，执行中方案沿用原依据",
                        "cause": cause,
                        "reason": reason,
                        "basis_id": executing["payload"].get("basis_id"),
                        "new_snapshot_id": snapshot["id"],
                        "plan_status": executing["payload"].get("plan_status"),
                    },
                )
        return snapshot, changed

    # ---------- 环境变化 ----------

    def update_environment(self, actor: Actor, sea_state: Optional[int] = None, spare_total_km: Optional[float] = None, reason: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if sea_state is None and spare_total_km is None:
            raise ValidationError("至少提供一项环境变化")
        if sea_state is not None:
            if not self.rules.role_can_environment(actor.role, "sea_state"):
                raise PermissionDenied("角色无权更新海况")
            sea_state = integer({"sea_state": sea_state}, "sea_state", 0, 9)
        if spare_total_km is not None:
            if not self.rules.role_can_environment(actor.role, "spare_total_km"):
                raise PermissionDenied("角色无权更新备缆总量")
            spare_total_km = number({"spare_total_km": spare_total_km}, "spare_total_km", 0)
        reason = text({"reason": reason}, "reason") if str(reason or "").strip() else ""
        with self.repository.tx() as connection:
            if sea_state is not None:
                self.repository.tx_set_sea_state(connection, sea_state)
            if spare_total_km is not None:
                self.repository.tx_set_inventory_total(connection, spare_total_km)
            snapshot, reevaluated = self._reevaluate_locked(connection, actor, cause="environment_update", reason=reason, note_executing=True)
        return {
            "environment": self.repository.environment_view(),
            "inventory": self.repository.inventory_view(),
            "snapshot": snapshot,
            "reevaluated": reevaluated,
        }

    def environment(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.environment_view()

    def inventory(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.inventory_view()

    # ---------- 断点恢复 ----------

    def recover(self, actor: Actor) -> Dict[str, Any]:
        """ reconcile 动作日志：started 孤儿按有无落库效果分别补齐或回滚标记。

        处置链写入是单事务的，恢复后系统停留在上次完整处置，可安全续做。
        """
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        report: Dict[str, Any] = {"scanned": 0, "committed": [], "rolled_back": []}
        for entry in self.repository.journal_started():
            report["scanned"] += 1
            if self.repository.audit_has_action_id(entry["record_id"], entry["action_id"]):
                record = self.repository.get(entry["record_id"])
                self.repository.journal_finish(entry["action_id"], "committed", record)
                report["committed"].append(entry["action_id"])
            else:
                self.repository.journal_finish(entry["action_id"], "rolled_back", None)
                report["rolled_back"].append(entry["action_id"])
        return report

    # ---------- 只读查询 ----------

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
