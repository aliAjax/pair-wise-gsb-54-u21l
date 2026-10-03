"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "detected"
CREATE_ROLES = {'noc_operator'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'test': {'noc_operator'}, 'restore': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}}
TRANSITIONS = {'approve': {'detected': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced'}, 'test': {'spliced': 'tested'}, 'restore': {'tested': 'restored'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled'}}
# 未执行方案：海况或备缆余量变化时需要失效重算的状态
PENDING_STATES = ("detected", "approved")
# 执行中方案：环境变化时沿用原依据，不重算
EXECUTING_STATES = ("mobilized", "surveyed", "spliced", "tested")
# 环境变化权限：海况与备缆余量分别由不同角色维护
ENVIRONMENT_ROLES = {"sea_state": {"metocean_officer"}, "spare_total_km": {"depot_keeper"}}
REVIEW_ROLES = {"repair_manager"}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        for roles in ENVIRONMENT_ROLES.values():
            all_roles.update(roles)
        all_roles.update(REVIEW_ROLES)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_environment(self, role: str, field: str) -> bool:
        return role == "admin" or role in ENVIRONMENT_ROLES.get(field, set())

    def role_can_review(self, role: str) -> bool:
        return role == "admin" or role in REVIEW_ROLES

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "cable")
        text(p, "segment")
        start = number(p, "start_km", 0)
        end = number(p, "end_km", 0)
        number(p, "depth_m", 1)
        integer(p, "sea_state", 0, 9)
        boolean(p, "vessel_available")
        number(p, "spare_length_km", 0)
        boolean(p, "permit_valid")
        integer(p, "capacity_gbps", 1)
        if end <= start:
            raise ValidationError("结束里程必须大于开始里程")
        return p

    def recompute_plan(self, payload: Dict[str, Any], sea_state: int, spare_available_km: float) -> Dict[str, Any]:
        """按给定海况与备缆余量重算方案派生字段，是失效重算的唯一入口。"""
        p = dict(payload)
        sea_state = int(sea_state)
        distance = round(float(p["end_km"]) - float(p["start_km"]), 2)
        required = round(distance * 1.05, 2)
        p["sea_state"] = sea_state
        p["repair_distance_km"] = distance
        p["required_spare_km"] = required
        p["estimated_repair_hours"] = round(distance / 2.0 + float(p["depth_m"]) / 100.0 + sea_state * 2.0, 2)
        p["repair_feasible"] = bool(
            p.get("vessel_available")
            and p.get("permit_valid")
            and float(p.get("spare_length_km", 0)) >= required
            and float(spare_available_km) >= required
            and sea_state <= 5
        )
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        return self.recompute_plan(p, int(p["sea_state"]), float(p["spare_length_km"]))

    def plan_material_changed(self, old_payload: Dict[str, Any], new_payload: Dict[str, Any], old_state: str, new_state: str) -> bool:
        """判断重算结果是否有实质变化，避免时间线被无意义版本刷屏。"""
        keys = ("plan_status", "repair_feasible", "estimated_repair_hours", "sea_state", "required_spare_km")
        return old_state != new_state or any(old_payload.get(key) != new_payload.get(key) for key in keys)

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"restored", "cancelled"} or item["payload"].get("cable") != payload.get("cable") or item["payload"].get("segment") != payload.get("segment"):
                continue
            if float(payload["start_km"]) < float(item["payload"].get("end_km", 0)) and float(payload["end_km"]) > float(item["payload"].get("start_km", 0)):
                raise Conflict("同一光缆区段已有未结束抢修")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "approve":
            if p.get("plan_status") == "invalidated":
                raise ValidationError("方案已失效，待海况或备缆恢复重算后重新批准")
            if not bool(p["permit_valid"]) or not bool(p["vessel_available"]):
                raise ValidationError("许可或船舶条件不满足")
            changes["repair_manager"] = text(data, "repair_manager")
            summary = "抢修方案已批准"
        elif action == "mobilize":
            if p.get("plan_status") == "invalidated":
                raise ValidationError("方案已失效，不能动员")
            if float(data.get("weather_window_hours", 0)) < float(p["estimated_repair_hours"]):
                raise ValidationError("海况窗口不足以完成抢修")
            if float(data.get("available_spare_km", 0)) < float(p["required_spare_km"]):
                raise ValidationError("船上备缆不足")
            changes["weather_window_hours"] = float(data["weather_window_hours"])
            changes["vessel_name"] = text(data, "vessel_name")
            summary = "抢修船已动员"
        elif action == "survey":
            if not boolean(data, "survey_complete"):
                raise ValidationError("勘察尚未完成")
            fault_km = number(data, "fault_location_km", 0)
            if not (float(p["start_km"]) <= fault_km <= float(p["end_km"])):
                raise ValidationError("故障点不在申报区段")
            changes["fault_location_km"] = fault_km
            summary = "故障点勘察完成"
        elif action == "splice":
            loss = number(data, "splice_loss_db", 0)
            if loss > 0.2:
                raise ValidationError("接续损耗超过阈值")
            if float(data.get("spare_used_km", 0)) < float(p["repair_distance_km"]):
                raise ValidationError("备缆使用长度不足")
            changes["splice_loss_db"] = loss
            changes["spare_used_km"] = float(data["spare_used_km"])
            summary = "光缆接续完成"
        elif action == "test":
            end_loss = number(data, "end_to_end_loss_db", 0)
            if end_loss > 0.5:
                raise ValidationError("端到端损耗不合格")
            changes["end_to_end_loss_db"] = end_loss
            changes["test_passed"] = True
            summary = "系统测试通过"
        elif action == "restore":
            if not boolean(data, "traffic_restored"):
                raise ValidationError("业务流量尚未恢复")
            changes["traffic_restored"] = True
            changes["restore_capacity_gbps"] = integer(data, "restore_capacity_gbps", 1)
            summary = "通信恢复"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "抢修取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
