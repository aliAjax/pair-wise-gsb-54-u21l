"""SQLite 表结构与事务访问。

处置链一致性约定：记录、审计事件、备缆库存、基线快照与动作日志的写入
全部通过 tx() 在同一事务内完成，任何一步失败整体回滚，库中只存在
"上次完整处置"，不会出现三者各自为政的半持久化状态。
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .domain import Conflict, NotFound, ValidationError


DEFAULT_SEA_STATE = 3
DEFAULT_DEPOT_TOTAL_KM = 1000.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


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
                    action_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS environment (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    sea_state INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    total_km REAL NOT NULL,
                    reserved_km REAL NOT NULL DEFAULT 0,
                    used_km REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inventory_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action_id TEXT NOT NULL,
                    amount_km REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS basis_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sea_state INTEGER NOT NULL,
                    spare_total_km REAL NOT NULL,
                    spare_reserved_km REAL NOT NULL,
                    spare_used_km REAL NOT NULL,
                    spare_available_km REAL NOT NULL,
                    cause TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    record_id INTEGER,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS action_journal (
                    action_id TEXT PRIMARY KEY,
                    record_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    request TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS record_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    payload TEXT NOT NULL,
                    submitted_by TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_audit_action_id ON audit_events(action_id);
                CREATE INDEX IF NOT EXISTS idx_revisions_record ON record_revisions(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reservation_active ON inventory_reservations(record_id) WHERE status='reserved';
                """
            )
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(audit_events)").fetchall()}
            if "action_id" not in columns:
                connection.execute("ALTER TABLE audit_events ADD COLUMN action_id TEXT")
            now = _now()
            connection.execute("INSERT OR IGNORE INTO environment(id, sea_state, updated_at) VALUES (1, ?, ?)", (DEFAULT_SEA_STATE, now))
            connection.execute("INSERT OR IGNORE INTO inventory(id, total_km, reserved_km, used_km, updated_at) VALUES (1, ?, 0, 0, ?)", (DEFAULT_DEPOT_TOTAL_KM, now))
            snapshot_count = int(connection.execute("SELECT COUNT(*) AS total FROM basis_snapshots").fetchone()["total"])
            if snapshot_count == 0:
                connection.execute(
                    "INSERT INTO basis_snapshots(sea_state,spare_total_km,spare_reserved_km,spare_used_km,spare_available_km,cause,reason,record_id,actor_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (DEFAULT_SEA_STATE, DEFAULT_DEPOT_TOTAL_KM, 0.0, 0.0, DEFAULT_DEPOT_TOTAL_KM, "init", "初始基线", None, "system", now),
                )

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """单事务上下文：处置链上的所有写入同生共死。"""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _journal_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["request"] = json.loads(item["request"])
        item["result"] = json.loads(item["result"]) if item["result"] else None
        return item

    # ---------- 记录 ----------

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, _canonical(payload), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,action_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, _canonical({"state": state, "basis_id": payload.get("basis_id"), "plan_status": payload.get("plan_status")}), None, now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def find_by_reference(self, reference: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE reference=?", (reference,)).fetchone()
        return self._row(row) if row is not None else None

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    # ---------- 事务内原语（必须由 tx() 包裹） ----------

    def tx_get_record(self, connection: sqlite3.Connection, record_id: int) -> Dict[str, Any]:
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def tx_list_records_by_states(self, connection: sqlite3.Connection, states: Sequence[str], exclude_id: Optional[int] = None) -> List[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in states)
        sql = "SELECT * FROM records WHERE state IN (%s)" % placeholders
        params: List[Any] = list(states)
        if exclude_id is not None:
            sql += " AND id<>?"
            params.append(int(exclude_id))
        sql += " ORDER BY id"
        rows = connection.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def tx_update_record(self, connection: sqlite3.Connection, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        if int(row["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")
        version = int(expected_version) + 1
        connection.execute(
            "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
            (state, version, _canonical(payload), actor_id, _now(), record_id),
        )
        return self.tx_get_record(connection, record_id)

    def tx_insert_audit(self, connection: sqlite3.Connection, record_id: int, action: str, actor_id: str, version: int, details: Dict[str, Any], action_id: Optional[str] = None) -> None:
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,action_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (record_id, action, actor_id, int(version), _canonical(details), action_id, _now()),
        )

    # ---------- 环境与备缆库存（事务内） ----------

    def tx_environment(self, connection: sqlite3.Connection) -> Dict[str, Any]:
        return dict(connection.execute("SELECT * FROM environment WHERE id=1").fetchone())

    def tx_set_sea_state(self, connection: sqlite3.Connection, sea_state: int) -> None:
        connection.execute("UPDATE environment SET sea_state=?, updated_at=? WHERE id=1", (int(sea_state), _now()))

    def tx_inventory(self, connection: sqlite3.Connection) -> Dict[str, Any]:
        item = dict(connection.execute("SELECT * FROM inventory WHERE id=1").fetchone())
        item["available_km"] = round(float(item["total_km"]) - float(item["reserved_km"]) - float(item["used_km"]), 6)
        return item

    def tx_set_inventory_total(self, connection: sqlite3.Connection, total_km: float) -> None:
        inventory = self.tx_inventory(connection)
        if float(total_km) < float(inventory["reserved_km"]) + float(inventory["used_km"]):
            raise ValidationError("备缆总量不能低于已预留与已使用之和")
        connection.execute("UPDATE inventory SET total_km=?, updated_at=? WHERE id=1", (float(total_km), _now()))

    def tx_active_reservation(self, connection: sqlite3.Connection, record_id: int) -> Optional[Dict[str, Any]]:
        row = connection.execute("SELECT * FROM inventory_reservations WHERE record_id=? AND status='reserved'", (record_id,)).fetchone()
        return dict(row) if row is not None else None

    def tx_reserve(self, connection: sqlite3.Connection, record_id: int, action_id: str, amount_km: float) -> None:
        inventory = self.tx_inventory(connection)
        if float(amount_km) > float(inventory["available_km"]) + 1e-9:
            raise ValidationError("备缆余量不足")
        now = _now()
        try:
            connection.execute(
                "INSERT INTO inventory_reservations(record_id,action_id,amount_km,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (record_id, action_id, float(amount_km), "reserved", now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("记录已持有备缆预留") from exc
        connection.execute("UPDATE inventory SET reserved_km=reserved_km+?, updated_at=? WHERE id=1", (float(amount_km), now))

    def tx_release(self, connection: sqlite3.Connection, record_id: int) -> float:
        reservation = self.tx_active_reservation(connection, record_id)
        if reservation is None:
            return 0.0
        now = _now()
        connection.execute("UPDATE inventory_reservations SET status='released', updated_at=? WHERE id=?", (now, reservation["id"]))
        connection.execute("UPDATE inventory SET reserved_km=reserved_km-?, updated_at=? WHERE id=1", (float(reservation["amount_km"]), now))
        return float(reservation["amount_km"])

    def tx_consume(self, connection: sqlite3.Connection, record_id: int, used_km: float) -> None:
        reservation = self.tx_active_reservation(connection, record_id)
        reserved_amount = float(reservation["amount_km"]) if reservation is not None else 0.0
        inventory = self.tx_inventory(connection)
        extra = float(used_km) - reserved_amount
        if extra > float(inventory["available_km"]) + 1e-9:
            raise ValidationError("备缆余量不足以覆盖实际使用")
        now = _now()
        if reservation is not None:
            connection.execute("UPDATE inventory_reservations SET status='consumed', updated_at=? WHERE id=?", (now, reservation["id"]))
        connection.execute(
            "UPDATE inventory SET reserved_km=reserved_km-?, used_km=used_km+?, updated_at=? WHERE id=1",
            (reserved_amount, float(used_km), now),
        )

    def tx_insert_snapshot(self, connection: sqlite3.Connection, actor_id: str, cause: str, reason: str = "", record_id: Optional[int] = None) -> Dict[str, Any]:
        environment = self.tx_environment(connection)
        inventory = self.tx_inventory(connection)
        cursor = connection.execute(
            "INSERT INTO basis_snapshots(sea_state,spare_total_km,spare_reserved_km,spare_used_km,spare_available_km,cause,reason,record_id,actor_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                int(environment["sea_state"]),
                float(inventory["total_km"]),
                float(inventory["reserved_km"]),
                float(inventory["used_km"]),
                float(inventory["available_km"]),
                cause,
                reason or "",
                record_id,
                actor_id,
                _now(),
            ),
        )
        return {
            "id": int(cursor.lastrowid),
            "sea_state": int(environment["sea_state"]),
            "spare_total_km": float(inventory["total_km"]),
            "spare_reserved_km": float(inventory["reserved_km"]),
            "spare_used_km": float(inventory["used_km"]),
            "spare_available_km": float(inventory["available_km"]),
            "cause": cause,
            "reason": reason or "",
        }

    # ---------- 动作日志（幂等与断点恢复） ----------

    def journal_entry(self, action_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM action_journal WHERE action_id=?", (action_id,)).fetchone()
        return self._journal_row(row) if row is not None else None

    def journal_start(self, action_id: str, record_id: int, action: str, actor_id: str, request: Dict[str, Any]) -> None:
        """独立小事务落 started 标记；崩溃后恢复流程据此识别未完成动作。"""
        now = _now()
        with self._connect() as connection:
            row = connection.execute("SELECT status, request FROM action_journal WHERE action_id=?", (action_id,)).fetchone()
            if row is None:
                try:
                    connection.execute(
                        "INSERT INTO action_journal(action_id,record_id,action,actor_id,request,status,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (action_id, record_id, action, actor_id, _canonical(request), "started", None, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict("相同action_id的请求正在处理") from exc
                return
            if json.loads(row["request"]) != request:
                raise Conflict("action_id与原始请求不一致")
            if row["status"] == "rolled_back":
                connection.execute("UPDATE action_journal SET status='started', result=NULL, updated_at=? WHERE action_id=?", (now, action_id))
            elif row["status"] == "started":
                # 前次尝试未落库即中断，重放同一请求可安全续做
                return
            else:
                raise Conflict("action_id已提交")

    def journal_finish(self, action_id: str, status: str, result: Optional[Dict[str, Any]]) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE action_journal SET status=?, result=?, updated_at=? WHERE action_id=?",
                (status, _canonical(result) if result is not None else None, _now(), action_id),
            )

    def tx_journal_finish(self, connection: sqlite3.Connection, action_id: str, status: str, result: Optional[Dict[str, Any]]) -> None:
        connection.execute(
            "UPDATE action_journal SET status=?, result=?, updated_at=? WHERE action_id=?",
            (status, _canonical(result) if result is not None else None, _now(), action_id),
        )

    def journal_started(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM action_journal WHERE status='started' ORDER BY created_at").fetchall()
        return [self._journal_row(row) for row in rows]

    def audit_has_action_id(self, record_id: int, action_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM audit_events WHERE record_id=? AND action_id=? LIMIT 1", (record_id, action_id)).fetchone()
        return row is not None

    # ---------- 后到版本（待复核） ----------

    def add_revision(self, record_id: int, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO record_revisions(record_id,payload,submitted_by,status,note,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, _canonical(payload), actor_id, "pending_review", "", _now()),
            )
            row = connection.execute("SELECT * FROM record_revisions WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        return self._revision_row(row)

    @staticmethod
    def _revision_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def get_revision(self, record_id: int, revision_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM record_revisions WHERE id=? AND record_id=?", (revision_id, record_id)).fetchone()
        if row is None:
            raise NotFound("复核版本不存在")
        return self._revision_row(row)

    def list_revisions(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM record_revisions WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._revision_row(row) for row in rows]

    def tx_finish_revision(self, connection: sqlite3.Connection, revision_id: int, status: str, actor_id: str, note: str) -> None:
        connection.execute(
            "UPDATE record_revisions SET status=?, note=?, reviewed_by=?, reviewed_at=? WHERE id=?",
            (status, note or "", actor_id, _now(), revision_id),
        )

    # ---------- 只读视图 ----------

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,action_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), _canonical(details), None, _now()),
            )

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

    def current_basis(self) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM basis_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row)

    def snapshots(self, limit: int = 20) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM basis_snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def inventory_view(self) -> Dict[str, Any]:
        with self._connect() as connection:
            item = dict(connection.execute("SELECT * FROM inventory WHERE id=1").fetchone())
            rows = connection.execute("SELECT * FROM inventory_reservations ORDER BY id DESC LIMIT 100").fetchall()
        item["available_km"] = round(float(item["total_km"]) - float(item["reserved_km"]) - float(item["used_km"]), 6)
        item["reservations"] = [dict(row) for row in rows]
        return item

    def environment_view(self) -> Dict[str, Any]:
        with self._connect() as connection:
            environment = dict(connection.execute("SELECT * FROM environment WHERE id=1").fetchone())
        environment["basis"] = self.current_basis()
        environment["snapshots"] = self.snapshots(limit=20)
        return environment

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
