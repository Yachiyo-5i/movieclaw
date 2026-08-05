"""Webhook 投递器：过滤订阅 → 格式化 → 发送 → 重试 → 记录（docs/design/webhook.md §5）。

对外唯一入口 ``emit_events()``：领域无关，任何产生点（Jellyfin 协议层
handler、订阅服务……）在 ``session.commit()`` **之后**调用。同步签名 +
fire-and-forget，任何失败只记日志，绝不影响业务主链路（与
``services/channel_push.py`` 同一惯例）。

投递语义（对外契约，见设计文档 §2.4）：
- 超时 10s，失败重试 2 次（间隔 5s / 30s），共 3 次尝试；
- at-least-once 但不承诺必达（无持久队列，进程重启丢弃在途任务）；
- 不保证顺序；重试时 event_id 不变，消费端按 X-MovieClaw-Delivery 幂等去重；
- 出站走 ``movieclaw_net.egress_transport``（服务标签 ``webhook``，供代理
  路由），但**关闭熔断**：熔断器按服务标签全进程共享，而 webhook 的目标是
  逐 endpoint 的——一个宕机的 endpoint 会把熔断打开，拖垮其他健康 endpoint
  的投递。重试本身有界（3 次），失败代价可控；不跟随重定向（httpx 默认）。
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
from movieclaw_api.services.webhook.handlebars import TemplateError
from movieclaw_api.services.webhook.jellyfin_formatter import (
    format_jellyfin,
    merge_for_jellyfin,
)
from movieclaw_api.settings import WebhookEndpoint, WebhookSetting, get_setting_store
from movieclaw_events import OutboundEvent
from movieclaw_net import EgressScope, egress_transport

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
                "item_id": 0,
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
    """服务器上下文：实例 ID/名称/地址复用 Jellyfin 兼容层身份，版本取应用版本，
    用户名取管理员账号（Jellyfin 变量字典的 NotificationUsername）。

    注意这是**内部上下文**，自有协议报文的 server 对象只取其中
    id/name/version 三个键（见 ``_wire_server``），url/username 不进信封。
    """
    from movieclaw_api import __version__
    from movieclaw_api.settings import AdminAccountSetting, get_setting_store
    from movieclaw_api.settings.schemas import get_jellyfin_compat

    compat = await get_jellyfin_compat()
    admin = await get_setting_store().get(AdminAccountSetting)
    return {
        "id": compat.server_id,
        "name": compat.server_name,
        "version": __version__,
        "url": compat.published_server_url,
        "username": admin.username,
    }


def _wire_server(server: dict) -> dict:
    """自有协议信封的 server 对象（稳定契约：id/name/version）。"""
    return {k: server.get(k, "") for k in ("id", "name", "version")}


async def _deliver_endpoint(
    endpoint: WebhookEndpoint, events: list[OutboundEvent], server: dict
) -> None:
    subscribed = set(endpoint.events)
    hits = []
    for event in events:
        if not is_known_event(event.event):
            logger.warning("忽略未在事件目录登记的 Webhook 事件：%s", event.event)
            continue
        if event.event in subscribed:
            hits.append(event)
    if endpoint.format == "jellyfin":
        # 同批 stopped+completed 都映射到 PlaybackStop，completed 吸收 stopped
        hits = merge_for_jellyfin(hits)
    for event in hits:
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

    try:
        if endpoint.format == "jellyfin":
            if not endpoint.template.strip():
                record.error = (
                    "jellyfin 格式的 endpoint 未配置模板，无法投递（请粘贴下游文档提供的模板）"
                )
                record_delivery(endpoint.id, record)
                return record
            body, headers = format_jellyfin(
                event, server=server, template=endpoint.template
            )
        else:
            body, headers = format_movieclaw(
                event, server=_wire_server(server), secret=endpoint.secret
            )
    except (TemplateError, ValueError) as exc:
        record.error = f"模板渲染失败：{exc}"
        record_delivery(endpoint.id, record)
        logger.warning(
            "Webhook 模板渲染失败：%s → %s（%s）", event.event, endpoint.name or endpoint.url, exc
        )
        return record
    if endpoint.headers:
        headers = {**headers, **endpoint.headers}
    scope = EgressScope.LAN if endpoint.egress_scope == "lan" else EgressScope.WAN
    attempts = max_attempts or (len(RETRY_DELAYS) + 1)

    for attempt in range(1, attempts + 1):
        record.attempts = attempt
        try:
            # use_breaker=False：熔断键按服务标签共享，会让坏 endpoint 连坐
            # 健康 endpoint（模块 docstring 有完整理由）
            transport = egress_transport("webhook", scope=scope, use_breaker=False)
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT, transport=transport
            ) as client:
                response = await client.post(endpoint.url, content=body, headers=headers)
            record.status_code = response.status_code
            if record.ok:
                record.error = ""
                break
            record.error = f"目标返回 HTTP {response.status_code}"
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
