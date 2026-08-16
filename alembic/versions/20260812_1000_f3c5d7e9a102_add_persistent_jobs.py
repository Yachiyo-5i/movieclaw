"""add persistent jobs: 统一可恢复后台任务底座

只新增独立表和索引，不修改既有 task_run / scheduled_task / 字幕数据；旧版本
回退时会忽略这些表，符合数据库迁移向前兼容约束。

Revision ID: f3c5d7e9a102
Revises: e2b4c6d8f901
Create Date: 2026-08-12 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3c5d7e9a102"
down_revision: str | None = "e2b4c6d8f901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("definition_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("input_data", sa.JSON(), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("conflict_policy", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=True),
        sa.Column("actor_name", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("parent_job_id", sa.Text(), nullable=True),
        sa.Column("root_job_id", sa.Text(), nullable=True),
        sa.Column("triggered_by_job_id", sa.Text(), nullable=True),
        sa.Column("retry_of_job_id", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
        sa.Column("cancel_requested_by", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("retention_until", sa.DateTime(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["parent_job_id"], ["job.id"]),
        sa.ForeignKeyConstraint(["root_job_id"], ["job.id"]),
        sa.ForeignKeyConstraint(["triggered_by_job_id"], ["job.id"]),
        sa.ForeignKeyConstraint(["retry_of_job_id"], ["job.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for name in (
        "job_type",
        "status",
        "dedupe_key",
        "priority",
        "available_at",
        "origin",
        "correlation_id",
        "parent_job_id",
        "root_job_id",
        "triggered_by_job_id",
        "retry_of_job_id",
        "lease_owner",
        "lease_expires_at",
        "finished_at",
        "retention_until",
    ):
        op.create_index(f"ix_job_{name}", "job", [name])
    # 同一语义任务在活跃状态只能有一条；终态历史仍可无限保留。该部分唯一索引
    # 是并发请求跨进程去重的最后防线，应用层的先查再写只负责友好返回已有任务。
    op.create_index(
        "uq_job_active_dedupe",
        "job",
        ["dedupe_key"],
        unique=True,
        sqlite_where=sa.text(
            "dedupe_key IS NOT NULL AND status IN "
            "('queued','running','retry_wait','cancelling','waiting','blocked')"
        ),
    )

    op.create_table(
        "job_resource",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id", "resource_type", "resource_id", "relation", name="uq_job_resource"
        ),
    )
    op.create_index("ix_job_resource_job_id", "job_resource", ["job_id"])
    op.create_index(
        "ix_job_resource_lookup", "job_resource", ["resource_type", "resource_id", "job_id"]
    )

    op.create_table(
        "job_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "revision", name="uq_job_event_revision"),
    )
    op.create_index("ix_job_event_job_id", "job_event", ["job_id"])
    op.create_index("ix_job_event_revision", "job_event", ["revision"])
    op.create_index("ix_job_event_event_type", "job_event", ["event_type"])
    op.create_index("ix_job_event_created_at", "job_event", ["created_at"])

    op.create_table(
        "job_lock",
        sa.Column("lock_key", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("lock_key"),
    )
    op.create_index("ix_job_lock_job_id", "job_lock", ["job_id"])
    op.create_index("ix_job_lock_lease_expires_at", "job_lock", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_table("job_lock")
    op.drop_table("job_event")
    op.drop_table("job_resource")
    op.drop_table("job")
