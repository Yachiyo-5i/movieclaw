from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field, field_serializer, field_validator

from movieclaw_api.schemas.base import BaseModel
from movieclaw_db.models.llm_provider import LlmProvider
from movieclaw_db.models.site_credential import ConfigStatus
from movieclaw_llm.models import ModelInfo, ProviderPreset
from movieclaw_llm.protocols.openai_chat import sdk_default_user_agent


class LlmPresetView(BaseModel):
    """供应商预设的对外视图：设置页用它渲染类型选项与模型目录。"""

    id: str
    display_name: str
    #: 预设默认端点；None 表示必须由用户填写（openai_compat）或走 SDK 官方默认
    base_url: str | None = None
    #: 该预设是否必须填 base_url（通用兼容端点没有默认值）
    requires_base_url: bool
    #: 用户不覆盖 User-Agent 时实际发送的 SDK 自带 UA（设置页占位提示用）
    default_user_agent: str = Field(default_factory=sdk_default_user_agent)
    models: list[ModelInfo] = Field(default_factory=list)

    @classmethod
    def from_preset(cls, preset: ProviderPreset) -> LlmPresetView:
        return cls(
            id=preset.id,
            display_name=preset.display_name,
            base_url=preset.base_url,
            requires_base_url=preset.requires_base_url,
            models=preset.models,
        )


class LlmProviderView(BaseModel):
    """LLM 供应商配置的对外视图（**脱敏**：绝不回传 API Key）。"""

    provider_type: str
    base_url: str | None = None
    user_agent: str | None = Field(
        default=None, description="自定义 User-Agent；null 表示用 SDK 默认 UA"
    )
    default_model: str
    status: ConfigStatus
    usable: bool = Field(description="是否可用 = 连接测试通过（status=active）")
    last_error: str | None = Field(default=None, description="最近测试失败原因（清晰中文）")
    last_checked_at: datetime | None = None
    available_models: list[str] | None = Field(
        default=None, description="最近验证成功时端点上报的可用模型列表"
    )
    extra_models: list[ModelInfo] = Field(
        default_factory=list, description="用户补录的自定义模型目录（含参数）"
    )
    created_at: datetime
    updated_at: datetime

    @field_serializer("last_checked_at", "created_at", "updated_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        """库内 naive UTC 补时区标记再输出，理由见 schemas.site.ConfiguredSite。"""
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()

    @classmethod
    def from_model(cls, row: LlmProvider) -> LlmProviderView:
        """从 ORM 记录构造脱敏视图。只挑选可公开字段，天然屏蔽密钥密文。"""
        return cls(
            provider_type=row.provider_type,
            base_url=row.base_url,
            user_agent=row.user_agent,
            default_model=row.default_model,
            status=row.status,
            usable=row.status == ConfigStatus.ACTIVE,
            last_error=row.last_error,
            last_checked_at=row.last_checked_at,
            available_models=row.available_models,
            extra_models=[ModelInfo.model_validate(m) for m in row.extra_models or []],
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class LlmProviderPayload(BaseModel):
    """保存 LLM 供应商配置的请求体（单例：PUT 即 upsert）。

    API Key 出于安全不回显，编辑时需要重新填写。
    """

    provider_type: str = Field(description="供应商类型：openai / bailian / openai_compat")
    base_url: str | None = Field(default=None, description="API 端点（留空用预设默认）")
    user_agent: str | None = Field(
        default=None,
        max_length=200,
        description="自定义 User-Agent 请求头（留空使用 openai SDK 自带 UA）",
    )
    api_key: str = Field(min_length=1, description="API Key")
    default_model: str = Field(min_length=1, description="默认使用的模型 id")
    # 自定义端点的 default_model 必须能在这里找到——只有裸 id 没有参数，
    # agent 无法做预算决策
    extra_models: list[ModelInfo] = Field(
        default_factory=list,
        description="自定义模型目录 JSON 数组，元素形如 "
        '{"id":"模型id","context_window":131072,"max_output_tokens":8192}'
        "（openai_compat 端点必填，且须包含 default_model 对应的条目）",
    )

    @field_validator(
        "provider_type", "base_url", "user_agent", "api_key", "default_model", mode="before"
    )
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        """去除首尾空白；空串归一为 None（可选字段"没填"的统一表达）。"""
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("API 端点必须以 http:// 或 https:// 开头")
        return value.rstrip("/")

    @field_validator("user_agent")
    @classmethod
    def _validate_user_agent(cls, value: str | None) -> str | None:
        """请求头值只允许可打印 ASCII。

        换行会造成请求头注入，中文等非 ASCII 字符会被 HTTP 客户端直接拒绝——
        两类都在入口拦下，比等到调模型时报一句看不懂的底层异常更友好。
        """
        if value is None:
            return None
        if any(ch < " " or ch > "~" for ch in value):
            raise ValueError("User-Agent 只能包含可打印的 ASCII 字符（不能含换行或中文）")
        return value
