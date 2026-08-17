from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, UniqueConstraint

from movieclaw_db.models.base import TimestampMixin


class RatioBoostStat(TimestampMixin, table=True):
    """刷流的小时桶统计：每站每小时一行，支撑「近 24 小时 / 近 7 天，
    用 X GB 种子贡献了 Y GB 上传」的过程指标展示。

    台账（RatioBoostTask）只有累计值，回答不了"最近产出如何"；本表由
    引擎每 tick 累加：

    - ``uploaded_bytes``：该小时内全站刷流任务的上传增量之和（差分口径，
      与效率 EMA 同源，不重复计量）；
    - ``used_bytes_sum`` / ``tick_count``：每 tick 采样一次在池占用并累加，
      两者相除即该小时的**平均在池体积**——"用了多少种子"的口径是时间
      平均，不是瞬时值（tick 间预算会因准入/汰换波动）。

    行数可控：每站每天 24 行，保留 30 天后由引擎顺手清理。
    """

    __tablename__ = "ratio_boost_stat"
    __table_args__ = (UniqueConstraint("site_id", "bucket_start"),)

    id: int | None = Field(default=None, primary_key=True)
    site_id: str = Field(index=True, description="站点标识")
    # 小时对齐的桶起点（naive UTC，与全库时间约定一致）
    bucket_start: datetime = Field(index=True, description="小时桶起点（UTC 整点）")
    uploaded_bytes: int = Field(default=0, description="该小时的上传增量之和（字节）")
    used_bytes_sum: int = Field(default=0, description="各 tick 在池占用采样值之和（字节）")
    tick_count: int = Field(default=0, description="采样 tick 数；平均在池 = sum / count")
