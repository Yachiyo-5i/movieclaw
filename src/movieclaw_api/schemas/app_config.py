"""应用设置的请求/响应模型（「设置 → 应用设置」页）。"""

from __future__ import annotations

from pydantic import Field

from movieclaw_api.schemas.base import BaseModel


class AppConfigPayload(BaseModel):
    """保存请求体：与 ``AppServerSetting`` 配置域字段一一对应（整体覆盖语义）。"""

    external_url: str = Field(
        default="",
        description="网络可访问到本应用的完整地址（http/https），"
        "如 http://192.168.1.10:3000；空 = 未配置",
    )


class AppConfigView(AppConfigPayload):
    """读取响应：当前与保存请求体同构，独立成类为后续扩展运行时状态留位。"""
