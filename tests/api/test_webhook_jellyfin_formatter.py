"""Jellyfin 变量字典与映射去重单测：ticks 换算、补零、GUID 同源、completed 吸收 stopped。"""

from __future__ import annotations

from datetime import UTC, datetime

from movieclaw_api.services.webhook.jellyfin_formatter import (
    jellyfin_variables,
    merge_for_jellyfin,
)
from movieclaw_events import OutboundEvent
from movieclaw_jellyfin.ids import episode_guid, item_guid

SERVER = {
    "id": "srv01",
    "name": "MovieClaw",
    "version": "0.5.0",
    "url": "http://mc.lan:3000",
    "username": "admin",
}


def _episode_event(event: str = "playback.completed") -> OutboundEvent:
    return OutboundEvent(
        event=event,
        occurred_at=datetime(2026, 8, 4, 12, 30, tzinfo=UTC),
        data={
            "media": {
                "type": "episode",
                "item_id": 7,
                "tmdb_id": 1396,
                "imdb_id": "tt0903747",
                "title": "绝命毒师",
                "year": 2008,
                "season_number": 2,
                "episode_number": 4,
                "episode_title": "Down",
            },
            "playback": {
                "position_ms": 3_120_000,
                "duration_ms": 3_300_000,
                "played": True,
                "play_count": 1,
            },
            "client": {"name": "Infuse", "device_name": "Apple TV", "device_id": "d1"},
        },
    )


def test_episode_variables():
    values = jellyfin_variables(_episode_event(), SERVER)
    assert values["NotificationType"] == "PlaybackStop"
    assert values["PlayedToCompletion"] is True
    assert values["ServerUrl"] == "http://mc.lan:3000"
    assert values["NotificationUsername"] == "admin"
    # GUID 与 Jellyfin 兼容 API 同源
    assert values["ItemId"] == episode_guid(7, 2, 4)
    assert values["SeriesId"] == item_guid(7)
    assert values["ItemType"] == "Episode"
    assert values["Name"] == "Down"  # 单集标题优先
    assert values["SeriesName"] == "绝命毒师"
    assert values["SeasonNumber00"] == "02"
    assert values["EpisodeNumber000"] == "004"
    # ticks 换算只在此边界发生
    assert values["PlaybackPositionTicks"] == 3_120_000 * 10_000
    assert values["RunTime"] == "00:55:00"
    assert values["PlaybackPosition"] == "00:52:00"
    assert values["DeviceName"] == "Apple TV"
    assert values["Provider_tmdb"] == 1396


def test_stopped_not_completed():
    values = jellyfin_variables(_episode_event("playback.stopped"), SERVER)
    assert values["NotificationType"] == "PlaybackStop"
    assert values["PlayedToCompletion"] is False


def test_favorite_maps_to_userdata_saved():
    event = OutboundEvent(
        event="item.favorited",
        data={
            "media": {"type": "season", "item_id": 7, "title": "绝命毒师", "season_number": 2},
            "favorite": True,
            "client": None,
        },
    )
    values = jellyfin_variables(event, SERVER)
    assert values["NotificationType"] == "UserDataSaved"
    assert values["Favorite"] is True
    assert values["ItemType"] == "Season"
    assert "SeriesName" not in values  # 非 episode 不给剧集变量


def test_unmapped_event_returns_none():
    event = OutboundEvent(event="subscription.download_started", data={})
    assert jellyfin_variables(event, SERVER) is None


def test_merge_completed_absorbs_stopped():
    stopped = OutboundEvent(event="playback.stopped", data={})
    completed = OutboundEvent(event="playback.completed", data={})
    other = OutboundEvent(event="playback.started", data={})
    merged = merge_for_jellyfin([other, stopped, completed])
    assert [e.event for e in merged] == ["playback.started", "playback.completed"]
    # 没有 completed 时 stopped 原样保留
    assert merge_for_jellyfin([stopped]) == [stopped]


def test_merge_only_absorbs_same_unit():
    """去重按单元比对：其他条目/其他集的 stopped 不受 completed 牵连。"""

    def _ev(event: str, item_id: int, episode: int) -> OutboundEvent:
        return OutboundEvent(
            event=event,
            data={"media": {"item_id": item_id, "season_number": 1, "episode_number": episode}},
        )

    completed_a = _ev("playback.completed", 1, 1)
    stopped_a = _ev("playback.stopped", 1, 1)
    stopped_b = _ev("playback.stopped", 2, 5)
    merged = merge_for_jellyfin([completed_a, stopped_a, stopped_b])
    assert merged == [completed_a, stopped_b]
