"""人物页（/people/[tmdbPersonId]）的响应模型。

定位与条目详情页一致：回答「**我库里**这个人是谁、他参演/执导过我拥有的
哪些片」。数据全部来自本地（person / media_item_person / media_metadata），
不联网——断网、TMDB 挂掉、没配 API Key 都照样能用。因此这里没有生平简介、
出生地这类需要实时拉取的字段：那属于「这个人本身是什么」，与本页职责不同。
"""

from __future__ import annotations

from pydantic import Field

from movieclaw_api.schemas.base import BaseModel
from movieclaw_media.models import MediaKind


class PersonCreditView(BaseModel):
    """人物页作品列表的一格：一部我库里的片 + 这个人在其中的身份。"""

    media_item_id: int
    kind: MediaKind
    tmdb_id: int
    title: str
    year: int | None
    poster_url: str | None = Field(
        default=None, description="海报（优先本地刮削资产，回落 TMDB 图床）；都没有为 NULL"
    )
    library_id: int | None = Field(
        default=None,
        description=(
            "任一拥有该条目文件的库 id，供前端跳条目详情页；"
            "文件已全部删除、只剩档案时为 NULL，前端渲染为不可点"
        ),
    )
    department: str = Field(description="身份：cast=演员 / director=导演（剧集为主创）")
    character: str | None = Field(default=None, description="饰演角色（仅 cast）")


class PersonView(BaseModel):
    """人物页的完整数据。"""

    tmdb_person_id: int
    name: str
    original_name: str | None = None
    avatar_url: str | None = Field(
        default=None, description="头像（TMDB 图床，经前端缓存代理）；TMDB 无照片为 NULL"
    )
    credits: list[PersonCreditView] = Field(
        default_factory=list,
        description="库内作品，主演在前、同档按剧组主次顺序与年份倒序（见 PersonRepository）",
    )
