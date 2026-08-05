"""通用事件出站通道的统一信封（docs/design/webhook.md §1.3）。

本包是叶子包：不依赖任何协议包与服务层，任何域包（movieclaw_playback、
订阅服务……）都可以依赖它构造事件。事件的**投递**（过滤、格式化、签名、
重试）在 ``movieclaw_api/services/webhook`` 中，与本包解耦——领域侧只负责
"发生了什么"，不关心"发给谁、怎么发"。

设计要点：
- 信封字段（event_id / event / occurred_at / batch_id）是全系统契约，
  事件专属内容全部收进 ``data`` dict，新增领域不动信封；
- ``event_id`` 用 ULID：时间有序（消费端可排序）+ 全局唯一（幂等去重键），
  自实现约 20 行以避免引入 ``python-ulid`` 依赖触发 runtime-version bump。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

# Crockford Base32 字母表（ULID 规范：去掉易混淆的 I/L/O/U）
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """生成一个 26 字符 ULID：48 位毫秒时间戳 + 80 位随机数。

    同毫秒内不保证单调递增（本场景事件量级用不到），跨毫秒天然按时间排序。
    """
    value = (int(time.time() * 1000) & (2**48 - 1)) << 80
    value |= int.from_bytes(os.urandom(10), "big")
    chars = []
    for shift in range(125, -1, -5):
        chars.append(_B32[(value >> shift) & 0x1F])
    return "".join(chars)


@dataclass(frozen=True)
class OutboundEvent:
    """一条待外发的领域事件（统一信封）。

    ``server`` 信息（实例 ID、应用版本）由 formatter 统一附加，不进信封——
    领域侧不该关心部署形态。
    """

    event: str
    """事件名，须是 webhook 事件目录中登记的 ``<domain>.<action>``。"""

    data: dict
    """事件专属字段，由领域侧 builder 装配；结构承诺见设计文档各域章节。"""

    event_id: str = field(default_factory=new_ulid)
    """ULID。重试投递时不变，消费端以此幂等去重。"""

    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    """事件发生时间（带时区）。"""

    batch_id: str | None = None
    """级联批次 ID：整剧标记已看等一次动作产生多条事件时共享，供下游聚合。"""


__all__ = ["OutboundEvent", "new_ulid"]
