"""Community V2 重建迁移（community-rebuild-plan.md v3.9 §7.2）。

破坏性行为：清空旧业务数据（含 boards），仅保留 4 个 seed 板块。
只能在无生产数据的库执行；含任意非 seed 业务行 → RuntimeError。
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0002_community_v2"
down_revision = "0001_community_core"
branch_labels = None
depends_on = None

# §7.2 冻结 seed（与 0001 逐字一致）
BOARDS_SEED = (
    ("da38ecb6-6f37-5724-be95-10e496b5f3dd", "linear-algebra",
     "线性代数", "矩阵、向量空间、特征值与线性变换", 10),
    ("dcd2a3a5-7e06-5b7e-891f-e065765dcde0", "calculus",
     "微积分", "极限、导数、积分与级数", 20),
    ("d6559df9-da74-51ca-9526-a77229c19237", "probability",
     "概率论", "概率模型、随机变量与统计推断", 30),
    ("768737cb-a6a8-527d-a7f1-153bb8841872", "study-methods",
     "学习方法", "学习方法、复习策略与学习习惯交流", 40),
)

SEED_BOARD_IDS = {row[0] for row in BOARDS_SEED}


def _get_check_constraint_name(table_name: str, column_name: str) -> str:
    """按列名查询 0001 自动命名的 CHECK 约束；必须恰好 1 条。"""
    rows = op.get_bind().execute(
        text(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = CAST(:table_name AS regclass)
              AND contype = 'c'
              AND pg_get_constraintdef(oid) ILIKE '%%' || :column_name || '%%'
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"{table_name}.{column_name} CHECK 约束解析失败: 找到 {len(rows)} 条"
        )
    return rows[0][0]


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1) 破坏性门禁：含非 seed 业务数据则拒绝执行
    # ------------------------------------------------------------------
    conn = op.get_bind()

    board_extra = conn.execute(
        text(
            """
            SELECT count(*) FROM community_boards
            WHERE board_id != ALL(:seed_ids)
            """
        ),
        {"seed_ids": list(SEED_BOARD_IDS)},
    ).scalar()

    other_total = conn.execute(
        text(
            """
            SELECT
                (SELECT count(*) FROM community_notifications)
              + (SELECT count(*) FROM community_idempotency_requests)
              + (SELECT count(*) FROM community_outbox)
              + (SELECT count(*) FROM community_post_likes)
              + (SELECT count(*) FROM community_replies)
              + (SELECT count(*) FROM community_posts)
            AS total
            """
        )
    ).scalar()

    if (board_extra or 0) > 0 or (other_total or 0) > 0:
        raise RuntimeError(
            "0002 拒绝在含业务数据的库执行（community-rebuild-plan.md §7.2 门禁）"
        )

    # ------------------------------------------------------------------
    # 2) 清空旧业务数据（含 boards）
    # ------------------------------------------------------------------
    op.execute(
        "TRUNCATE community_notifications, community_idempotency_requests, "
        "community_outbox, community_post_likes, community_replies, "
        "community_posts, community_boards"
    )

    # ------------------------------------------------------------------
    # 3) 重插 seed
    # ------------------------------------------------------------------
    for board_id, slug, name, description, sort_order in BOARDS_SEED:
        op.execute(
            "INSERT INTO community_boards (board_id, slug, name, description, sort_order) "
            f"VALUES ('{board_id}', '{slug}', '{name}', '{description}', {sort_order})"
        )

    # ------------------------------------------------------------------
    # 4) boards 扩展
    # ------------------------------------------------------------------
    op.execute(
        "ALTER TABLE community_boards ADD COLUMN created_by uuid NULL"
    )
    op.execute(
        "CREATE INDEX ix_community_boards_created_by ON community_boards (created_by)"
    )
    op.execute(
        "ALTER TABLE community_boards ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()"
    )
    op.execute(
        "ALTER TABLE community_boards ADD COLUMN post_count integer NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE community_boards ADD CONSTRAINT ck_community_boards_post_count "
        "CHECK (post_count >= 0)"
    )
    op.execute(
        "ALTER TABLE community_boards ALTER COLUMN sort_order SET DEFAULT 100"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_community_boards_lower_name "
        "ON community_boards (lower(name))"
    )

    # ------------------------------------------------------------------
    # 5) notifications 扩展
    # ------------------------------------------------------------------
    notif_event_check = _get_check_constraint_name(
        "community_notifications", "event_type"
    )
    op.execute(f"ALTER TABLE community_notifications DROP CONSTRAINT {notif_event_check}")
    op.execute(
        "ALTER TABLE community_notifications ADD CONSTRAINT ck_community_notifications_event_type "
        "CHECK (event_type IN ('post_replied','reply_marked_solved',"
        "'application_approved','application_rejected'))"
    )
    op.execute(
        "ALTER TABLE community_notifications ALTER COLUMN post_id DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE community_notifications ALTER COLUMN reply_id DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE community_notifications ADD COLUMN board_slug varchar(64) NULL"
    )

    # ------------------------------------------------------------------
    # 6) idempotency_requests 扩展
    # ------------------------------------------------------------------
    idem_op_check = _get_check_constraint_name(
        "community_idempotency_requests", "operation"
    )
    idem_res_check = _get_check_constraint_name(
        "community_idempotency_requests", "resource_type"
    )
    op.execute(f"ALTER TABLE community_idempotency_requests DROP CONSTRAINT {idem_op_check}")
    op.execute(f"ALTER TABLE community_idempotency_requests DROP CONSTRAINT {idem_res_check}")
    op.execute(
        "ALTER TABLE community_idempotency_requests ADD CONSTRAINT ck_community_idempotency_operation "
        "CHECK (operation IN ('create_post','create_reply','upload_attachment','create_application'))"
    )
    op.execute(
        "ALTER TABLE community_idempotency_requests ADD CONSTRAINT ck_community_idempotency_resource_type "
        "CHECK (resource_type IN ('post','reply','attachment','application'))"
    )

    # ------------------------------------------------------------------
    # 7) 新建 attachments
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE community_attachments (
            attachment_id uuid PRIMARY KEY,
            uploader_id uuid NOT NULL,
            post_id uuid REFERENCES community_posts(post_id) ON DELETE RESTRICT,
            position smallint,
            storage_key varchar(128) NOT NULL,
            original_filename varchar(100) NOT NULL DEFAULT '',
            mime varchar(32) NOT NULL,
            size_bytes integer NOT NULL,
            width integer NOT NULL,
            height integer NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'uploaded',
            delete_attempts integer NOT NULL DEFAULT 0,
            last_delete_error text,
            next_delete_attempt_at timestamptz,
            storage_deleted_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_community_attachments_storage_key UNIQUE (storage_key),
            CONSTRAINT ck_community_attachments_position CHECK (position BETWEEN 0 AND 2),
            CONSTRAINT ck_community_attachments_status
                CHECK (status IN ('uploaded','attached','deleted','orphaned')),
            CONSTRAINT ck_community_attachments_delete_attempts CHECK (delete_attempts >= 0),
            CONSTRAINT ck_community_attachments_uploaded CHECK (
                status <> 'uploaded' OR (post_id IS NULL AND position IS NULL)),
            CONSTRAINT ck_community_attachments_attached CHECK (
                status <> 'attached' OR (post_id IS NOT NULL AND position IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_community_attachments_post_position "
        "ON community_attachments (post_id, position) WHERE status = 'attached'"
    )
    op.execute(
        "CREATE INDEX ix_community_attachments_uploader ON community_attachments (uploader_id)"
    )
    op.execute(
        "CREATE INDEX ix_community_attachments_post ON community_attachments (post_id, position)"
    )
    op.execute(
        "CREATE INDEX ix_community_attachments_cleanup ON community_attachments "
        "(status, next_delete_attempt_at) WHERE next_delete_attempt_at IS NOT NULL"
    )

    # ------------------------------------------------------------------
    # 8) 新建 board_applications
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE community_board_applications (
            application_id uuid PRIMARY KEY,
            applicant_id uuid NOT NULL,
            name varchar(80) NOT NULL,
            slug varchar(64) NOT NULL,
            description varchar(500) NOT NULL DEFAULT '',
            reason varchar(500) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'pending',
            board_id uuid REFERENCES community_boards(board_id) ON DELETE RESTRICT,
            reviewer_id uuid,
            reviewed_at timestamptz,
            reject_reason varchar(500),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_community_board_applications_board UNIQUE (board_id),
            CONSTRAINT ck_community_board_applications_status
                CHECK (status IN ('pending','approved','rejected')),
            CONSTRAINT ck_community_board_applications_pending CHECK (
                status <> 'pending' OR (board_id IS NULL AND reviewer_id IS NULL
                    AND reviewed_at IS NULL AND reject_reason IS NULL)),
            CONSTRAINT ck_community_board_applications_approved CHECK (
                status <> 'approved' OR (board_id IS NOT NULL AND reviewer_id IS NOT NULL
                    AND reviewed_at IS NOT NULL AND reject_reason IS NULL)),
            CONSTRAINT ck_community_board_applications_rejected CHECK (
                status <> 'rejected' OR (board_id IS NULL AND reviewer_id IS NOT NULL
                    AND reviewed_at IS NOT NULL AND reject_reason IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_community_board_applications_pending_name "
        "ON community_board_applications (lower(name)) WHERE status = 'pending'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_community_board_applications_pending_slug "
        "ON community_board_applications (slug) WHERE status = 'pending'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_community_board_applications_pending_applicant "
        "ON community_board_applications (applicant_id) WHERE status = 'pending'"
    )
    op.execute(
        "CREATE INDEX ix_community_board_applications_applicant "
        "ON community_board_applications (applicant_id, created_at DESC, application_id DESC)"
    )
    op.execute(
        "CREATE INDEX ix_community_board_applications_review "
        "ON community_board_applications (status, created_at ASC, application_id ASC)"
    )


def downgrade() -> None:
    """回滚 0002：删新表/列，恢复 0001 约束；不恢复已清空数据。"""
    # 删新表
    op.execute("DROP TABLE IF EXISTS community_board_applications CASCADE")
    op.execute("DROP TABLE IF EXISTS community_attachments CASCADE")

    # idempotency 恢复
    op.execute(
        "ALTER TABLE community_idempotency_requests DROP CONSTRAINT IF EXISTS "
        "ck_community_idempotency_operation"
    )
    op.execute(
        "ALTER TABLE community_idempotency_requests DROP CONSTRAINT IF EXISTS "
        "ck_community_idempotency_resource_type"
    )
    op.execute(
        "DELETE FROM community_idempotency_requests WHERE operation IN "
        "('upload_attachment','create_application')"
    )
    op.execute(
        "ALTER TABLE community_idempotency_requests ADD CONSTRAINT ck_community_idempotency_operation "
        "CHECK (operation IN ('create_post','create_reply'))"
    )
    op.execute(
        "ALTER TABLE community_idempotency_requests ADD CONSTRAINT ck_community_idempotency_resource_type "
        "CHECK (resource_type IN ('post','reply'))"
    )

    # notifications 恢复
    op.execute(
        "ALTER TABLE community_notifications DROP CONSTRAINT IF EXISTS "
        "ck_community_notifications_event_type"
    )
    op.execute(
        "DELETE FROM community_notifications WHERE event_type IN "
        "('application_approved','application_rejected')"
    )
    op.execute(
        "ALTER TABLE community_notifications ADD CONSTRAINT ck_community_notifications_event_type "
        "CHECK (event_type IN ('post_replied','reply_marked_solved'))"
    )
    op.execute(
        "DELETE FROM community_notifications WHERE post_id IS NULL OR reply_id IS NULL"
    )
    op.execute(
        "ALTER TABLE community_notifications ALTER COLUMN post_id SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE community_notifications ALTER COLUMN reply_id SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE community_notifications DROP COLUMN IF EXISTS board_slug"
    )

    # boards 恢复
    op.execute(
        "ALTER TABLE community_boards DROP CONSTRAINT IF EXISTS ck_community_boards_post_count"
    )
    op.execute(
        "DROP INDEX IF EXISTS uq_community_boards_lower_name"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_community_boards_created_by"
    )
    op.execute(
        "ALTER TABLE community_boards DROP COLUMN IF EXISTS created_by"
    )
    op.execute(
        "ALTER TABLE community_boards DROP COLUMN IF EXISTS updated_at"
    )
    op.execute(
        "ALTER TABLE community_boards DROP COLUMN IF EXISTS post_count"
    )
    op.execute(
        "ALTER TABLE community_boards ALTER COLUMN sort_order SET DEFAULT 0"
    )
