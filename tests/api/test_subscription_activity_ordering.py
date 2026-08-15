"""订阅海报墙最近活动排序的持久化与迁移回归测试。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from alembic import command

from movieclaw_api.core.config import get_settings
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import _build_config, run_migrations
from movieclaw_db.models import (
    ActivityType,
    MediaItem,
    RuleSet,
    Subscription,
    SubscriptionActivity,
)
from movieclaw_db.repositories.subscription_repo import SubscriptionRepository


def _insert_legacy_subscription(
    connection: sqlite3.Connection,
    *,
    subscription_id: int,
    media_item_id: int,
    created_at: str,
    updated_at: str,
) -> None:
    """在新增字段前的表结构中插入一条最小订阅。"""
    connection.execute(
        """
        INSERT INTO subscription
            (id, created_at, updated_at, media_item_id, kind, selected_seasons,
             follow_future, rule_set_id, status)
        VALUES (?, ?, ?, ?, 'movie', '[]', 0, 1, 'active')
        """,
        (subscription_id, created_at, updated_at, media_item_id),
    )


def _activity_time(connection: sqlite3.Connection, subscription_id: int) -> datetime:
    value = connection.execute(
        "SELECT last_activity_at FROM subscription WHERE id = ?",
        (subscription_id,),
    ).fetchone()[0]
    return datetime.fromisoformat(value)


def test_migration_backfills_and_trigger_keeps_last_activity_valid(tmp_path, monkeypatch) -> None:
    """回填取历史最大值；触发器单调推进并与活动事务共同回滚。"""
    database = tmp_path / "subscription-activity-ordering.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()
    config = _build_config()
    command.upgrade(config, "c1e4f7a9b205")

    created = "2026-08-15 08:00:00.000000"
    updated = "2026-08-15 09:00:00.000000"
    historical_activity = "2026-08-15 10:00:00.000000"
    later_updated = "2026-08-15 12:00:00.000000"
    with sqlite3.connect(database) as connection:
        _insert_legacy_subscription(
            connection,
            subscription_id=1,
            media_item_id=1,
            created_at=created,
            updated_at=updated,
        )
        _insert_legacy_subscription(
            connection,
            subscription_id=2,
            media_item_id=2,
            created_at=created,
            updated_at=later_updated,
        )
        connection.execute(
            """
            INSERT INTO subscription_activity
                (created_at, updated_at, subscription_id, type, message, payload)
            VALUES (?, ?, 1, 'created', '历史活动', '{}')
            """,
            (historical_activity, historical_activity),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database) as connection:
        assert _activity_time(connection, 1) == datetime.fromisoformat(historical_activity)
        assert _activity_time(connection, 2) == datetime.fromisoformat(later_updated)

        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM subscription ORDER BY last_activity_at DESC, id DESC"
        ).fetchall()
        assert any("ix_subscription_last_activity" in str(row) for row in plan)

        newest = "2026-08-15 13:00:00.000000"
        connection.execute(
            """
            INSERT INTO subscription_activity
                (created_at, updated_at, subscription_id, type, message, payload)
            VALUES (?, ?, 1, 'grabbed', '已投递', '{}')
            """,
            (newest, newest),
        )
        connection.commit()
        assert _activity_time(connection, 1) == datetime.fromisoformat(newest)

        # 迟到或补写的旧活动只能补时间线，不能让海报墙排序时间倒退。
        connection.execute(
            """
            INSERT INTO subscription_activity
                (created_at, updated_at, subscription_id, type, message, payload)
            VALUES (?, ?, 1, 'searched', '补写旧搜索', '{}')
            """,
            (created, created),
        )
        connection.commit()
        assert _activity_time(connection, 1) == datetime.fromisoformat(newest)

        rolled_back = "2026-08-15 14:00:00.000000"
        connection.execute(
            """
            INSERT INTO subscription_activity
                (created_at, updated_at, subscription_id, type, message, payload)
            VALUES (?, ?, 1, 'imported', '待回滚活动', '{}')
            """,
            (rolled_back, rolled_back),
        )
        assert _activity_time(connection, 1) == datetime.fromisoformat(rolled_back)
        connection.rollback()
        assert _activity_time(connection, 1) == datetime.fromisoformat(newest)

        # 模拟应用回退后的旧 INSERT：不认识新列仍由服务端默认值安全补齐。
        _insert_legacy_subscription(
            connection,
            subscription_id=3,
            media_item_id=3,
            created_at=created,
            updated_at=updated,
        )
        connection.commit()
        assert _activity_time(connection, 3) is not None

    command.downgrade(config, "c1e4f7a9b205")
    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(subscription)").fetchall()
        }
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    assert "last_activity_at" not in columns
    assert "trg_subscription_activity_last_activity" not in {row[0] for row in triggers}
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'ordering.db'}")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_repository_orders_by_persisted_activity_then_id(db) -> None:
    """查询只读取订阅固定字段；相同活动时间由 id 倒序稳定兜底。"""
    async with db.session() as session:
        rule = RuleSet(name="排序测试", is_default=True)
        media = [
            MediaItem(kind="movie", tmdb_id=number, title=str(number), original_title=str(number))
            for number in (101, 102, 103)
        ]
        session.add(rule)
        session.add_all(media)
        await session.commit()
        await session.refresh(rule)
        for item in media:
            await session.refresh(item)

        base = datetime(2026, 8, 15, 8, 0, 0)
        subscriptions = [
            Subscription(
                media_item_id=item.id,
                kind="movie",
                rule_set_id=rule.id,
                last_activity_at=activity_at,
            )
            for item, activity_at in zip(
                media,
                (base, base + timedelta(hours=1), base + timedelta(hours=1)),
                strict=True,
            )
        ]
        session.add_all(subscriptions)
        await session.commit()

        repo = SubscriptionRepository(session)
        rows = await repo.list_all(kind="movie")
        assert [row.media_item_id for row in rows] == [
            media[2].id,
            media[1].id,
            media[0].id,
        ]

        await repo.add_activity(
            SubscriptionActivity(
                subscription_id=subscriptions[0].id,
                type=ActivityType.ADJUSTED,
                message="排序测试活动",
                created_at=base + timedelta(hours=2),
                updated_at=base + timedelta(hours=2),
            )
        )
        reordered = await repo.list_all(kind="movie")

    assert [row.media_item_id for row in reordered] == [
        media[0].id,
        media[2].id,
        media[1].id,
    ]
