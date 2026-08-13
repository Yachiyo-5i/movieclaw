from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

from movieclaw_db.models.downloader_client import ClientType, DownloaderClient
from movieclaw_db.models.site_credential import ConfigStatus


class PathMapping(BaseModel):
    """一条路径映射：movieclaw 视角的目录前缀 → 下载器视角的对应前缀。

    跨容器/跨主机部署时同一块盘两边挂载路径不同（movieclaw 看到
    ``/data/downloads``，下载器容器里是 ``/downloads``），提交下载前
    按最长前缀把保存目录翻译成下载器视角。视角一致的部署无需配置。
    """

    local: str = Field(min_length=1, description="movieclaw 上的路径前缀")
    remote: str = Field(min_length=1, description="下载器上的对应路径前缀")

    @field_validator("local", "remote")
    @classmethod
    def _validate_abs(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith("/"):
            raise ValueError("路径映射两端都必须是以 / 开头的绝对路径")
        return value


class DownloaderView(BaseModel):
    """下载器配置的对外视图（**脱敏**：绝不回传密码）。"""

    id: int
    name: str
    client_type: ClientType
    url: str
    username: str | None = None
    save_path: str | None = Field(default=None, description="提交下载时的默认保存目录")
    path_mappings: list[PathMapping] | None = Field(
        default=None,
        description="路径映射 JSON 数组（movieclaw 路径 → 下载器路径），"
        '形如 [{"local":"/volume1/downloads","remote":"/downloads"}]',
    )
    enabled: bool
    is_default: bool = Field(description="是否为默认下载器（一键下载不选目标时投给它）")
    status: ConfigStatus
    usable: bool = Field(description="是否可用 = 已启用且连接测试通过（status=active）")
    version: str | None = Field(default=None, description="最近一次连接成功获取的版本号")
    last_error: str | None = Field(default=None, description="最近测试失败原因（清晰中文）")
    last_checked_at: datetime | None = Field(default=None, description="最近一次测试时间")
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
    def from_model(cls, row: DownloaderClient) -> DownloaderView:
        """从 ORM 记录构造脱敏视图。只挑选可公开字段，天然屏蔽密码密文。"""
        return cls(
            id=row.id,  # type: ignore[arg-type]  # 落库后必有主键
            name=row.name,
            client_type=row.client_type,
            url=row.url,
            username=row.username,
            save_path=row.save_path,
            path_mappings=row.path_mappings,  # type: ignore[arg-type]  # 落库前已按 PathMapping 规范化
            enabled=row.enabled,
            is_default=row.is_default,
            status=row.status,
            usable=row.enabled and row.status == ConfigStatus.ACTIVE,
            version=row.version,
            last_error=row.last_error,
            last_checked_at=row.last_checked_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class DownloadTaskUnitView(BaseModel):
    """订阅下载覆盖的一个追踪单元；电影沿用 0/0 哨兵。"""

    season_number: int
    episode_number: int


class DownloadTaskSubscriptionView(BaseModel):
    """下载器任务关联的订阅摘要，供任务中心回到业务上下文。"""

    id: int
    media_item_id: int
    media_title: str
    media_kind: str
    poster_url: str | None = None
    units: list[DownloadTaskUnitView] = Field(default_factory=list)


class DownloadTaskView(BaseModel):
    """任务中心使用的下载器实时快照。

    下载器仍是下载状态的事实源；这里只在请求时汇总快照，并通过 infohash
    关联订阅工单或手动下载意图，不把下载进度复制进本地数据库。
    """

    id: str = Field(description="稳定前端键：下载器 ID + infohash；缺失任务用 missing 前缀")
    info_hash: str
    name: str | None
    downloader_id: int | None
    downloader_name: str | None
    downloader_type: ClientType | None
    progress: float | None = Field(default=None, ge=0, le=1)
    size_bytes: int | None = None
    dlspeed_bytes: int | None = None
    eta_seconds: int | None = None
    state: Literal["downloading", "stalled", "paused", "completed", "error", "missing", "unknown"]
    source: Literal["subscription", "manual", "external"]
    media_item_id: int | None = None
    media_title: str | None = None
    media_kind: str | None = None
    poster_url: str | None = None
    subscriptions: list[DownloadTaskSubscriptionView] = Field(default_factory=list)


class DownloadTaskSourceView(BaseModel):
    """一台下载器在本次快照中的可观测状态；单台故障不拖垮整页。"""

    id: int
    name: str
    client_type: ClientType
    status: Literal["active", "disabled", "unavailable", "error"]
    message: str | None = None
    task_count: int = 0


class DownloadTaskListView(BaseModel):
    items: list[DownloadTaskView]
    sources: list[DownloadTaskSourceView]


class DownloadTaskDeleteView(BaseModel):
    """删除下载器任务的结果。"""

    downloader_id: int
    info_hash: str
    delete_files: bool


class DownloaderPayload(BaseModel):
    """新增/更新下载器的请求体（更新时 id 走路径参数）。

    与站点配置同语义：更新是**全字段覆盖**，密码出于安全不回显，
    编辑时需要重新填写（未填则视为该下载器无需密码）。
    """

    name: str = Field(min_length=1, max_length=50, description="下载器名称（全局唯一）")
    client_type: ClientType = Field(description="下载器类型：qbittorrent / transmission")
    url: str = Field(description="下载器地址，如 http://192.168.1.10:8080")
    username: str | None = Field(default=None, description="登录用户名（未开鉴权可留空）")
    password: str | None = Field(default=None, description="登录密码（未开鉴权可留空）")
    save_path: str | None = Field(default=None, description="默认保存目录（留空用下载器默认）")
    path_mappings: list[PathMapping] | None = Field(
        default=None,
        description="路径映射（movieclaw 路径 → 下载器路径，视角一致时留空）",
    )
    enabled: bool = Field(default=True, description="是否启用（默认启用）")

    @field_validator("name", "url", "username", "password", "save_path", mode="before")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        """去除首尾空白；空串归一为 None（可选字段"没填"的统一表达）。"""
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("下载器地址必须以 http:// 或 https:// 开头")
        return value.rstrip("/")

    @field_validator("path_mappings")
    @classmethod
    def _normalize_mappings(cls, value: list[PathMapping] | None) -> list[PathMapping] | None:
        """空列表归一为 None；两端各自查重。

        同一 movieclaw 路径配两条映射，翻译结果取决于遍历顺序（纯配置错误）；
        两条映射指向同一下载器路径同样是错配。PathMapping 校验已去掉尾部斜杠，
        这里的比较天然把 ``/data/`` 与 ``/data`` 视为重复。
        """
        if not value:
            return None
        locals_ = [m.local for m in value]
        if len(set(locals_)) != len(locals_):
            raise ValueError("路径映射中 movieclaw 路径不能重复")
        remotes = [m.remote for m in value]
        if len(set(remotes)) != len(remotes):
            raise ValueError("路径映射中下载器路径不能重复")
        return value


class DownloaderStatusUpdate(BaseModel):
    """启用/停用请求体。"""

    enabled: bool = Field(description="true=启用 / false=停用（停用后不接收新的下载提交）")


class DownloadSubmitPayload(BaseModel):
    """手动提交下载的请求体：搜索结果里的一条种子。

    site_id + download_url 均来自搜索接口返回的 TorrentHit，后端凭它们
    带站点登录态取回 .torrent 字节再递交下载器。
    """

    site_id: str = Field(min_length=1, description="种子所属站点 ID（TorrentHit.site_id）")
    download_url: str = Field(min_length=1, description="种子下载入口（TorrentHit.download_url）")
    # 入库目标（可选）：带 library_id 时保存目录改为由库推导（主根/标题 (年份)）。
    # title/year 来自搜索结果的解析实体；无法确定条目身份时只带 library_id，
    # 落到库主根目录。三者都缺省 = 维持原行为（下载器默认目录）。
    library_id: int | None = Field(default=None, description="入库到哪个媒体库")
    title: str | None = Field(default=None, description="条目标题（推导条目子目录用）")
    year: int | None = Field(default=None, description="条目年份")
    # 副标题作识别信号：落 download_hint，扫描器用其中的中文片名/「全N集」
    # 收敛拼音命名种子（TorrentHit.subtitle 原样带过来即可）
    subtitle: str | None = Field(default=None, description="种子副标题（识别线索用）")
    # 用户在下载弹窗里手选的保存目录（movieclaw 视角）：给出时优先于库推导，
    # 提交前照常过路径映射翻译与覆盖守门
    save_path: str | None = Field(default=None, description="手选保存目录（覆盖库推导）")
    # 指定投递到哪台下载器（配了多台按需分流）；缺省走默认下载器
    downloader_id: int | None = Field(default=None, description="指定下载器；缺省用默认下载器")
    # 智能入库由服务端重新按 TMDB 身份路由，不能信任前端预检返回的 library_id。
    # 成功提交后会按 infohash 锚定身份，供共享监听目录完成后直接认领。
    auto_route: bool = Field(default=False, description="按确认的 TMDB 身份自动匹配媒体库并投递")
    media_kind: Literal["movie", "tv"] | None = Field(
        default=None, description="智能入库的媒体类型"
    )
    tmdb_id: int | None = Field(default=None, description="智能入库已确认的 TMDB 条目 ID")

    @field_validator("save_path")
    @classmethod
    def _validate_save_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        if not value.startswith("/"):
            raise ValueError("保存目录必须是以 / 开头的绝对路径")
        return value

    @model_validator(mode="after")
    def _validate_auto_route(self) -> DownloadSubmitPayload:
        """智能入库必须是一组完整、不可被手选目录覆盖的身份锚。"""
        if not self.auto_route:
            return self
        if self.media_kind is None or self.tmdb_id is None or not self.title:
            raise ValueError("智能入库必须提供媒体类型、TMDB ID 和标题")
        if self.library_id is not None or self.save_path is not None:
            raise ValueError("智能入库不能同时指定媒体库或保存目录")
        return self


class ManualDownloadTargetPayload(BaseModel):
    """手动下载的识别预检输入：只接受搜索结果已解析出的最小身份线索。"""

    kind: Literal["movie", "tv"] = Field(description="搜索结果识别出的媒体类型")
    title: str = Field(min_length=1, description="搜索结果识别出的主标题")
    year: int = Field(ge=1888, le=2100, description="搜索结果识别出的发行/首播年份")
    subtitle: str | None = Field(default=None, description="种子副标题（中文别名等识别补强）")
    # 缺省按默认下载器预检，与 dl submit 的既有语义一致；前端显式切换
    # 下载器时带上它，确保路径映射的预检结论与真实提交是同一台机器。
    downloader_id: int | None = Field(
        default=None, ge=1, description="预检指定下载器；缺省用默认下载器"
    )
    # 歧义时只能从本次返回的候选中确认一个 ID，服务端会再次校验，不能把
    # 任意 TMDB ID 当成已识别结果直接放行。
    selected_tmdb_id: int | None = Field(
        default=None, ge=1, description="用户从本次识别候选中确认的 TMDB 条目 ID"
    )


class ManualDownloadCandidateView(BaseModel):
    """预检未收敛时留给用户确认的 TMDB 候选。"""

    tmdb_id: int
    title: str
    year: int | None = None
    episode_count: int | None = None


class ManualDownloadTargetView(BaseModel):
    """手动下载的「识别 → 路由 → 投递目录」预检结论。"""

    status: Literal["ready", "ambiguous", "not_found"]
    tmdb_id: int | None = None
    candidates: list[ManualDownloadCandidateView] = Field(default_factory=list)
    library_id: int | None = None
    library_name: str | None = None
    mode: Literal["watch", "inplace", "downloader_default"] | None = None
    path: str | None = Field(default=None, description="movieclaw 视角的实际投递目录")
    staging_path: str | None = Field(default=None, description="自定义目录规则的整理落点")
    route_matched: bool | None = Field(default=None, description="是否命中媒体库收藏范围")
    route_reason: str | None = Field(default=None, description="媒体库路由理由")
    ok: bool = Field(default=False, description="当前选择的下载器和投递配置能否自动入库")
    warning: str | None = Field(default=None, description="不可自动入库时的中文指引")


class DownloadSubmitView(BaseModel):
    """手动提交下载的结果视图。"""

    info_hash: str | None = Field(description="种子 infohash（极少数纯 v2 磁力无法解析时为空）")
    name: str = Field(description="下载器中的任务名称（提交后未能立即回查到时为空）")
    already_exists: bool = Field(description="种子提交前已存在于下载器（幂等，未重复添加）")
    downloader_id: int = Field(description="接收本次提交的下载器 ID")
    downloader_name: str = Field(description="接收本次提交的下载器名称")
    save_path: str | None = Field(
        description="实际使用的保存目录（下载器视角，已过路径映射；空 = 下载器自身默认目录）"
    )
