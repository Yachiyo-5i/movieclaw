"""TJUPT 配置与 NexusPHP 用户资料解析回归测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from parsel import Selector

from movieclaw_tracker import load_all_sites
from movieclaw_tracker.frameworks.nexusphp import NexusPHPSite
from movieclaw_tracker.registry import get_site_config
from movieclaw_tracker.selectors import NexusPHPSelectors


def test_tjupt_yaml_loads_credential_configuration() -> None:
    load_all_sites()

    config = get_site_config("tjupt")

    assert config.site_class is NexusPHPSite
    assert config.display_name == "北洋园 TJUPT"
    assert config.base_url == "https://tjupt.org"
    assert config.min_request_interval == 5.0
    assert config.supported_auth_types == ("cookie", "credential")
    assert isinstance(config.selectors, NexusPHPSelectors)
    assert config.selectors.login_select_defaults == (("logout", "7"),)
    assert config.selectors.profile_username_css == "a.User_Name b"
    assert config.selectors.profile_uid_css.endswith("::attr(href)")
    assert config.selectors.torrent_time_css == ":scope > td:nth-child(4)"
    assert config.selectors.torrent_size_css == ":scope > td:nth-child(5)"
    assert config.selectors.torrent_seeders_css == ":scope > td:nth-child(6)"
    assert config.selectors.profile_seeding_css == ""
    assert config.selectors.profile_leeching_css == ""


def test_tjupt_nine_outer_column_fixture_parses_torrent_row() -> None:
    """页面虽含 15 个后代 td，字段定位必须按 9 个外层列计算。"""
    load_all_sites()
    config = get_site_config("tjupt")
    assert isinstance(config.selectors, NexusPHPSelectors)
    site = NexusPHPSite(
        selectors=config.selectors,
        category_map=config.category_map,
        site_id=config.site_id,
        base_url=config.base_url,
        client=MagicMock(),
        auth_manager=MagicMock(),
    )
    doc = Selector(
        text="""
        <table class="torrents"><tr><th>类型</th></tr><tr>
          <td><a href="?cat=401"><img></a></td>
          <td><table><tr><td></td><td></td><td></td><td>
            <a href="details.php?id=42">Fixture title</a>
            <a href="download.php?id=42">download</a>
          </td><td></td><td></td></tr></table></td>
          <td>0</td><td>2026-08-12<br>09:11:34</td><td>1.50 GB</td>
          <td><a href="details.php?id=42">7</a></td><td>2</td><td>9</td><td>fixture-uploader</td>
        </tr></table>
        """
    )

    [item] = site._parse_torrent_rows(doc)

    assert item.torrent_id == "42"
    assert item.title == "Fixture title"
    assert item.site_category_id == "401"
    assert item.size == "1.50 GB"
    assert item.seeders == 7
    assert item.leechers == 2
    assert item.snatched == 9
    assert item.upload_time is not None


async def test_tjupt_profile_fixture_extracts_username_and_uid() -> None:
    load_all_sites()
    config = get_site_config("tjupt")
    assert isinstance(config.selectors, NexusPHPSelectors)
    client = MagicMock()
    response = MagicMock()
    response.text = """
    <a class="User_Name" href="userdetails.php?id=314159"><b>fixture-user</b></a>
    """
    client.get = AsyncMock(return_value=response)
    site = NexusPHPSite(
        selectors=config.selectors,
        category_map=config.category_map,
        site_id=config.site_id,
        base_url=config.base_url,
        client=client,
        auth_manager=MagicMock(),
    )

    profile = await site.get_user_profile()

    assert profile.username == "fixture-user"
    assert profile.user_id == "314159"
    client.get.assert_awaited_once_with("https://tjupt.org/userdetails.php", params={})
