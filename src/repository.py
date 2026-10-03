"""SQLite 表结构与事务访问。

处置链相关的每次落库（记录状态、审计时间线、备缆占用、链步骤）
都在同一个 BEGIN IMMEDIATE 事务内提交，要么全部生效，要么全部不生效，
因此不存在"一半已保存"的可见中间态；崩溃后可按幂等键从 prepared 步骤续做。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import Conflict, NotFound, RecoveryRequired


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


PRE_EXECUTION_STATES = ("detected", "approved")


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS environment (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL,
                    sea_state INTEGER NOT NULL,
                    spare_total_km REAL NOT NULL,
                    vessel_available INTEGER NOT NULL,
                    permit_valid INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS record_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    reference TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    resolution_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolved_by TEXT
                );
                CREATE TABLE IF NOT EXISTS chain_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    idem_key TEXT NOT NULL UNIQUE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    input TEXT NOT NULL,
                    prepared TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'prepared',
                    result_version INTEGER,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS spare_reservations (
                    record_id INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
                    held_km REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'held',
                    created_at TEXT NOT NULL,
                    released_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_reviews_status ON record_reviews(status, id);
                CREATE INDEX IF NOT EXISTS idx_chain_record ON chain_steps(record_id, seq);
                """
            )
            row = connection.execute("SELECT COUNT(*) AS n FROM environment").fetchone()
            if int(row["n"]) == 0:
                connection.execute(
                    "INSERT INTO environment(id,version,sea_state,spare_total_km,vessel_available,permit_valid,updated_by,updated_at)"
                    " VALUES(1,1,3,100,1,1,'system',?)",
                    (_now(),),
                )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _step(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["input"] = json.loads(item["input"])
        item["prepared"] = json.loads(item["prepared"])
        return item

    # ---- 故障记录 ------------------------------------------------------------

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def get_by_reference(self, reference: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE reference=?", (reference,)).fetchone()
        return self._row(row) if row else None

    def spare_reservation(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM spare_reservations WHERE record_id=?", (record_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, _dumps(payload), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, _dumps({"state": state, "basis_env_version": payload.get("basis_env_version")}), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- 环境台账（海况/备缆余量） ------------------------------------------

    def get_environment(self) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM environment WHERE id=1").fetchone()
        env = dict(row)
        env["vessel_available"] = bool(env["vessel_available"])
        env["permit_valid"] = bool(env["permit_valid"])
        held = self.held_spare_km()
        env["held_spare_km"] = round(held, 2)
        env["available_spare_km"] = round(float(env["spare_total_km"]) - held, 2)
        return env

    def held_spare_km(self) -> float:
        with self._connect() as connection:
            row = connection.execute("SELECT COALESCE(SUM(held_km),0) AS total FROM spare_reservations WHERE status='held'").fetchone()
        return float(row["total"])

    def apply_environment(
        self,
        values: Dict[str, Any],
        actor_id: str,
        invalidate: Callable[[Dict[str, Any], int], tuple],
    ) -> Dict[str, Any]:
        """更新海况/备缆余量；敏感字段变化时，同事务使未执行方案失效。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM environment WHERE id=1").fetchone()
            old = dict(row)
            sensitive_changed = (
                int(values["sea_state"]) != int(old["sea_state"])
                or float(values["spare_total_km"]) != float(old["spare_total_km"])
            )
            new_version = int(old["version"]) + (1 if sensitive_changed else 0)
            connection.execute(
                "UPDATE environment SET version=?,sea_state=?,spare_total_km=?,vessel_available=?,permit_valid=?,updated_by=?,updated_at=? WHERE id=1",
                (
                    new_version,
                    int(values["sea_state"]),
                    float(values["spare_total_km"]),
                    1 if values.get("vessel_available", bool(old["vessel_available"])) else 0,
                    1 if values.get("permit_valid", bool(old["permit_valid"])) else 0,
                    actor_id,
                    now,
                ),
            )
            invalidated: List[int] = []
            if sensitive_changed:
                rows = connection.execute(
                    "SELECT * FROM records WHERE state IN (%s)" % ",".join("?" for _ in PRE_EXECUTION_STATES),
                    PRE_EXECUTION_STATES,
                ).fetchall()
                for row in rows:
                    record = self._row(row)
                    new_payload, changed = invalidate(record["payload"], new_version)
                    if not changed:
                        continue
                    next_version = int(record["version"]) + 1
                    connection.execute(
                        "UPDATE records SET payload=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                        (_dumps(new_payload), next_version, actor_id, now, record["id"]),
                    )
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            record["id"], "basis_invalidated", actor_id, next_version,
                            _dumps({"old_env_version": int(old["version"]), "new_env_version": new_version,
                                    "sea_state": int(values["sea_state"]),
                                    "spare_total_km": float(values["spare_total_km"])}),
                            now,
                        ),
                    )
                    invalidated.append(int(record["id"]))
            connection.commit()
        env = self.get_environment()
        env["invalidated_record_ids"] = invalidated
        return env

    # ---- 待复核（后到版本） --------------------------------------------------

    def add_review(self, record_id: int, reference: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO record_reviews(record_id,reference,payload,actor_id,status,created_at) VALUES(?,?,?,?,'pending',?)",
                (record_id, reference, _dumps(payload), actor_id, now),
            )
            review_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "revision_parked", actor_id,
                 int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()["version"]),
                 _dumps({"review_id": review_id, "reason": "同故障记录后到版本，留待复核，不覆盖已生效状态"}), now),
            )
            row = connection.execute("SELECT * FROM record_reviews WHERE id=?", (review_id,)).fetchone()
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def list_reviews(self, status: str = "pending") -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM record_reviews WHERE status=? ORDER BY id", (status,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def get_review(self, review_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM record_reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFound("待复核记录不存在")
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def resolve_review(
        self,
        review_id: int,
        approve: bool,
        actor_id: str,
        note: str,
        rebuilt_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM record_reviews WHERE id=?", (review_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("待复核记录不存在")
            if row["status"] != "pending":
                connection.rollback()
                raise Conflict("该待复核版本已处理")
            record = connection.execute("SELECT * FROM records WHERE id=?", (row["record_id"],)).fetchone()
            if approve:
                if record["state"] not in PRE_EXECUTION_STATES:
                    connection.rollback()
                    raise Conflict("记录已进入执行阶段，后到版本不能覆盖")
                next_version = int(record["version"]) + 1
                connection.execute(
                    "UPDATE records SET payload=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                    (_dumps(rebuilt_payload), next_version, actor_id, now, record["id"]),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record["id"], "revision_accepted", actor_id, next_version,
                     _dumps({"review_id": review_id, "note": note, "basis_env_version": rebuilt_payload.get("basis_env_version")}), now),
                )
            else:
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record["id"], "revision_rejected", actor_id, int(record["version"]),
                     _dumps({"review_id": review_id, "note": note}), now),
                )
            connection.execute(
                "UPDATE record_reviews SET status=?,resolution_note=?,resolved_at=?,resolved_by=? WHERE id=?",
                ("accepted" if approve else "rejected", note, now, actor_id, review_id),
            )
            connection.commit()
        return self.get_review(review_id)

    # ---- 处置链步骤（断点续做 + 幂等） --------------------------------------

    def step_by_key(self, idem_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM chain_steps WHERE idem_key=?", (idem_key,)).fetchone()
        return self._step(row) if row else None

    def open_step(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM chain_steps WHERE record_id=? AND status='prepared' ORDER BY seq DESC LIMIT 1",
                (record_id,),
            ).fetchone()
        return self._step(row) if row else None

    def chain_steps(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM chain_steps WHERE record_id=? ORDER BY seq", (record_id,)).fetchall()
        return [self._step(row) for row in rows]

    def begin_step(
        self,
        record_id: int,
        idem_key: str,
        action: str,
        actor_id: str,
        expected_version: int,
        data: Dict[str, Any],
        prepared: Dict[str, Any],
    ) -> Dict[str, Any]:
        """第一阶段事务：登记动作意图（含重算结果快照），提交后即可被恢复。"""
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
                if row is None:
                    connection.rollback()
                    raise NotFound("记录不存在")
                if int(row["version"]) != int(expected_version):
                    connection.rollback()
                    raise Conflict("版本冲突，请刷新后重试")
                open_row = connection.execute(
                    "SELECT id FROM chain_steps WHERE record_id=? AND status='prepared'", (record_id,)
                ).fetchone()
                if open_row is not None:
                    connection.rollback()
                    raise RecoveryRequired("存在断在半路的处置步骤，请用同一幂等键resume续做")
                seq_row = connection.execute("SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM chain_steps WHERE record_id=?", (record_id,)).fetchone()
                cursor = connection.execute(
                    "INSERT INTO chain_steps(record_id,seq,idem_key,action,actor_id,expected_version,input,prepared,status,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,'prepared',?)",
                    (record_id, int(seq_row["next_seq"]), idem_key, action, actor_id,
                     int(expected_version), _dumps(data or {}), _dumps(prepared), now),
                )
                step_id = int(cursor.lastrowid)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("幂等键已存在，请改用恢复接口或新的幂等键") from exc
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM chain_steps WHERE id=?", (step_id,)).fetchone()
        return self._step(row)

    def complete_step(self, step_id: int) -> Dict[str, Any]:
        """第二阶段事务：按 prepared 快照一次落 记录+审计+备缆，标记步骤完成。

        重放（步骤已完成）直接返回当时结果，不再占用/归还备缆。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            step_row = connection.execute("SELECT * FROM chain_steps WHERE id=?", (step_id,)).fetchone()
            if step_row is None:
                connection.rollback()
                raise NotFound("处置步骤不存在")
            step = self._step(step_row)
            if step["status"] == "done":
                record = self._row(connection.execute("SELECT * FROM records WHERE id=?", (step["record_id"],)).fetchone())
                connection.rollback()
                return {"replayed": True, "step": step, "record": record}
            if step["status"] != "prepared":
                connection.rollback()
                raise Conflict("该处置步骤已被后续版本取代")

            record_row = connection.execute("SELECT * FROM records WHERE id=?", (step["record_id"],)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            record = self._row(record_row)
            if int(record["version"]) != int(step["expected_version"]):
                # 记录在意图登记后被其他动作推进：该意图作废，需重新处置
                connection.execute(
                    "UPDATE chain_steps SET status='superseded', completed_at=? WHERE id=?", (now, step_id)
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record["id"], "step_superseded", step["actor_id"], int(record["version"]),
                     _dumps({"idem_key": step["idem_key"], "action": step["action"]}), now),
                )
                connection.commit()
                raise Conflict("依据版本已变化，该处置动作已作废，请重新提交")

            prepared = step["prepared"]
            next_version = int(record["version"]) + 1
            new_state = prepared["to"]
            new_payload = prepared["payload"]
            spare_op = prepared.get("spare_op", {})

            if "hold" in spare_op:
                held = float(spare_op["hold"])
                total_row = connection.execute("SELECT spare_total_km FROM environment WHERE id=1").fetchone()
                held_row = connection.execute(
                    "SELECT COALESCE(SUM(held_km),0) AS total FROM spare_reservations WHERE status='held'"
                ).fetchone()
                if float(total_row["spare_total_km"]) - float(held_row["total"]) + 1e-9 < held:
                    connection.rollback()
                    raise Conflict("备缆余量不足，动员被拒绝")
                dup = connection.execute("SELECT record_id FROM spare_reservations WHERE record_id=?", (record["id"],)).fetchone()
                if dup is not None:
                    connection.rollback()
                    raise Conflict("该处置已占用过备缆，禁止重复占用")
                connection.execute(
                    "INSERT INTO spare_reservations(record_id,held_km,status,created_at) VALUES(?,?, 'held',?)",
                    (record["id"], held, now),
                )
            if "release" in spare_op:
                res_row = connection.execute(
                    "SELECT * FROM spare_reservations WHERE record_id=? AND status='held'", (record["id"],)
                ).fetchone()
                if res_row is None:
                    connection.rollback()
                    raise Conflict("未找到该处置的备缆占用记录，无法归还")
                connection.execute(
                    "UPDATE spare_reservations SET status='released', released_at=? WHERE record_id=?",
                    (now, record["id"]),
                )

            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (new_state, next_version, _dumps(new_payload), step["actor_id"], now, record["id"]),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record["id"], step["action"], step["actor_id"], next_version,
                 _dumps({"summary": prepared["summary"], "idem_key": step["idem_key"], "seq": step["seq"],
                         "from": prepared["from"], "to": new_state, "spare_op": spare_op, "input": step["input"]}),
                 now),
            )
            connection.execute(
                "UPDATE chain_steps SET status='done',result_version=?,completed_at=? WHERE id=?",
                (next_version, now, step_id),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record["id"],)).fetchone()
            connection.commit()
        return {"replayed": False, "step": self._step(dict(step_row, status="done", result_version=next_version, completed_at=now)),
                "record": self._row(result)}
