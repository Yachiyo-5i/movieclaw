"""M-Team API 业务错误分类单元测试。

M-Team 统一返回 HTTP 200 + ``{"code", "message", "data"}``，业务失败只体现在
message 上。其中「帳號已停用」「Key 失效」这类凭据类错误必须抛 TrackerAuthError
（上层据此快速熔断并提醒用户），不能与站点改版类的 TrackerParseError 混为一谈——
否则用户会看到误导性的「站点响应格式异常」，且站点状态长期停留在「已验证」。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from movieclaw_tracker import load_all_sites
from movieclaw_tracker.exceptions import TrackerAuthError, TrackerParseError
from movieclaw_tracker.registry import get_site_config
from movieclaw_tracker.sites.custom.mteam import MTeamSite


@pytest.fixture(scope="module", autouse=True)
def _load_configs() -> None:
    load_all_sites()


def _site_with_response(payload: dict) -> MTeamSite:
    config = get_site_config("mteam")
    response = MagicMock()
    response.json.return_value = payload
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


@pytest.mark.parametrize(
    "message",
    ["帳號已停用", "無效的 API Key", "Unauthorized", "FORBIDDEN", "用户已被封禁"],
)
async def test_credential_class_errors_raise_auth_error(message: str) -> None:
    site = _site_with_response({"code": "1", "message": message, "data": None})

    with pytest.raises(TrackerAuthError) as exc_info:
        await site._post_api("/api/torrent/search", json={})

    # 站点原始 message 必须透传给用户，不能被吞成笼统文案
    assert message in exc_info.value.message


async def test_other_business_errors_raise_parse_error() -> None:
    site = _site_with_response({"code": "1", "message": "參數錯誤", "data": None})

    with pytest.raises(TrackerParseError) as exc_info:
        await site._post_api("/api/torrent/search", json={})

    assert "參數錯誤" in exc_info.value.message


async def test_non_json_response_raises_parse_error() -> None:
    config = get_site_config("mteam")
    response = MagicMock()
    response.json.side_effect = ValueError("not json")
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    site = MTeamSite(
        site_id=config.site_id,
        base_url=config.base_url,
        web_base_url=config.web_base_url,
        category_map=config.category_map,
        client=client,
        auth_manager=MagicMock(),
    )

    with pytest.raises(TrackerParseError):
        await site._post_api("/api/torrent/search", json={})
