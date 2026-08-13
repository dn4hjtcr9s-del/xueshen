"""0006 全局恢复维护门禁（评审二轮 P0-1 / P1-5）。

门禁放在恢复不会 DROP 的 ops schema 中。恢复入口使用等价的幂等 bootstrap
DDL，因此从旧备份恢复时可以先隔离流量，再执行 Alembic 升级到当前 head。
本迁移还会修复早期 bootstrap 创建的弱约束表，确保 active 状态始终拥有完整
owner、原因和开始时间，避免出现无法审计或无法安全接管的半初始化门禁。
"""

from __future__ import annotations

from alembic import op

revision = "0006_global_maintenance_gate"
down_revision = "0005_lease_fencing_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建并校准维护门禁表；兼容恢复入口已提前创建的旧 bootstrap 表。"""
    op.execute("CREATE SCHEMA IF NOT EXISTS ops")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops.system_maintenance (
            singleton_id boolean PRIMARY KEY DEFAULT true,
            active boolean NOT NULL DEFAULT false,
            owner_token uuid,
            reason text,
            started_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT system_maintenance_singleton CHECK (singleton_id),
            CONSTRAINT system_maintenance_state_consistent CHECK (
                (active
                    AND owner_token IS NOT NULL
                    AND reason IS NOT NULL
                    AND started_at IS NOT NULL)
                OR
                (NOT active
                    AND owner_token IS NULL
                    AND reason IS NULL
                    AND started_at IS NULL)
            )
        )
        """
    )
    # 旧 bootstrap 仅有三个单向 CHECK。补齐列、默认值和非空约束后，统一删除
    # 该受控表上的旧 CHECK，并以完整状态不变量重建，避免 IF NOT EXISTS 掩盖弱 schema。
    op.execute(
        """
        ALTER TABLE ops.system_maintenance
            ADD COLUMN IF NOT EXISTS singleton_id boolean,
            ADD COLUMN IF NOT EXISTS active boolean,
            ADD COLUMN IF NOT EXISTS owner_token uuid,
            ADD COLUMN IF NOT EXISTS reason text,
            ADD COLUMN IF NOT EXISTS started_at timestamptz,
            ADD COLUMN IF NOT EXISTS updated_at timestamptz
        """
    )
    op.execute(
        """
        UPDATE ops.system_maintenance
        SET singleton_id = COALESCE(singleton_id, true),
            active = COALESCE(active, false),
            owner_token = CASE
                WHEN COALESCE(active, false)
                    THEN COALESCE(
                        owner_token,
                        '00000000-0000-0000-0000-000000000000'::uuid
                    )
                ELSE NULL
            END,
            reason = CASE
                WHEN COALESCE(active, false)
                    THEN COALESCE(NULLIF(reason, ''), 'legacy-incomplete-maintenance-state')
                ELSE NULL
            END,
            started_at = CASE
                WHEN COALESCE(active, false) THEN COALESCE(started_at, now())
                ELSE NULL
            END,
            updated_at = COALESCE(updated_at, now())
        """
    )
    op.execute(
        """
        ALTER TABLE ops.system_maintenance
            ALTER COLUMN singleton_id SET DEFAULT true,
            ALTER COLUMN singleton_id SET NOT NULL,
            ALTER COLUMN active SET DEFAULT false,
            ALTER COLUMN active SET NOT NULL,
            ALTER COLUMN updated_at SET DEFAULT now(),
            ALTER COLUMN updated_at SET NOT NULL
        """
    )
    op.execute(
        """
        DO $$
        DECLARE
            constraint_name text;
        BEGIN
            FOR constraint_name IN
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = 'ops.system_maintenance'::regclass
                  AND contype = 'c'
            LOOP
                EXECUTE format(
                    'ALTER TABLE ops.system_maintenance DROP CONSTRAINT %I',
                    constraint_name
                );
            END LOOP;
        END $$
        """
    )
    op.execute(
        """
        ALTER TABLE ops.system_maintenance
            ADD CONSTRAINT system_maintenance_singleton CHECK (singleton_id),
            ADD CONSTRAINT system_maintenance_state_consistent CHECK (
                (active
                    AND owner_token IS NOT NULL
                    AND reason IS NOT NULL
                    AND started_at IS NOT NULL)
                OR
                (NOT active
                    AND owner_token IS NULL
                    AND reason IS NULL
                    AND started_at IS NULL)
            )
        """
    )
    op.execute(
        """
        INSERT INTO ops.system_maintenance (singleton_id, active)
        VALUES (true, false)
        ON CONFLICT (singleton_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ops.system_maintenance")
