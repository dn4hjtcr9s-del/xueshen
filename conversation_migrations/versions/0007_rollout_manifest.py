"""Conversation Rollout manifest 与消息指针（memory-rebuild §1.7 / §5.2 Phase 0-C）。

短期记忆改为 JSONL 事实源后，PG 退化为"索引 + 协调层"：本迁移新增
``conversation_rollout_segments``（段清单 / manifest）并在 ``conversation_messages``
上增加可空指针列。

段状态机为三段式（Phase 0 决策）：
``open``（本地热段，尚未上传，object_key/etag/sha256/byte_size/ordinal_end 均可空）
→ ``sealed``（对象已上传且 manifest 已登记，上述字段全部必填且此后不可变）
→ ``deleted``（逻辑删除 tombstone，deleted_at 必填）。

全部新列可空或带安全默认值，因此旧代码在新库上仍可读写；``conversation_messages.
content`` 保留作为过渡回退，在本轮不收紧、不清空（§5.12：完成全量 pointer 回填、
read-repair 观察与删除演练前不得设为不可空）。

命名说明：revision id 受 ``alembic_version.version_num varchar(32)`` 限制，
因此沿用本链的缩写惯例（参见 ``0004_ks_alias_group_unique``）。
"""

from __future__ import annotations

from alembic import op

revision = "0007_rollout_manifest"
down_revision = "0006_turn_progress_event"
branch_labels = None
depends_on = None

_SEGMENT_STATUSES = ("open", "sealed", "deleted")


