"""事件 Webhook 管理接口（「设置 → Webhook」页的后端）。

五个端点（实现全部在 services/webhook_config.py，本文件只做参数进出）：
- GET  /webhook                          —— 配置 + 事件目录（secret 打码）；
- PUT  /webhook                          —— 全量保存（新建 endpoint 一次性返回 secret 明文）；
- POST /webhook/endpoints/{id}/rotate-secret —— 轮换密钥（一次性返回新明文）；
- POST /webhook/endpoints/{id}/test      —— 发送示例事件，同步返回投递结果；
- GET  /webhook/endpoints/{id}/deliveries —— 最近投递记录（内存环形缓冲）。
"""

from __future__ import annotations

from fastapi import APIRouter

from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.schemas.webhook import (
    DeliveryView,
    WebhookConfigPayload,
    WebhookConfigView,
    WebhookEndpointView,
)
from movieclaw_api.services import webhook_config

router = APIRouter(prefix="/webhook", tags=["webhook"])


@router.get(
    "",
    response_model=ApiResponse[WebhookConfigView],
    summary="读取 Webhook 配置与事件目录",
    operation_id="webhook.show",
)
async def get_webhook_config() -> ApiResponse[WebhookConfigView]:
    return ok(await webhook_config.build_config_view())


@router.put(
    "",
    response_model=ApiResponse[WebhookConfigView],
    summary="保存 Webhook 配置（新建 endpoint 的 secret 仅本次返回明文）",
    operation_id="webhook.set",
)
async def save_webhook_config(
    payload: WebhookConfigPayload,
) -> ApiResponse[WebhookConfigView]:
    return ok(await webhook_config.save_config(payload))


@router.post(
    "/endpoints/{endpoint_id}/rotate-secret",
    response_model=ApiResponse[WebhookEndpointView],
    summary="轮换签名密钥（旧密钥即刻作废，新明文仅本次返回）",
    operation_id="webhook.rotate-secret",
)
async def rotate_webhook_secret(endpoint_id: str) -> ApiResponse[WebhookEndpointView]:
    return ok(await webhook_config.rotate_secret(endpoint_id))


@router.post(
    "/endpoints/{endpoint_id}/test",
    response_model=ApiResponse[DeliveryView],
    summary="发送测试事件（同步返回状态码/耗时/错误）",
    operation_id="webhook.test",
)
async def test_webhook_endpoint(endpoint_id: str) -> ApiResponse[DeliveryView]:
    return ok(await webhook_config.send_test(endpoint_id))


@router.get(
    "/endpoints/{endpoint_id}/deliveries",
    response_model=ApiResponse[list[DeliveryView]],
    summary="最近投递记录（每端点保留 50 条，重启清空）",
    operation_id="webhook.deliveries",
)
async def list_webhook_deliveries(endpoint_id: str) -> ApiResponse[list[DeliveryView]]:
    return ok(await webhook_config.list_deliveries(endpoint_id))
