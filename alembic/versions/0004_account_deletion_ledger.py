"""0004 ops.account_deletion_ledger（评审 P0-2，裁决 2026-08-12）。

账号删除 ledger 独立持久层：恢复流程只 DROP public schema，ops schema 存活，
ledger 因此在同环境恢复中保留删除水位；备份 manifest 内嵌 ledger 快照以支撑
全新环境灾难恢复。ledger 只保存 user_hash 与完成证明，不含可还原用户正文。
"""

from __future__ import annotations

from alembic import op

revision = "0004_account_deletion_ledger"
down_revision = "0003_operation_commit_started"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA ops")
    op.execute(
        """
        CREATE TABLE ops.account_deletion_ledger (
            account_deletion_id uuid PRIMARY KEY,
            user_hash char(64) NOT NULL,
            user_hash_key_version varchar(32) NOT NULL,
            status text NOT NULL CHECK (status IN (
                'requested', 'running', 'completed', 'failed'
            )),
            requested_at timestamptz NOT NULL,
            purge_completed_at timestamptz,
            completion_proof_checksum char(64),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ops.account_deletion_ledger")
    op.execute("DROP SCHEMA IF EXISTS ops")
