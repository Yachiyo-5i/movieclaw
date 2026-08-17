"""M-Team 积分商城「单种免费」（mallSingleFree）免费判定单元测试。

背景：用户可花积分给单个种子购买限时免费（常与置顶同时生效）。这种免费
叠加在常规促销之上——生效期间网页显示 FREE 倒计时，但 ``discount`` 字段
保持原促销值不变（如 PERCENT_50）。适配器若只看 ``discount``，会把免费种
误判为折扣种（真实案例：种子 239155，网页 FREE 但应用按五折处理）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from movieclaw_tracker import load_all_sites
from movieclaw_tracker.models import SearchQuery, TorrentCategory
from movieclaw_tracker.registry import get_site_config
from movieclaw_tracker.sites.custom.mteam import MTeamSite


@pytest.fixture(scope="module", autouse=True)
def _load_configs() -> None:
    load_all_sites()


def _torrent_payload(mall_status: str | None) -> dict[str, Any]:
    """按真实 API 响应结构构造一条 discount=PERCENT_50 的种子数据。"""
    status: dict[str, Any] = {
        "discount": "PERCENT_50",
        "discountEndTime": None,
        "seeders": "335",
        "leechers": "8",
        "timesCompleted": "722",
    }
    if mall_status is not None:
        status["mallSingleFree"] = {
            "status": mall_status,
            "startDate": "2026-08-16 10:12:27",
            "endDate": "2026-08-19 10:12:27",
        }
    return {
        "id": "239155",
        "name": "MIDE-548 早漏イクイク敏感4SEX 七沢みあ",
        "smallDescr": "",
        "category": "410",
        "size": "10695618415",
        "createdDate": "2018-05-26 11:02:07",
        "status": status,
    }


def _mock_site(payload: dict[str, Any]) -> MTeamSite:
    config = get_site_config("mteam")
    response = MagicMock()
    response.json.return_value = {
        "code": "0",
        "message": "SUCCESS",
        "data": {"data": [payload], "totalPages": 1},
    }
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    return MTeamSite(
        site_id=config.site_id,
        base_url=config.base_url,
        web_base_url=config.web_base_url,
        category_map=config.category_map,
        client=client,
        auth_manager=MagicMock(),
    )


async def _search_single(site: MTeamSite):
    result = await site.search(
        SearchQuery(keyword="MIDE-548", categories=[TorrentCategory.AV])
    )
    assert len(result.items) == 1
    return result.items[0]


async def test_ongoing_mall_free_marks_torrent_free() -> None:
    item = await _search_single(_mock_site(_torrent_payload("ONGOING")))

    assert item.free is True
    assert item.download_volume_factor == 0.0
    # 免费截止时间取商城免费的 endDate（而非为空的 discountEndTime）
    assert item.free_deadline is not None
    # 上传系数不受商城免费影响
    assert item.upload_volume_factor == 1.0


async def test_finished_mall_free_keeps_regular_discount() -> None:
    item = await _search_single(_mock_site(_torrent_payload("FINISHED")))

    assert item.free is False
    assert item.download_volume_factor == 0.5
    assert item.free_deadline is None


async def test_absent_mall_free_keeps_regular_discount() -> None:
    item = await _search_single(_mock_site(_torrent_payload(None)))

    assert item.free is False
    assert item.download_volume_factor == 0.5


async def test_detail_applies_mall_free() -> None:
    site = _mock_site(_torrent_payload("ONGOING"))
    # detail 接口返回单条数据（非列表包装），复用同一份种子数据
    site.client.post.return_value.json.return_value = {
        "code": "0",
        "message": "SUCCESS",
        "data": _torrent_payload("ONGOING"),
    }

    detail = await site.get_torrent_detail("239155")

    assert detail.free is True
    assert detail.download_volume_factor == 0.0
    assert detail.free_deadline is not None
