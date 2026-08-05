"""播放/收藏事件 builder 单测：媒体翻译、哨兵不外泄、级联 batch_id。"""

from __future__ import annotations

from movieclaw_db.engine import get_database
from movieclaw_playback import state as playback_state
from movieclaw_playback.events import (
    ClientInfo,
    build_favorite_event,
    build_marked_events,
    build_playback_event,
)


async def test_movie_playback_event_hides_sentinel(seeded_db):
    async with get_database().session() as session:
        unit = (seeded_db["movie"], 0, 0)
        row, newly_played = await playback_state.record_playback_progress(
            session, unit, position_ms=3_000_000, runtime_ms=3_100_000
        )
        await session.commit()
        assert newly_played is True
        event = await build_playback_event(
            session,
            "playback.completed",
            unit,
            row,
            duration_ms=3_100_000,
            client=ClientInfo(name="Infuse", device_name="Apple TV"),
        )
    assert event is not None
    media = event.data["media"]
    assert media["type"] == "movie"
    assert media["tmdb_id"] == 27205
    assert media["season_number"] is None and media["episode_number"] is None
    assert event.data["playback"]["played"] is True
    assert event.data["playback"]["duration_ms"] == 3_100_000
    assert event.data["client"]["name"] == "Infuse"
    assert event.data["client"]["device_id"] is None  # 空串外发为 null


async def test_episode_playback_event_and_no_flip(seeded_db):
    async with get_database().session() as session:
        unit = (seeded_db["show"], 2, 4)
        row, newly_played = await playback_state.record_playback_progress(
            session, unit, position_ms=60_000, runtime_ms=2_800_000
        )
        await session.commit()
        assert newly_played is False  # 2% 进度不足以标记已看
        event = await build_playback_event(session, "playback.stopped", unit, row)
    media = event.data["media"]
    assert media["type"] == "episode"
    assert (media["season_number"], media["episode_number"]) == (2, 4)
    assert event.data["client"] is None


async def test_marked_events_share_batch_id(seeded_db):
    units = [(seeded_db["show"], 1, 1), (seeded_db["show"], 1, 2)]
    async with get_database().session() as session:
        await playback_state.mark_played(session, units, date_played=None)
        await session.commit()
        events = await build_marked_events(session, "playback.marked_played", units)
    assert len(events) == 2
    assert events[0].batch_id is not None
    assert events[0].batch_id == events[1].batch_id
    assert all(e.data["playback"]["played"] for e in events)


async def test_marked_single_unit_no_batch(seeded_db):
    units = [(seeded_db["movie"], 0, 0)]
    async with get_database().session() as session:
        await playback_state.mark_played(session, units, date_played=None)
        await session.commit()
        events = await build_marked_events(session, "playback.marked_played", units)
    assert len(events) == 1
    assert events[0].batch_id is None


async def test_favorite_levels(seeded_db):
    show = seeded_db["show"]
    cases = [
        ((seeded_db["movie"], 0, 0), "movie", None, None),
        ((show, -1, -1), "series", None, None),
        ((show, 2, -1), "season", 2, None),
        ((show, 1, 2), "episode", 1, 2),
    ]
    async with get_database().session() as session:
        for unit, level, season, episode in cases:
            await playback_state.set_favorite(session, unit, favorite=True)
            await session.commit()
            event = await build_favorite_event(session, unit, favorite=True)
            media = event.data["media"]
            assert event.event == "item.favorited"
            assert media["type"] == level, unit
            assert media["season_number"] == season
            assert media["episode_number"] == episode
            # 哨兵数值绝不外泄
            assert media["season_number"] != -1 and media["episode_number"] != -1
            assert event.data["favorite"] is True


async def test_unfavorite_event_name(seeded_db):
    async with get_database().session() as session:
        event = await build_favorite_event(
            session, (seeded_db["movie"], 0, 0), favorite=False
        )
    assert event.event == "item.unfavorited"
    assert event.data["favorite"] is False


async def test_missing_item_returns_none(seeded_db):
    async with get_database().session() as session:
        assert (
            await build_favorite_event(session, (99999, 0, 0), favorite=True)
        ) is None
        assert (
            await build_marked_events(session, "playback.marked_played", [(99999, 0, 0)])
        ) == []
