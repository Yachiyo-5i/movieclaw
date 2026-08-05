"""事件 Webhook 出站链路（docs/design/webhook.md §5）。

分层：
- ``catalog``    —— 事件目录：全系统可外发事件的注册表（分组/说明/Jellyfin 映射）。
- ``formatter``  —— 外发格式器：内部 ``OutboundEvent`` → HTTP 报文（body + headers）。
- ``deliveries`` —— 投递记录：每 endpoint 一个内存环形缓冲（诊断用途）。
- ``dispatcher`` —— 投递器：过滤订阅 → 格式化 → 签名 → 发送 → 重试 → 记录。

新领域事件接入清单（投递链路零改动）：
① 领域侧写 builder 产出 ``OutboundEvent``（参考 ``movieclaw_playback/events.py``）；
② 在 ``catalog.EVENT_CATALOG`` 登记一行；
③ 产生点在 ``session.commit()`` **之后**调用 ``emit_events([...])``。
"""

from movieclaw_api.services.webhook.catalog import EVENT_CATALOG, EventSpec, catalog_view
from movieclaw_api.services.webhook.deliveries import recent_deliveries
from movieclaw_api.services.webhook.dispatcher import deliver_test, emit_events

__all__ = [
    "EVENT_CATALOG",
    "EventSpec",
    "catalog_view",
    "emit_events",
    "deliver_test",
    "recent_deliveries",
]
