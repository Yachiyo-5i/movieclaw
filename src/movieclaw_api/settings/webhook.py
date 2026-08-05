"""事件 Webhook 配置域（「设置 → Webhook」页的数据模型，docs/design/webhook.md §4.1）。

一条配置里挂一个 endpoint 列表（Stripe 式管理页的数据底座）。与其他配置域
的唯一不同点：secret 嵌套在列表元素里，而 ``register_setting(secret_fields=...)``
只加密**顶层**字段——因此本模型用 Pydantic 校验/序列化钩子自带加解密：

    落库（model_dump）→ 逐 endpoint 加密 secret；
    读取（model_validate）→ 逐 endpoint 解密。

调用方（投递器、管理 API）始终只接触明文模型，与其他配置域体验一致。
"""

from __future__ import annotations

import contextlib
import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from movieclaw_api.settings.base import SettingSchema, register_setting
from movieclaw_db.crypto import SecretBox, get_secret_box

WEBHOOK_NAMESPACE = "webhook"

# secret 前缀：便于用户在下游日志里一眼识别来源，也是打码展示的保留部分
_SECRET_PREFIX = "mcwh_"


def generate_webhook_secret() -> str:
    """生成一个 endpoint 签名密钥（``mcwh_`` + 32 字节随机 hex）。"""
    return f"{_SECRET_PREFIX}{secrets.token_hex(32)}"


def mask_webhook_secret(secret: str) -> str:
    """打码展示：保留前缀 + 前 4 位，其余星号。空值原样返回。"""
    if not secret:
        return ""
    visible = len(_SECRET_PREFIX) + 4
    return secret[:visible] + "****"


class WebhookEndpoint(BaseModel):
    """一个推送目标。format 决定外发报文形态（设计文档 §2 / §3）。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", description="服务端生成的 endpoint ID（ULID）")
    name: str = Field(default="", description="显示名，如 Yamtrack / Home Assistant")
    url: str = Field(default="", description="目标地址（HTTP POST）")
    format: Literal["movieclaw", "jellyfin"] = Field(
        default="movieclaw", description="外发格式：自有协议 / Jellyfin 兼容模板"
    )
    enabled: bool = Field(default=True, description="单端点开关")
    events: list[str] = Field(
        default_factory=list, description="订阅的事件名列表（事件目录中的精确名称）"
    )
    secret: str = Field(
        default="", description="HMAC 签名密钥（仅 movieclaw 格式；服务端生成，加密落库）"
    )
    template: str = Field(
        default="", description="Handlebars 模板（仅 jellyfin 格式；用户从下游文档粘贴）"
    )
    headers: dict[str, str] = Field(
        default_factory=dict, description="附加请求头（jellyfin 格式下游鉴权用）"
    )
    egress_scope: Literal["lan", "wan"] = Field(
        default="lan", description="出口范围：lan 内网直连（默认）/ wan 走代理配置"
    )


@register_setting(namespace=WEBHOOK_NAMESPACE, title="事件 Webhook")
class WebhookSetting(SettingSchema):
    """Webhook 总配置。默认关闭、零 endpoint，不影响存量部署。"""

    enabled: bool = Field(default=False, description="总开关")
    endpoints: list[WebhookEndpoint] = Field(default_factory=list)

    @field_validator("endpoints", mode="after")
    @classmethod
    def _decrypt_endpoint_secrets(
        cls, endpoints: list[WebhookEndpoint]
    ) -> list[WebhookEndpoint]:
        """从库读回时解密各 endpoint 的 secret；加密器未初始化时保持密文
        （fail-safe：宁可暂不可用，绝不明文落库）。"""
        for ep in endpoints:
            if ep.secret and SecretBox.is_encrypted(ep.secret):
                with contextlib.suppress(RuntimeError):
                    ep.secret = get_secret_box().decrypt(ep.secret)
        return endpoints

    @field_serializer("endpoints")
    def _encrypt_endpoint_secrets(self, endpoints: list[WebhookEndpoint]) -> list[dict]:
        """序列化（落库/导出）时加密 secret。加密器未初始化时抛错——
        这里静默放行等于把明文写进数据库，必须响亮失败。"""
        result = []
        for ep in endpoints:
            data = ep.model_dump()
            if data["secret"] and not SecretBox.is_encrypted(data["secret"]):
                data["secret"] = get_secret_box().encrypt(data["secret"])
            result.append(data)
        return result
