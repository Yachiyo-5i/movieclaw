"""add wanted scope and stop orphan subscription attempts

工单状态继续表达不可逆的现实进度；新增 in_scope 独立表达单元是否仍属于
当前订阅期望集合。升级时按当前订阅参数回填，并把已经没有入域目标的下载尝试
置为 cancelled。字段保留服务端默认值，旧版本回退后仍可继续插入工单。

Revision ID: a0d2f5b7c934
Revises: f9c1e4a6b823
Create Date: 2026-08-15 01:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "a0d2f5b7c934"
down_revision: str | None = "f9c1e4a6b823"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _int_set(values: list[object]) -> set[int]:
    """迁移必须容忍历史 JSON 被手工改坏，跳过无法解释的值。"""
    result: set[int] = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _unit_set(values: list[object]) -> set[tuple[int, int]]:
    result: set[tuple[int, int]] = set()
    for unit in values:
        if not isinstance(unit, (list, tuple)) or len(unit) != 2:
            continue
        try:
            result.add((int(unit[0]), int(unit[1])))
        except (TypeError, ValueError):
            continue
    return result


def upgrade() -> None:
    with op.batch_alter_table("wanted_item", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("in_scope", sa.Boolean(), nullable=False, server_default=sa.true())
        )

    connection = op.get_bind()
    subscriptions = sa.table(
        "subscription",
        sa.column("id", sa.Integer()),
        sa.column("kind", sa.String()),
        sa.column("selected_seasons", sa.JSON()),
        sa.column("follow_future", sa.Boolean()),
        sa.column("created_at", sa.DateTime()),
    )
    wanted = sa.table(
        "wanted_item",
        sa.column("id", sa.Integer()),
        sa.column("subscription_id", sa.Integer()),
        sa.column("season_number", sa.Integer()),
        sa.column("episode_number", sa.Integer()),
        sa.column("air_date", sa.Date()),
        sa.column("status", sa.String()),
        sa.column("info_hash", sa.String()),
        sa.column("in_scope", sa.Boolean()),
    )
    attempts = sa.table(
        "subscription_download_attempt",
        sa.column("id", sa.Integer()),
        sa.column("subscription_id", sa.Integer()),
        sa.column("replaces_attempt_id", sa.Integer()),
        sa.column("info_hash", sa.String()),
        sa.column("status", sa.String()),
        sa.column("units", sa.JSON()),
        sa.column("next_search_at", sa.DateTime()),
        sa.column("cleanup_note", sa.String()),
    )

    subscriptions_by_id = {
        row.id: row for row in connection.execute(sa.select(subscriptions)).mappings().all()
    }
    for row in connection.execute(sa.select(wanted)).mappings().all():
        subscription = subscriptions_by_id.get(row.subscription_id)
        if subscription is None:
            continue
        selected = _int_set(_json_list(subscription.selected_seasons))
        created_date = _date(subscription.created_at)
        air_date = _date(row.air_date)
        follows_this_unit = (
            bool(subscription.follow_future)
            and row.season_number != 0
            and (air_date is None or created_date is None or air_date > created_date)
        )
        in_scope = (
            subscription.kind == "movie" or row.season_number in selected or follows_this_unit
        )
        if not in_scope:
            connection.execute(
                sa.update(wanted).where(wanted.c.id == row.id).values(in_scope=False)
            )

    active_wanted: dict[tuple[int, str], set[tuple[int, int]]] = {}
    for row in connection.execute(
        sa.select(wanted).where(
            wanted.c.in_scope.is_(True),
            wanted.c.status.in_(("grabbed", "downloaded")),
            wanted.c.info_hash.is_not(None),
        )
    ).mappings():
        active_wanted.setdefault((row.subscription_id, (row.info_hash or "").lower()), set()).add(
            (row.season_number, row.episode_number)
        )
    attempt_rows = list(connection.execute(sa.select(attempts)).mappings())
    by_id = {row.id: row for row in attempt_rows}
    children_by_parent: dict[int, list[sa.RowMapping]] = {}
    for row in attempt_rows:
        if row.replaces_attempt_id is not None:
            children_by_parent.setdefault(row.replaces_attempt_id, []).append(row)
    cancellable = {
        "active",
        "replacement_pending",
        "trial",
        "cleanup_pending",
        "completed",
    }
    for row in attempt_rows:
        if row.status not in cancellable:
            continue
        source_hashes = {(row.info_hash or "").lower()}
        if row.status == "trial" and row.replaces_attempt_id is not None:
            parent = by_id.get(row.replaces_attempt_id)
            source_hashes = {(parent.info_hash or "").lower()} if parent is not None else set()
        elif row.status == "cleanup_pending":
            source_hashes.update(
                (child.info_hash or "").lower()
                for child in children_by_parent.get(row.id, [])
                if child.status in {"active", "completed"}
            )
        allowed = _unit_set(_json_list(row.units))
        active_units: set[tuple[int, int]] = set()
        for info_hash in source_hashes:
            active_units.update(active_wanted.get((row.subscription_id, info_hash), set()))
        has_target = bool(active_units & allowed) if allowed else bool(active_units)
        if not has_target:
            connection.execute(
                sa.update(attempts)
                .where(attempts.c.id == row.id)
                .values(
                    status="cancelled",
                    next_search_at=None,
                    cleanup_note="关联单元已退出当前订阅范围；保留下载器任务，不再观察或换源",
                )
            )


def downgrade() -> None:
    # 旧代码不认识 cancelled；回退时恢复观察，让旧版本保持原有行为。
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE subscription_download_attempt "
            "SET status = 'active', cleanup_note = NULL WHERE status = 'cancelled'"
        )
    )
    with op.batch_alter_table("wanted_item", schema=None) as batch_op:
        batch_op.drop_column("in_scope")
