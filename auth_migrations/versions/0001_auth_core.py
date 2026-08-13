"""认证核心表（方案 §5.1）：users / refresh_tokens / identity_mapping_outbox。

- users：身份源头，user_id 为全局唯一内部身份（所有 agent 共用）。
- refresh_tokens：不透明 refresh token 只存 SHA-256 哈希，支持 family 级撤销与轮换。
- identity_mapping_outbox：跨库补偿表，与 users 同事务落库，由 memory-api 进程内
  消费任务幂等写入 memory 库 account_identity_mappings。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001_auth_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_users_status"
        ),
    )
    # 邮箱唯一性按非空值判定（方案 §4.3：部分唯一索引）
    op.create_index(
        "uq_users_email_nonnull",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("token_hash", sa.LargeBinary(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    op.create_table(
        "identity_mapping_outbox",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.user_id"),
            nullable=False,
        ),
        sa.Column("issuer", sa.String(300), nullable=False),
        sa.Column("external_subject", sa.String(300), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'dead')", name="ck_identity_mapping_outbox_status"
        ),
    )
    op.create_index(
        "ix_identity_mapping_outbox_status_next",
        "identity_mapping_outbox",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_table("identity_mapping_outbox")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
