"""AGSVPT 内置 NexusPHP 配置回归测试。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from parsel import Selector

from movieclaw_tracker.frameworks.nexusphp import NexusPHPSite
from movieclaw_tracker.models import TorrentCategory
from movieclaw_tracker.registry import get_site_config


@pytest.fixture(scope="module", autouse=True)
def _load_configs() -> None:
    """加载内置 YAML 配置，验证 AGSVPT 会被注册。"""
    from movieclaw_tracker import load_all_sites

    load_all_sites()


def test_agsvpt_uses_expected_nexusphp_configuration() -> None:
    """AGSVPT 仅覆盖实测差异，其他 NexusPHP 行为继续复用默认值。"""
    config = get_site_config("agsvpt")

    assert config.site_class is NexusPHPSite
    assert config.base_url == "https://www.agsvpt.com"
    assert config.supported_auth_types == ("cookie", "credential")
    assert config.category_map == {
        TorrentCategory.MOVIE: ["401"],
        TorrentCategory.TV: ["402", "403", "407", "419"],
        TorrentCategory.DOCUMENTARY: ["404"],
        TorrentCategory.ANIME: ["405"],
        TorrentCategory.MUSIC: ["406", "408", "411"],
        TorrentCategory.GAME: ["413"],
        TorrentCategory.OTHER: ["415"],
    }

    selectors = config.selectors
    assert selectors is not None
    assert selectors.page_offset == -1
    assert selectors.torrent_row_css == "table.torrents > tr:has(table.torrentname)"
    assert selectors.torrent_category_css == "a[href^='?cat=']::attr(href)"
    assert selectors.detail_size_css == (
        "td.rowhead:contains('基本信息') + td.rowfollow > b::next_sibling_text"
    )
    assert selectors.profile_uid_css == "a[class*='User_Name']::attr(href)"
    assert selectors.profile_username_css == "a[class*='User_Name']"
    assert selectors.profile_class_css == "a[class*='User_Name']::attr(class)"
    assert selectors.profile_uploaded_css == "font.color_uploaded::next_sibling_text"
    assert selectors.profile_downloaded_css == "font.color_downloaded::next_sibling_text"
    assert selectors.profile_ratio_css == "font.color_ratio::next_sibling_text"
    assert selectors.profile_seeding_css == "img[alt='Torrents seeding']::next_sibling_text"
    assert selectors.profile_leeching_css == "img[alt='Torrents leeching']::next_sibling_text"


async def test_agsvpt_selectors_parse_representative_nexusphp_markup() -> None:
    """站点覆盖的选择器应继续由通用 NexusPHP 解析器消费。"""
    config = get_site_config("agsvpt")
    assert config.selectors is not None
    site = NexusPHPSite(
        selectors=config.selectors,
        category_map=config.category_map,
        site_id=config.site_id,
        base_url=config.base_url,
        client=MagicMock(),
        auth_manager=MagicMock(),
    )
    torrent_doc = Selector(
        text="""\
        <table class="torrents"><tr>
          <td><a href="?cat=401"><img alt="Movie(电影)"></a></td>
          <td><table class="torrentname"><tr><td class="embedded">
            <a href="details.php?id=123">Example Movie</a>
            <img class="pro_free">
            <a href="download.php?id=123">download</a>
          </td></tr></table></td>
          <td>0</td><td><span title="2026-08-09 12:00:00">刚刚</span></td>
          <td>2.62 GB</td><td>2</td><td>7</td><td>22</td><td><a>Uploader</a></td>
        </tr></table>
        """
    )

    [item] = site._parse_torrent_rows(torrent_doc)

    assert item.torrent_id == "123"
    assert item.title == "Example Movie"
    assert item.category is TorrentCategory.MOVIE
    assert item.free is True
    assert item.download_url == "https://www.agsvpt.com/download.php?id=123"
    assert item.size == "2.62 GB"
    assert item.seeders == 2
    assert item.leechers == 7
    assert item.snatched == 22

    detail_doc = Selector(
        text="""\
        <table><tr><td class="rowhead">基本信息</td>
        <td class="rowfollow"><b>大小</b> 2.62 GB</td></tr></table>
        """
    )
    profile_doc = Selector(
        text="""\
        <a class="User_Name" href="userdetails.php?id=42">user</a>
        <font class="color_uploaded">上传量：</font> 1.50 TB
        <font class="color_downloaded">下载量：</font> 500 GB
        <font class="color_ratio">分享率：</font> 3.00
        <img alt="Torrents seeding">20
        <img alt="Torrents leeching">1
        """
    )

    assert site._field_optional_str(detail_doc, config.selectors.detail_size_css) == "2.62 GB"
    assert site._field_str(profile_doc, config.selectors.profile_username_css) == "user"
    assert site._field_str(profile_doc, config.selectors.profile_class_css) == "User_Name"
    assert site._field_str(profile_doc, config.selectors.profile_uploaded_css) == "1.50 TB"
    assert site._field_str(profile_doc, config.selectors.profile_downloaded_css) == "500 GB"
    assert site._field_str(profile_doc, config.selectors.profile_ratio_css) == "3.00"
    assert site._field_int(profile_doc, config.selectors.profile_seeding_css) == 20
    assert site._field_int(profile_doc, config.selectors.profile_leeching_css) == 1

    response = MagicMock()
    response.text = profile_doc.get()
    site.client.get = AsyncMock(return_value=response)

    profile = await site.get_user_profile()

    assert profile.user_id == "42"
    assert profile.username == "user"
    assert profile.user_class == "User_Name"
