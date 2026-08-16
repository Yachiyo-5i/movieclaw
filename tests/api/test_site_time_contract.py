"""站点时间跨持久层、API 与订阅日历的系统契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from movieclaw_api.schemas.search import TorrentHit
from movieclaw_api.services.subscription.matching import publish_calendar_date
from movieclaw_api.services.torrent_sync import _validate_page_times
from movieclaw_db.models.site_torrent import TorrentSource
from movieclaw_db.repositories.torrent_repo import TorrentObservation
from movieclaw_tracker.exceptions import TrackerParseError
from movieclaw_tracker.models import TorrentListItem


def test_observation_converts_aware_values_before_dropping_timezone() -> None:
    observation = TorrentObservation(
        site_id="fixture",
        torrent_id="1",
        title="fixture",
        source=TorrentSource.LIST,
        publish_time=datetime.fromisoformat("2026-08-14T20:00:00+08:00"),
        free_deadline=datetime.fromisoformat("2026-08-14T20:00:00+08:00"),
    )

    assert observation.publish_time == datetime(2026, 8, 14, 12)
    assert observation.free_deadline == datetime(2026, 8, 14, 12)


def test_torrent_hit_serializes_tracker_times_with_utc_offset() -> None:
    hit = TorrentHit(
        site_id="fixture",
        site_name="Fixture",
        torrent_id="1",
        title="fixture",
        upload_time=datetime(2026, 8, 14, 12),
    )

    assert hit.model_dump(mode="json")["upload_time"] == "2026-08-14T12:00:00+00:00"


def test_sync_rejects_page_without_any_publish_time() -> None:
    with pytest.raises(TrackerParseError, match="未解析到任何种子发布时间"):
        _validate_page_times(
            "fixture",
            1,
            [TorrentListItem(torrent_id="1", title="fixture")],
        )


def test_sync_rejects_publish_time_far_in_the_future() -> None:
    future = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=20)

    with pytest.raises(TrackerParseError, match="晚于当前时间超过 15 分钟"):
        _validate_page_times(
            "fixture",
            1,
            [TorrentListItem(torrent_id="1", title="fixture", upload_time=future)],
        )


def test_publish_calendar_date_uses_site_local_day() -> None:
    assert publish_calendar_date(datetime(2026, 8, 13, 16, 30)) == datetime(2026, 8, 14).date()
