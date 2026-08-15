"""媒体库统计列部分迁移的恢复回归测试。"""

from __future__ import annotations

import sqlite3

from alembic import command

from movieclaw_api.core.config import get_settings
from movieclaw_db.migrations import _build_config


def test_repair_migration_restores_missing_episode_count(tmp_path, monkeypatch) -> None:
    """版本已前进但列缺失时，后续迁移仍可补齐数据库契约。"""
    database = tmp_path / "partial-library-stats.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()
    config = _build_config()
    command.upgrade(config, "d2f5a8c1e346")

    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE library DROP COLUMN stats_episode_count")
        connection.commit()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(library)")}
        assert "stats_episode_count" not in columns

    command.upgrade(config, "head")

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(library)")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert "stats_episode_count" in columns
    assert revision == "a5c8d1e3f679"
    get_settings.cache_clear()
