"""播放记录在 Web 业务界面的响应模型。"""

from __future__ import annotations

from datetime import datetime

from movieclaw_api.schemas.base import BaseModel
from movieclaw_media.models import MediaKind


class RecentWatchItemView(BaseModel):
    """媒体库首页的一张最近观看卡片。"""

    media_item_id: int
    library_id: int
    kind: MediaKind
    title: str
    year: int | None
    poster_url: str | None
    backdrop_url: str | None
    episode_still_url: str | None
    season_number: int
    episode_number: int
    episode_title: str | None
    # 锚点之后、当前成员从未看过且文件在位的分集数——“还能接着看几集”，
    # 不是“最近入库了几集”：看完全剧、补齐旧季与洗版都不该触发提醒。
    unwatched_ahead_count: int
    position_ms: int
    duration_ms: int | None
    progress_percent: int | None
    played: bool
    play_count: int
    last_played_at: datetime


class RecentWatchView(BaseModel):
    """最近观看横排的数据载荷。"""

    items: list[RecentWatchItemView]
