"""add persisted subscription activity ordering

订阅海报墙此前按创建时间排序；若读取时再根据工单、状态和活动流水动态计算，
列表成本会随历史数据增长。新增 last_activity_at 作为固定排序键，并由 SQLite
触发器在活动插入的同一事务内原子推进，任何写入路径都不会漏维护。

字段保留服务端默认值：应用内回退到旧版本后，旧代码创建订阅时不提供此列也能
继续写入，符合单向迁移的向前兼容约束。

Revision ID: d2f5a8c1e346
Revises: c1e4f7a9b205
Create Date: 2026-08-15 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2f5a8c1e346"
down_revision: str | None = "c1e4f7a9b205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRIGGER_NAME = "trg_subscription_activity_last_activity"
_INDEX_NAME = "ix_subscription_last_activity"


def upgrade() -> None:
    # 先允许 NULL，完成历史回填后再收紧。CURRENT_TIMESTAMP 必须永久保留，
    # 否则用户回退到旧应用后，旧 INSERT 不认识新增列会违反 NOT NULL。
    with op.batch_alter_table("subscription", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "last_activity_at",
                sa.DateTime(),
                nullable=True,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )

    # 历史订阅取自身更新时间与最后一条活动中的较晚者。created_at/updated_at
    # 原本均非空；COALESCE 只处理从未产生过活动的旧订阅。
    op.execute(
        sa.text(
            """
            UPDATE subscription
            SET last_activity_at = MAX(
                created_at,
                updated_at,
                COALESCE(
                    (
                        SELECT MAX(subscription_activity.created_at)
                        FROM subscription_activity
                        WHERE subscription_activity.subscription_id = subscription.id
                    ),
                    created_at
                )
            )
            """
        )
    )

    with op.batch_alter_table("subscription", schema=None) as batch_op:
        batch_op.alter_column(
            "last_activity_at",
            existing_type=sa.DateTime(),
            nullable=False,
            existing_server_default=sa.text("CURRENT_TIMESTAMP"),
        )
        # SQLite 可以反向扫描升序 B-tree 满足两个字段同时 DESC 的查询。
        batch_op.create_index(
            _INDEX_NAME,
            ["last_activity_at", "id"],
            unique=False,
        )

    # MAX 防止并发任务或补写的旧活动让排序时间倒退。触发器与活动 INSERT
    # 属于同一事务：事务回滚时排序键也一起回滚。
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {_TRIGGER_NAME}
            AFTER INSERT ON subscription_activity
            FOR EACH ROW
            BEGIN
                UPDATE subscription
                SET last_activity_at = MAX(last_activity_at, NEW.created_at)
                WHERE id = NEW.subscription_id;
            END
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME}"))
    with op.batch_alter_table("subscription", schema=None) as batch_op:
        batch_op.drop_index(_INDEX_NAME)
        batch_op.drop_column("last_activity_at")
