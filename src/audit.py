"""审计事件封装，时间线查询保持只读。

所有审计写入都由处置链事务（repository.begin_step/complete_step、
apply_environment、add_review/resolve_review）在同一事务内完成，
本类只暴露只读时间线，避免出现游离于处置链之外的审计记录。
"""
from typing import Any, Dict, List


class AuditRecorder:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def timeline(self, record_id: int) -> List[Dict[str, Any]]:
        return self.repository.audit_timeline(record_id)
