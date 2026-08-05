"""事件 Webhook 管理接口的请求/响应模型（「设置 → Webhook」页）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class WebhookEndpointPayload(BaseModel):
    """保存配置时的单个 endpoint。``id`` 为空 = 新建（服务端生成 id 与 secret）。"""

    id: str = ""
    name: str = ""
    url: str = ""
    format: Literal["movieclaw", "jellyfin"] = "movieclaw"
    enabled: bool = True
    events: list[str] = Field(default_factory=list)
    template: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    egress_scope: Literal["lan", "wan"] = "lan"


class WebhookConfigPayload(BaseModel):
    enabled: bool = False
    endpoints: list[WebhookEndpointPayload] = Field(default_factory=list)


class DeliveryView(BaseModel):
    """一条投递记录（内存环形缓冲，诊断用途）。"""

    event_id: str
    event: str
    ok: bool
    status_code: int | None = None
    duration_ms: int = 0
    attempts: int = 1
    error: str = ""
    at: str = ""


class WebhookEndpointView(BaseModel):
    id: str
    name: str
    url: str
    format: Literal["movieclaw", "jellyfin"]
    enabled: bool
    events: list[str]
    template: str
    headers: dict[str, str]
    egress_scope: Literal["lan", "wan"]
    secret_masked: str = ""
    secret: str | None = Field(
        default=None,
        description="secret 明文——仅在新建/轮换的响应里一次性返回，此后只给打码值",
    )
    last_delivery: DeliveryView | None = None


class EventCatalogEntry(BaseModel):
    event: str
    group: str
    label: str
    jellyfin_supported: bool
    default_on: bool = True


class WebhookConfigView(BaseModel):
    enabled: bool
    endpoints: list[WebhookEndpointView]
    catalog: list[EventCatalogEntry]
    """事件目录：前端按 group 渲染订阅多选，jellyfin 格式禁用无映射事件。"""
