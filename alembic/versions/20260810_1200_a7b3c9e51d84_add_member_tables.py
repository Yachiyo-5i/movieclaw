"""add member tables: 成员账号与库/站点访问白名单

成员管理（docs/design/member-management.md）P0 的数据层：

- ``member``：成员账号本体（用户名/密码哈希/状态/token_version/能力开关/
  白名单模式开关）。超管不在本表——超管仍是 ``auth.admin`` 配置域的一条
  配置，成员体系在其之外另立，零数据迁移；
- ``member_library_access``：成员 × 库 可见性白名单（``all_libraries=False``
  时生效），外键级联删除；
- ``member_site_access``：成员 × 站点 可用性白名单，``site_id`` 是 YAML
  注册表字符串标识、非外键，悬空行判定时自然失效。

纯新增表，向前兼容。

Revision ID: a7b3c9e51d84
Revises: d1e4b8f30527
Create Date: 2026-08-10 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b3c9e51d84"
down_revision: str | None = "d1e4b8f30527"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "member",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("nickname", sa.String(), nullable=False),
        sa.Column("avatar_path", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("allow_subscribe", sa.Boolean(), nullable=False),
        sa.Column("allow_search", sa.Boolean(), nullable=False),
        sa.Column("allow_direct_download", sa.Boolean(), nullable=False),
        sa.Column("all_libraries", sa.Boolean(), nullable=False),
        sa.Column("all_sites", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_member_username", "member", ["username"], unique=True)
    op.create_index("ix_member_status", "member", ["status"], unique=False)

    op.create_table(
        "member_library_access",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("library_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["library_id"], ["library.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("member_id", "library_id", name="uq_member_library_access"),
    )
    op.create_index(
        "ix_member_library_access_member_id",
        "member_library_access",
        ["member_id"],
        unique=False,
    )
    op.create_index(
        "ix_member_library_access_library_id",
        "member_library_access",
        ["library_id"],
        unique=False,
    )

    op.create_table(
        "member_site_access",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("member_id", "site_id", name="uq_member_site_access"),
    )
    op.create_index(
        "ix_member_site_access_member_id",
        "member_site_access",
        ["member_id"],
        unique=False,
    )
    op.create_index(
        "ix_member_site_access_site_id",
        "member_site_access",
        ["site_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_member_site_access_site_id", table_name="member_site_access")
    op.drop_index("ix_member_site_access_member_id", table_name="member_site_access")
    op.drop_table("member_site_access")
    op.drop_index(
        "ix_member_library_access_library_id", table_name="member_library_access"
    )
    op.drop_index(
        "ix_member_library_access_member_id", table_name="member_library_access"
    )
    op.drop_table("member_library_access")
    op.drop_index("ix_member_status", table_name="member")
    op.drop_index("ix_member_username", table_name="member")
    op.drop_table("member")