def _add_constraint_if_absent(table: str, name: str, definition: str) -> None:
    """幂等加约束（PostgreSQL 不支持 ADD CONSTRAINT IF NOT EXISTS）。"""
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{name}'
                  AND conrelid = '{table}'::regclass
            ) THEN
                ALTER TABLE {table} ADD CONSTRAINT {name} {definition};
            END IF;
        END $$
        """
    )


def upgrade() -> None:
    """新增 rollout 段清单表与消息侧指针列。"""
    allowed_statuses = ", ".join(f"'{status}'" for status in _SEGMENT_STATUSES)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS conversation.conversation_rollout_segments (
            segment_id uuid PRIMARY KEY,
            thread_id uuid NOT NULL,
            turn_id uuid NOT NULL,
            ordinal_start integer NOT NULL CHECK (ordinal_start >= 0),
            ordinal_end integer CHECK (ordinal_end IS NULL OR ordinal_end >= ordinal_start),
            object_key varchar(1024),
            object_etag varchar(256),
            sha256 char(64),
            byte_size bigint CHECK (byte_size IS NULL OR byte_size >= 0),
            status text NOT NULL DEFAULT 'open' CHECK (status IN ({allowed_statuses})),
            created_at timestamptz NOT NULL DEFAULT now(),
            sealed_at timestamptz,
            deleted_at timestamptz,
            -- 一个 thread 内 ordinal_start 唯一：段边界不重叠，重复封存被数据库拒绝
            CONSTRAINT uq_rollout_segment_thread_ordinal UNIQUE (thread_id, ordinal_start),
            -- 每 turn 一段：一个 turn 最多一个段，重复封存天然幂等（§5.4）
            CONSTRAINT uq_rollout_segment_turn UNIQUE (turn_id),
            -- 封存态必须字段完整：杜绝"sealed 但没有对象"的悬空 manifest
            CONSTRAINT ck_rollout_segment_sealed_fields CHECK (
                status <> 'sealed' OR (
                    object_key IS NOT NULL
                    AND object_etag IS NOT NULL
                    AND sha256 IS NOT NULL
                    AND byte_size IS NOT NULL
                    AND ordinal_end IS NOT NULL
                    AND sealed_at IS NOT NULL
                )
            ),
            -- 删除态与 deleted_at 严格同真同假
            CONSTRAINT ck_rollout_segment_deleted_state CHECK (
                (status = 'deleted') = (deleted_at IS NOT NULL)
            )
        )
        """
    )
    # (thread_id, ordinal_start) 与 turn_id 的唯一约束已各自建索引；此处补 object_key。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conv_rollout_segments_object_key "
        "ON conversation.conversation_rollout_segments (object_key)"
    )
    # reconcile 需要按状态扫描未封存 / 待清理段。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conv_rollout_segments_status "
        "ON conversation.conversation_rollout_segments (status, created_at)"
    )

    # ------------------------------------------------------------------
    # conversation_messages 指针列（§1.4：正文移出，只留指针）
    # ------------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE conversation.conversation_messages
            ADD COLUMN IF NOT EXISTS segment_id uuid,
            ADD COLUMN IF NOT EXISTS rollout_ordinal integer,
            ADD COLUMN IF NOT EXISTS rollout_byte_offset_start bigint,
            ADD COLUMN IF NOT EXISTS rollout_byte_offset_end bigint
        """
    )
    _add_constraint_if_absent(
        "conversation.conversation_messages",
        "fk_conv_messages_rollout_segment",
        "FOREIGN KEY (segment_id) "
        "REFERENCES conversation.conversation_rollout_segments(segment_id) "
        "ON DELETE SET NULL",
    )
    # 指针要么整体存在要么整体缺失：防止半套指针把读路径引到错误字节范围。
    _add_constraint_if_absent(
        "conversation.conversation_messages",
        "ck_conv_messages_rollout_pointer",
        """CHECK (
            (
                segment_id IS NULL
                AND rollout_ordinal IS NULL
                AND rollout_byte_offset_start IS NULL
                AND rollout_byte_offset_end IS NULL
            )
            OR (
                segment_id IS NOT NULL
                AND rollout_ordinal IS NOT NULL
                AND rollout_ordinal >= 0
                AND rollout_byte_offset_start IS NOT NULL
                AND rollout_byte_offset_end IS NOT NULL
                AND rollout_byte_offset_end > rollout_byte_offset_start
            )
        )""",
    )

    # ------------------------------------------------------------------
    # 段删除时一并清空指针（Phase 0 决策：BEFORE DELETE 触发器兜底）
    # ------------------------------------------------------------------
    # FK 的 ON DELETE SET NULL 只能置空外键列 segment_id 本身——PostgreSQL 17 的
    # "SET NULL (col, ...)" 列清单必须是外键引用列的子集，无法连带清空三个偏移列。
    # 而 ck_conv_messages_rollout_pointer 要求四个指针列同真同假，两者直接冲突会
    # 让"删除段"整体失败（已在 conversation_test 上实测复现）。因此用 BEFORE DELETE
    # 触发器先把四列一起置空，FK 动作随后匹配不到行，退化为空操作。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION
            conversation.clear_rollout_pointer_on_segment_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            UPDATE conversation.conversation_messages
            SET segment_id = NULL,
                rollout_ordinal = NULL,
                rollout_byte_offset_start = NULL,
                rollout_byte_offset_end = NULL
            WHERE segment_id = OLD.segment_id;
            RETURN OLD;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_rollout_segment_clear_pointer "
        "ON conversation.conversation_rollout_segments"
    )
    op.execute(
        "CREATE TRIGGER trg_rollout_segment_clear_pointer "
        "BEFORE DELETE ON conversation.conversation_rollout_segments "
        "FOR EACH ROW "
        "EXECUTE FUNCTION conversation.clear_rollout_pointer_on_segment_delete()"
    )


def downgrade() -> None:
    """移除指针列与段清单表；已有 rollout 数据时拒绝回滚，避免静默丢索引。"""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM conversation.conversation_messages
                WHERE segment_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    '0007_rollout_manifest 无法回滚：'
                    'conversation_messages 已存在 rollout 指针';
            END IF;
        END $$
        """
    )
    op.execute(
        "ALTER TABLE conversation.conversation_messages "
        "DROP CONSTRAINT IF EXISTS ck_conv_messages_rollout_pointer"
    )
    op.execute(
        "ALTER TABLE conversation.conversation_messages "
        "DROP CONSTRAINT IF EXISTS fk_conv_messages_rollout_segment"
    )
    op.execute(
        """
        ALTER TABLE conversation.conversation_messages
            DROP COLUMN IF EXISTS rollout_byte_offset_end,
            DROP COLUMN IF EXISTS rollout_byte_offset_start,
            DROP COLUMN IF EXISTS rollout_ordinal,
            DROP COLUMN IF EXISTS segment_id
        """
    )
    op.execute("DROP INDEX IF EXISTS conversation.ix_conv_rollout_segments_status")
    op.execute("DROP INDEX IF EXISTS conversation.ix_conv_rollout_segments_object_key")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_rollout_segment_clear_pointer "
        "ON conversation.conversation_rollout_segments"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS conversation.clear_rollout_pointer_on_segment_delete()"
    )
    op.execute("DROP TABLE IF EXISTS conversation.conversation_rollout_segments")
