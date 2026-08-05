"""外发格式器：内部 ``OutboundEvent`` → HTTP 报文（docs/design/webhook.md §2）。

P1 只实现自有协议（movieclaw 格式）；Jellyfin 兼容格式（变量字典 + Handlebars
模板渲染）按设计文档 §3 在 P2 落地，接口形态已按双格式预留：
``format(event, ...) -> (body, headers)``。
"""

from __future__ import annotations

import hashlib
import hmac
import json

from movieclaw_api import __version__
from movieclaw_events import OutboundEvent

SPEC_VERSION = "1"


def format_movieclaw(
    event: OutboundEvent, *, server: dict, secret: str
) -> tuple[bytes, dict[str, str]]:
    """自有协议报文：统一信封 JSON + HMAC-SHA256 签名头。

    签名对**原始请求体字节**计算（GitHub 惯例），消费端用同一 secret 对
    收到的 body 重算比对即可验真。
    """
    payload = {
        "spec_version": SPEC_VERSION,
        "event_id": event.event_id,
        "event": event.event,
        "occurred_at": event.occurred_at.isoformat(),
        "batch_id": event.batch_id,
        "server": server,
        "data": event.data,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": f"MovieClaw/{__version__}",
        "X-MovieClaw-Event": event.event,
        "X-MovieClaw-Delivery": event.event_id,
    }
    if secret:
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-MovieClaw-Signature"] = f"sha256={digest}"
    return body, headers
