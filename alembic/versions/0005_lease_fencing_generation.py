"""0005 lease fencing generation（评审 #7/#8，裁决 2026-08-12）。

为 memory_operations 与 memory_outbox 增加 lease_generation fencing token：
每次 claim（领取/回收后重新领取）递增；heartbeat、complete、reschedule、
delivery 写回等所有 lease 持有者写操作必须按 (id, locked_by, lease_generation,
运行中状态) 做 CAS 并检查 rowcount，旧 lease 持有者的迟到写回一律失败。
"""

from __future__ import annotations

from alembic import op

revision = "0005_lease_fencing_generation"
down_revision = "0004_account_deletion_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE memory_operations "
        "ADD COLUMN lease_generation bigint NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE memory_outbox "
        "ADD COLUMN lease_generation bigint NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE memory_outbox DROP COLUMN IF EXISTS lease_generation")
    op.execute("ALTER TABLE memory_operations DROP COLUMN IF EXISTS lease_generation")
