"""CHDBits 发布时间时区归一回归测试。"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from parsel import Selector

from movieclaw_tracker import load_all_sites
from movieclaw_tracker.frameworks.nexusphp import NexusPHPSite
from movieclaw_tracker.registry import get_site_config
from movieclaw_tracker.selectors import NexusPHPSelectors


def test_chdbits_publish_time_is_converted_to_naive_utc() -> None:
    """页面北京时间必须减 8 小时后再进入统一使用 naive UTC 的索引库。"""
    load_all_sites()
    config = get_site_config("chdbits")
    assert isinstance(config.selectors, NexusPHPSelectors)
    assert config.timezone == "Asia/Shanghai"
    site = NexusPHPSite(
        selectors=config.selectors,
        category_map=config.category_map,
        site_id=config.site_id,
        base_url=config.base_url,
        client=MagicMock(),
        auth_manager=MagicMock(),
        timezone=config.timezone,
    )
    row = Selector(
        text=(
            "<tr><td></td><td></td><td></td>"
            '<td><span title="2026-08-12 00:12:22">4 小时前</span></td></tr>'
        )
    )

    assert site._parse_upload_time(row) == datetime(2026, 8, 11, 16, 12, 22)
