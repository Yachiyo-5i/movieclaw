"""取消季范围迁移的存量数据回归测试。"""

from __future__ import annotations

import sqlite3

from alembic import command

from movieclaw_api.core.config import get_settings
from movieclaw_db.migrations import _build_config


def test_upgrade_backfills_scope_and_cancels_orphan_attempt(tmp_path, monkeypatch) -> None:
    database = tmp_path / "legacy-subscription.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()
    config = _build_config()
    command.upgrade(config, "f9c1e4a6b823")

    now = "2026-08-01 00:00:00"
    info_hash = "a" * 40
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO subscription
               (id, created_at, updated_at, media_item_id, kind, selected_seasons,
                follow_future, rule_set_id, status)
               VALUES (1, ?, ?, 1, 'tv', '[2]', 0, 1, 'active')""",
            (now, now),
        )
        connection.executemany(
            """INSERT INTO wanted_item
               (id, created_at, updated_at, subscription_id, media_item_id,
                season_number, episode_number, status, air_date, priority,
                search_attempts, info_hash)
               VALUES (?, ?, ?, 1, 1, ?, 1, ?, '2026-01-01', 0, 0, ?)""",
            [
                (1, now, now, 1, "grabbed", info_hash),
                (2, now, now, 2, "grabbed", "b" * 40),
            ],
        )
        connection.execute(
            """INSERT INTO subscription_download_attempt
               (id, created_at, updated_at, subscription_id, info_hash,
                torrent_title, units, quality, owned_by_movieclaw, status,
                last_progress_at, missing_observations, search_attempts)
               VALUES (1, ?, ?, 1, ?, 'Legacy.S01', '[[1,1]]', '{}', 1,
                       'replacement_pending', ?, 0, 0)""",
            (now, now, info_hash, now),
        )
        connection.execute(
            """INSERT INTO subscription_download_attempt
               (id, created_at, updated_at, subscription_id, info_hash,
                torrent_title, units, quality, owned_by_movieclaw, status,
                last_progress_at, missing_observations, search_attempts)
               VALUES (2, ?, ?, 1, ?, 'Old.S02', '[[2,1]]', '{}', 1,
                       'cleanup_pending', ?, 0, 0)""",
            (now, now, "c" * 40, now),
        )
        connection.execute(
            """INSERT INTO subscription_download_attempt
               (id, created_at, updated_at, subscription_id, replaces_attempt_id,
                info_hash, torrent_title, units, quality, owned_by_movieclaw,
                status, last_progress_at, missing_observations, search_attempts)
               VALUES (3, ?, ?, 1, 2, ?, 'Replacement.S02', '[[2,1]]', '{}', 1,
                       'active', ?, 0, 0)""",
            (now, now, "b" * 40, now),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database) as connection:
        scopes = connection.execute(
            "SELECT season_number, in_scope FROM wanted_item ORDER BY season_number"
        ).fetchall()
        attempts = connection.execute(
            "SELECT id, status, next_search_at, cleanup_note "
            "FROM subscription_download_attempt ORDER BY id"
        ).fetchall()

    assert scopes == [(1, 0), (2, 1)]
    assert attempts[0][1] == "cancelled"
    assert attempts[0][2] is None
    assert "退出当前订阅范围" in attempts[0][3]
    assert [row[1] for row in attempts[1:]] == ["cleanup_pending", "active"]

    command.downgrade(config, "f9c1e4a6b823")
    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(wanted_item)").fetchall()
        }
        restored_status = connection.execute(
            "SELECT status FROM subscription_download_attempt WHERE id = 1"
        ).fetchone()[0]
    assert "in_scope" not in columns
    assert restored_status == "active"
    get_settings.cache_clear()
