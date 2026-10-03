"""跨海光缆故障与抢修协调领域规则与状态转换。

处置链以故障记录为根，依据快照（海况/备缆版本）冻结在动员那一刻：
未执行方案在依据变化后失效，必须重算；执行中始终沿用原依据。
"""
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import (
    Conflict,
    PlanInvalidated,
    ValidationError,
    boolean,
    integer,
    number,
    text,
)


INITIAL_STATE = "detected"

# 未执行阶段：依据一旦变化即失效；执行阶段：冻结原依据，不再受海况/备缆影响。
PRE_EXECUTION_STATES = {"detected", "approved"}
EXECUTING_STATES = {"mobilized", "surveyed", "spliced", "tested"}
TERMINAL_STATES = {"restored", "cancelled"}
OPEN_STATES = PRE_EXECUTION_STATES | EXECUTING_STATES

CREATE_ROLES = {'noc_operator'}
ENV_ROLES = {'noc_operator', 'repair_manager'}
REVIEW_ROLES = {'repair_manager'}
ACTION_ROLES = {
    'approve': {'repair_manager'},
    'mobilize': {'vessel_master'},
    'survey': {'cable_engineer'},
    'splice': {'cable_engineer'},
    'test': {'noc_operator'},
    'restore': {'noc_operator', 'repair_manager'},
    'cancel': {'repair_manager'},
    'replan': {'noc_operator', 'repair_manager'},
}
TRANSITIONS = {
    'approve': {'detected': 'approved'},
    'mobilize': {'approved': 'mobilized'},
    'survey': {'mobilized': 'surveyed'},
    'splice': {'surveyed': 'spliced'},
    'test': {'spliced': 'tested'},
    'restore': {'tested': 'restored'},
    'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled'},
    'replan': {'detected': 'detected', 'approved': 'approved'},
}
# 这两项变化会使未执行方案失效
SENSITIVE_ENV_FIELDS = ("sea_state", "spare_total_km")


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    PRE_EXECUTION_STATES = PRE_EXECUTION_STATES
    EXECUTING_STATES = EXECUTING_STATES
    TERMINAL_STATES = TERMINAL_STATES
    OPEN_STATES = OPEN_STATES
    SENSITIVE_ENV_FIELDS = SENSITIVE_ENV_FIELDS

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | set(ENV_ROLES) | set(REVIEW_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_env(self, role: str) -> bool:
        return role == "admin" or role in ENV_ROLES

    def role_can_review(self, role: str) -> bool:
        return role == "admin" or role in REVIEW_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    # ---- 输入校验与方案计算 -------------------------------------------------

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

    def validate_environment(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """部分更新：只校验实际传入的字段，缺省由服务层用台账现值补齐。"""
        p = dict(payload or {})
        result: Dict[str, Any] = {}
        if "sea_state" in p:
            result["sea_state"] = integer(p, "sea_state", 0, 9)
        if "spare_total_km" in p:
            result["spare_total_km"] = number(p, "spare_total_km", 0)
        if "vessel_available" in p:
            result["vessel_available"] = boolean(p, "vessel_available")
        if "permit_valid" in p:
            result["permit_valid"] = boolean(p, "permit_valid")
        return result

    def build_plan(
        self,
        reported: Dict[str, Any],
        env: Dict[str, Any],
        env_version: int,
        available_spare_km: float,
    ) -> Dict[str, Any]:
        """以当前海况与备缆余量为依据计算方案；未执行方案均由此重算。"""
        p = dict(reported)
        distance = round(float(p["end_km"]) - float(p["start_km"]), 2)
        sea_state = int(env["sea_state"])
        p["repair_distance_km"] = distance
        p["required_spare_km"] = round(distance * 1.05, 2)
        p["estimated_repair_hours"] = round(distance / 2.0 + float(p["depth_m"]) / 100.0 + sea_state * 2.0, 2)
        p["sea_state"] = sea_state
        p["spare_length_km"] = round(float(available_spare_km), 2)
        p["vessel_available"] = bool(env.get("vessel_available", p.get("vessel_available", False)))
        p["permit_valid"] = bool(env.get("permit_valid", p.get("permit_valid", False)))
        p["repair_feasible"] = bool(
            p["vessel_available"]
            and p["permit_valid"]
            and float(p["spare_length_km"]) >= float(p["required_spare_km"])
            and sea_state <= 5
        )
        p["basis_env_version"] = int(env_version)
        p["basis_status"] = "active"
        return p

    def check_create_conflicts(self, reference: str, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["reference"] == reference:
                # 同reference重复申报走"待复核"，不在此判区段冲突
                continue
            if item["state"] in TERMINAL_STATES or item["payload"].get("cable") != payload.get("cable") or item["payload"].get("segment") != payload.get("segment"):
                continue
            if float(payload["start_km"]) < float(item["payload"].get("end_km", 0)) and float(payload["end_km"]) > float(item["payload"].get("start_km", 0)):
                raise Conflict("同一光缆区段已有未结束抢修")

    # ---- 依据有效性 ----------------------------------------------------------

    def invalidate_plan(self, payload: Dict[str, Any], env_version: int) -> Tuple[Dict[str, Any], bool]:
        """依据版本落后的未执行方案标记失效；已冻结的执行中方案不动。"""
        p = dict(payload)
        if p.get("basis_status") == "frozen":
            return p, False
        if int(p.get("basis_env_version", 0)) >= int(env_version):
            return p, False
        p["basis_status"] = "invalidated"
        return p, True

    def require_valid_plan(self, record: Dict[str, Any]) -> None:
        if record["payload"].get("basis_status") == "invalidated":
            raise PlanInvalidated("海况或备缆余量已变化，请先replan重算方案")

    # ---- 动作转换 ------------------------------------------------------------

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(
        self,
        record: Dict[str, Any],
        action: str,
        data: Dict[str, Any],
        spare_info: Optional[Dict[str, float]] = None,
        env: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Dict[str, Any], str, Dict[str, float]]:
        """返回(新状态, 新payload, 摘要, 备缆占用指令)。

        备缆指令形如 {"hold": 15.75} / {"release": 15.75} / {}，
        由处置链执行器在同一事务内落台账，规则层不直接碰库存。
        """
        self.require_transition(record, action)
        new_state = record["state"] if action == "replan" else TRANSITIONS[action][record["state"]]
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        spare_op: Dict[str, float] = {}
        summary = ""

        if action == "replan":
            if record["state"] not in PRE_EXECUTION_STATES:
                raise Conflict("只有未执行方案可以重算")
            rebuilt = self.build_plan(
                reported={
                    "cable": p["cable"], "segment": p["segment"],
                    "start_km": p["start_km"], "end_km": p["end_km"],
                    "depth_m": p["depth_m"], "capacity_gbps": p.get("capacity_gbps", 1),
                },
                env=env or {},
                env_version=int(spare_info["env_version"]) if spare_info else int(p.get("basis_env_version", 1)),
                available_spare_km=float(spare_info["available_spare_km"]) if spare_info else float(p.get("spare_length_km", 0)),
            )
            if "note" in p:
                rebuilt["note"] = p["note"]
            return new_state, rebuilt, "方案已按最新海况与备缆余量重算", {}

        if action == "approve":
            self.require_valid_plan(record)
            if not bool(p["permit_valid"]) or not bool(p["vessel_available"]):
                raise ValidationError("许可或船舶条件不满足")
            changes["repair_manager"] = text(data, "repair_manager")
            summary = "抢修方案已批准"
        elif action == "mobilize":
            self.require_valid_plan(record)
            if not bool(p["permit_valid"]) or not bool(p["vessel_available"]):
                raise ValidationError("许可或船舶条件不满足")
            if float(data.get("weather_window_hours", 0)) < float(p["estimated_repair_hours"]):
                raise ValidationError("海况窗口不足以完成抢修")
            required = float(p["required_spare_km"])
            available = float((spare_info or {}).get("available_spare_km", p.get("spare_length_km", 0)))
            if available < required:
                raise ValidationError("备缆余量不足，无法动员")
            changes["weather_window_hours"] = float(data["weather_window_hours"])
            changes["vessel_name"] = text(data, "vessel_name")
            # 冻结依据：执行中沿用动员时刻的海况与备缆
            changes["basis_status"] = "frozen"
            changes["basis_sea_state"] = int(p["sea_state"])
            changes["basis_spare_total_km"] = float(p["spare_length_km"])
            changes["basis_env_version"] = int(p.get("basis_env_version", 1))
            spare_op = {"hold": required}
            summary = "抢修船已动员，备缆已占用"
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
            spare_op = {"release": float(p["required_spare_km"])}
            summary = "通信恢复，备缆占用归还"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            if p.get("basis_status") == "frozen":
                spare_op = {"release": float(p["required_spare_km"])}
                summary = "抢修取消，备缆占用归还"
            else:
                summary = "抢修取消"
        p.update(changes)
        return new_state, p, summary, spare_op
