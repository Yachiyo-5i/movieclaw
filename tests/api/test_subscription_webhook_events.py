"""订阅域 webhook 事件 builder 单测：电影哨兵不外泄、剧集单元列表、目录已登记。"""

from __future__ import annotations

from movieclaw_api.services.subscription.events import (
    build_download_started_event,
    build_fulfilled_event,
)
from movieclaw_api.services.webhook.catalog import is_known_event
from movieclaw_api.services.webhook.jellyfin_formatter import jellyfin_variables
from movieclaw_db.models import MediaItem

MOVIE = MediaItem(
    id=5, kind="movie", tmdb_id=27205, imdb_id="tt1375666",
    title="盗梦空间", original_title="Inception", year=2010, aliases=[],
)
SHOW = MediaItem(
    id=7, kind="tv", tmdb_id=1396, title="绝命毒师",
    original_title="Breaking Bad", year=2008, aliases=[],
)


def test_download_started_series():
    event = build_download_started_event(
        SHOW, [(1, 1), (1, 2)], site_id="hdsky", torrent_title="Breaking.Bad.S01", spec="1080p"
    )
    assert event.event == "subscription.download_started"
    assert is_known_event(event.event)
    assert event.data["media"]["type"] == "series"
    assert event.data["media"]["tmdb_id"] == 1396
    assert event.data["units"] == [[1, 1], [1, 2]]
    assert event.data["torrent"] == {
        "site_id": "hdsky", "title": "Breaking.Bad.S01", "spec": "1080p",
    }


def test_movie_units_hide_sentinel():
    event = build_download_started_event(
        MOVIE, [(0, 0)], site_id="hdsky", torrent_title="Inception.2010"
    )
    assert event.data["media"]["type"] == "movie"
    assert event.data["units"] == []


def test_fulfilled_event():
    event = build_fulfilled_event(SHOW, [(2, 1)])
    assert event.event == "subscription.fulfilled"
    assert is_known_event(event.event)
    assert event.data["units"] == [[2, 1]]


def test_subscription_events_not_jellyfin_mappable():
    """订阅域没有 Jellyfin 对应物：变量字典返回 None（jellyfin endpoint 跳过）。"""
    event = build_fulfilled_event(SHOW, [(2, 1)])
    assert jellyfin_variables(event, {"id": "x"}) is None
