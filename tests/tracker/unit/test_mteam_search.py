"""M-Team 成人/普通索引路由单元测试。

M-Team 的成人内容位于独立 ``mode=adult`` 索引。H-Game、H-动画和
H-漫画虽然也有游戏/动漫语义，但站点分类 ID 只能在成人索引中搜索；
这里直接验证最终 API 请求，避免分类配置与适配器路由再次错配。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from movieclaw_tracker import load_all_sites
from movieclaw_tracker.models import SearchQuery, TorrentCategory
from movieclaw_tracker.registry import get_site_config
from movieclaw_tracker.sites.custom.mteam import MTeamSite


@pytest.fixture(scope="module", autouse=True)
def _load_configs() -> None:
    load_all_sites()


def _mock_site() -> tuple[MTeamSite, AsyncMock]:
    config = get_site_config("mteam")
    response = MagicMock()
    response.json.return_value = {
        "code": "0",
        "message": "SUCCESS",
        "data": {"data": [], "totalPages": 0},
    }
    post = AsyncMock(return_value=response)
    client = MagicMock()
    client.post = post
    site = MTeamSite(
        site_id=config.site_id,
        base_url=config.base_url,
        web_base_url=config.web_base_url,
        category_map=config.category_map,
        client=client,
        auth_manager=MagicMock(),
    )
    return site, post


async def test_adult_search_routes_h_categories_to_adult_index() -> None:
    site, post = _mock_site()

    await site.search(
        SearchQuery(keyword="Senior Year Love Story", categories=[TorrentCategory.AV])
    )

    payload = post.call_args.kwargs["json"]
    assert payload["mode"] == "adult"
    assert {"411", "412", "413"} <= set(payload["categories"])


@pytest.mark.parametrize(
    ("category", "expected_ids"),
    [
        (TorrentCategory.ANIME, ["405"]),
        (TorrentCategory.GAME, ["423"]),
    ],
)
async def test_normal_anime_and_game_stay_in_normal_index(
    category: TorrentCategory,
    expected_ids: list[str],
) -> None:
    site, post = _mock_site()

    await site.search(SearchQuery(keyword="test", categories=[category]))

    payload = post.call_args.kwargs["json"]
    assert payload["categories"] == expected_ids
    assert "mode" not in payload


async def test_unscoped_search_covers_both_indices() -> None:
    site, post = _mock_site()

    await site.search(SearchQuery(keyword="test"))

    assert post.await_count == 2
    normal_payload = post.await_args_list[0].kwargs["json"]
    adult_payload = post.await_args_list[1].kwargs["json"]
    assert normal_payload["categories"] == []
    assert "mode" not in normal_payload
    assert adult_payload["mode"] == "adult"
    assert {"411", "412", "413"} <= set(adult_payload["categories"])
