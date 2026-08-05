"""Webhook 投递器：过滤订阅 → 格式化 → 发送 → 重试 → 记录（docs/design/webhook.md §5）。

对外唯一入口 ``emit_events()``：领域无关，任何产生点（Jellyfin 协议层
handler、订阅服务……）在 ``session.commit()`` **之后**调用。同步签名 +
fire-and-forget，任何失败只记日志，绝不影响业务主链路（与
``services/channel_push.py`` 同一惯例）。

投递语义（对外契约，见设计文档 §2.4）：
- 超时 10s，失败重试 2 次（间隔 5s / 30s），共 3 次尝试；
- at-least-once 但不承诺必达（无持久队列，进程重启丢弃在途任务）；
- 不保证顺序；重试时 event_id 不变，消费端按 X-MovieClaw-Delivery 幂等去重；
- 出站走 ``movieclaw_net.egress_transport``（服务标签 ``webhook``），命中
  熔断时快速失败；不跟随重定向（httpx 默认）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

import httpx

from movieclaw_api.services.webhook.catalog import is_known_event
from movieclaw_api.services.webhook.deliveries import DeliveryRecord, record_delivery
from movieclaw_api.services.webhook.formatter import format_movieclaw
from movieclaw_api.settings import WebhookEndpoint, WebhookSetting, get_setting_store
from movieclaw_events import OutboundEvent
from movieclaw_net import CircuitOpenError, EgressScope, egress_transport

logger = logging.getLogger("movieclaw_api.webhook")

#: 在飞的投递任务：事件循环只持任务的弱引用，不留强引用的话
#: 任务可能在执行中途被 GC 回收，投递就会无声丢失
_tasks: set[asyncio.Task[None]] = set()

REQUEST_TIMEOUT = 10.0
#: 重试间隔（秒）；尝试次数 = len(RETRY_DELAYS) + 1
RETRY_DELAYS: tuple[float, ...] = (5.0, 30.0)


def emit_events(events: list[OutboundEvent]) -> None:
    """业务侧唯一入口：后台投递一批事件，失败只记日志，不阻塞、不抛错。"""
    if not events:
        return

    async def _run() -> None:
        try:
            setting = await get_setting_store().get(WebhookSetting)
            if not setting.enabled:
                return
            targets = [ep for ep in setting.endpoints if ep.enabled and ep.url]
            if not targets:
                return
            server = await _server_info()
            await asyncio.gather(
                *(_deliver_endpoint(ep, events, server) for ep in targets)
            )
        except Exception:  # noqa: BLE001 -- 投递绝不能影响业务主链路
            logger.exception("Webhook 投递任务失败（已忽略）")

    try:
        task = asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        logger.debug("无运行中的事件循环，Webhook 投递已跳过")
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def deliver_test(endpoint: WebhookEndpoint) -> DeliveryRecord:
    """设置页「发送测试」：同步投递一条示例事件（单次尝试，立刻反馈结果）。"""
    event = OutboundEvent(
        event="webhook.test",
        data={
            "media": {
                "type": "episode",
                "tmdb_id": 1399,
                "imdb_id": None,
                "title": "示例剧集",
                "original_title": "Sample Show",
                "year": 2011,
                "season_number": 2,
                "episode_number": 4,
            },
            "playback": {
                "position_ms": 3_120_000,
                "duration_ms": 3_300_000,
                "played": True,
                "play_count": 1,
            },
            "client": {"name": "MovieClaw 设置页", "device_name": None,
                       "device_id": None, "version": None},
        },
    )
    return await _deliver_one(endpoint, event, await _server_info(), max_attempts=1)


async def _server_info() -> dict:
    """信封的 server 对象：实例 ID/名称复用 Jellyfin 兼容层身份，版本取应用版本。"""
    from movieclaw_api import __version__
    from movieclaw_api.settings.schemas import get_jellyfin_compat

    compat = await get_jellyfin_compat()
    return {"id": compat.server_id, "name": compat.server_name, "version": __version__}


async def _deliver_endpoint(
    endpoint: WebhookEndpoint, events: list[OutboundEvent], server: dict
) -> None:
    subscribed = set(endpoint.events)
    for event in events:
        if not is_known_event(event.event):
            logger.warning("忽略未在事件目录登记的 Webhook 事件：%s", event.event)
            continue
        if event.event not in subscribed:
            continue
        await _deliver_one(endpoint, event, server)


async def _deliver_one(
    endpoint: WebhookEndpoint,
    event: OutboundEvent,
    server: dict,
    *,
    max_attempts: int | None = None,
) -> DeliveryRecord:
    """向单个 endpoint 投递单条事件（带重试），并写入投递记录。"""
    started = time.monotonic()
    record = DeliveryRecord(event_id=event.event_id, event=event.event)

    if endpoint.format != "movieclaw":
        # P2 落地 Jellyfin 兼容格式（设计文档 §3）；先记录明确失败而非静默丢弃
        record.error = "Jellyfin 兼容格式尚未支持（后续版本提供），本次投递已跳过"
        record_delivery(endpoint.id, record)
        return record

    body, headers = format_movieclaw(event, server=server, secret=endpoint.secret)
    if endpoint.headers:
        headers = {**headers, **endpoint.headers}
    scope = EgressScope.LAN if endpoint.egress_scope == "lan" else EgressScope.WAN
    attempts = max_attempts or (len(RETRY_DELAYS) + 1)

    for attempt in range(1, attempts + 1):
        record.attempts = attempt
        try:
            transport = egress_transport("webhook", scope=scope)
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT, transport=transport
            ) as client:
                response = await client.post(endpoint.url, content=body, headers=headers)
            record.status_code = response.status_code
            if record.ok:
                record.error = ""
                break
            record.error = f"目标返回 HTTP {response.status_code}"
        except CircuitOpenError:
            # 熔断打开：线路已知不通，快速失败不再重试
            record.status_code = None
            record.error = "出站熔断已打开（目标近期持续不可达），本次投递快速失败"
            break
        except Exception as exc:  # noqa: BLE001 -- 网络错误统一进投递记录
            record.status_code = None
            record.error = f"请求失败：{exc}"[:200]
        if attempt < attempts and RETRY_DELAYS[attempt - 1 : attempt]:
            delay = RETRY_DELAYS[attempt - 1]
            if delay > 0:
                await asyncio.sleep(delay)

    record.duration_ms = int((time.monotonic() - started) * 1000)
    record.at = datetime.now(UTC)
    record_delivery(endpoint.id, record)
    if record.ok:
        logger.info(
            "Webhook 已投递：%s → %s（HTTP %s，第 %d 次尝试）",
            event.event, endpoint.name or endpoint.url, record.status_code, record.attempts,
        )
    else:
        logger.warning(
            "Webhook 投递失败：%s → %s（%s）",
            event.event, endpoint.name or endpoint.url, record.error,
        )
    return record
