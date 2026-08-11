"""0003 memory_operations.commit_started_at（§11.6 取消仲裁，裁决 2026-08-11）。

已进入 commit 副作用的 running operation 不允许取消（409 OPERATION_CANCEL_NOT_ALLOWED）；
该列仅作取消仲裁，不进入任何公开 API 响应（§20.1）。进程在 commit 中崩溃时
标记残留，由 Lease 回收后执行层在下次开始执行时清除。
"""

from __future__ import annotations

from alembic import op

revision = "0003_operation_commit_started"
down_revision = "0002_activity_seen_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE memory_operations ADD COLUMN commit_started_at timestamptz NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE memory_operations DROP COLUMN IF EXISTS commit_started_at")
