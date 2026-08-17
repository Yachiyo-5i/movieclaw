from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class BoostTaskState(StrEnum):
    """刷流任务的生命周期状态。

    - ``ACTIVE``：在下载器中（下载中或做种中），占用刷流预算。
    - ``EVICTED``：被引擎主动汰换删除（上传效率过低，连数据一起删）。
    - ``MISSING``：下载器里找不到了——用户手动删除是明确意图，引擎只让出
      预算、不追究也不重新抢回，避免与用户拉扯。

    EVICTED / MISSING 都是终态；台账保留下来既是"累计上传贡献"的统计来源，
    也是去重依据（同一 (site_id, torrent_id) 不会被再次抢下）。
    """

    ACTIVE = "active"
    EVICTED = "evicted"
    MISSING = "missing"


class RatioBoostTask(TimestampMixin, table=True):
    """刷流任务台账：自动刷分享率引擎抢下的每一个免费种子。

    设计要点（完整机制见 docs/design/site-protection-ratio-boost.md）：

    - **所有权铁律**：只有"提交前下载器中不存在、由引擎亲手加入"的种子才会
      入本表；提交时发现已存在（用户自己在下）的绝不入账，从而永远不会被
      自动删除——与订阅管线 ``owned_by_movieclaw`` 同一哲学。
    - **效率追踪**：每 tick 从下载器读一次全量列表，按 infohash 差分累计
      上传量得到上传速度 EMA。EMA 而非瞬时值：PT 上传是突发的，瞬时 0
      不代表没人要。
    - ``created_at``（Mixin）即入池时间，汰换的 72 小时最低保留期以它起算。
    """

    __tablename__ = "ratio_boost_task"

    id: int | None = Field(default=None, primary_key=True)
    # 站点与站内种子 ID：去重与统计的维度（同一种子汰换后不再抢回）
    site_id: str = Field(index=True, description="站点标识")
    torrent_id: str = Field(description="站点内种子 ID")
    # 种子 v1 infohash（40 位小写十六进制），与下载器对账的唯一键
    info_hash: str = Field(unique=True, index=True, description="种子 infohash（小写）")
    # 实际承载任务的下载器；删除下载器配置时置空，台账保留
    downloader_id: int | None = Field(
        default=None,
        foreign_key="downloader_client.id",
        ondelete="SET NULL",
        description="承载该任务的下载器",
    )
    title: str = Field(default="", description="种子标题（展示用）")
    # 入池时记录的种子体积，预算占用按它累加（下载器视角的体积可能因未选
    # 文件而略小，刷流不选文件，二者一致）
    size_bytes: int = Field(description="种子体积（字节）")

    state: BoostTaskState = Field(
        default=BoostTaskState.ACTIVE, index=True, description="任务状态"
    )
    # 下载是否已完成（完成才可能进入汰换候选）
    completed: bool = Field(default=False, description="下载是否完成")
    # 最近观测的累计上传量；差分驱动 EMA 更新
    uploaded_bytes: int = Field(default=0, description="累计上传量（字节）")
    # 上传速度 EMA（字节/秒），汰换排序的依据
    upload_rate_ema: float = Field(default=0.0, description="上传速度 EMA（字节/秒）")
    last_checked_at: datetime | None = Field(
        default=None, description="最近一次与下载器对账时间"
    )
    evicted_at: datetime | None = Field(default=None, description="汰换/失踪的时间")
    evict_reason: str | None = Field(default=None, description="汰换原因（中文，展示用）")
