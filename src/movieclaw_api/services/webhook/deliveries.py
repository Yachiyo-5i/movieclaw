"""投递记录：每 endpoint 一个内存环形缓冲（docs/design/webhook.md §4.2）。

纯诊断用途——设置页展示"最近投递"帮用户排障（下游没收到？看这里的状态码
和错误摘要）。进程重启即清空，契约里已明示不承诺持久；确有重放需求时 P2
落表。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

_MAX_RECORDS_PER_ENDPOINT = 50


@dataclass
class DeliveryRecord:
    event_id: str
    event: str
    status_code: int | None = None
    """HTTP 状态码；None = 请求未到达（网络错误/熔断/格式化失败）。"""
    duration_ms: int = 0
    attempts: int = 1
    error: str = ""
    """失败摘要（中文），成功为空串。"""
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


_records: dict[str, deque[DeliveryRecord]] = {}


def record_delivery(endpoint_id: str, record: DeliveryRecord) -> None:
    _records.setdefault(endpoint_id, deque(maxlen=_MAX_RECORDS_PER_ENDPOINT)).append(
        record
    )


def recent_deliveries(endpoint_id: str) -> list[dict]:
    """最近投递记录，新的在前，供管理 API 直接返回。"""
    return [
        {
            "event_id": r.event_id,
            "event": r.event,
            "ok": r.ok,
            "status_code": r.status_code,
            "duration_ms": r.duration_ms,
            "attempts": r.attempts,
            "error": r.error,
            "at": r.at.isoformat(),
        }
        for r in reversed(_records.get(endpoint_id, ()))
    ]


def clear_deliveries() -> None:
    """清空全部记录（供测试隔离状态）。"""
    _records.clear()
