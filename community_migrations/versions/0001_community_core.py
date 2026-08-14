"""创建 Community 域核心 schema（方案 §7.1–§7.7，v1.6 冻结）。

独立 community 数据库（public schema），不创建或修改任何 Memory/Auth/
Conversation 表。板块 seed 使用 §7.1 固定 UUID 幂等写入，禁止随机生成。
"""

from __future__ import annotations

from alembic import op

revision = "0001_community_core"
down_revision = None
branch_labels = None
depends_on = None

# §7.1 冻结的板块 seed（UUIDv5(namespace=8f0db4c4-0b5c-4f6d-a2b3-c86ef29a8d4a,
# name="community-board:{slug}")）。文案为 v1.5 冻结物，执行期不得改写。
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


def upgrade() -> None:
    """创建 Community 全部表、索引、约束与板块 seed（§7.1–§7.7）。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ------------------------------------------------------------------
    # community_boards（§7.1）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE community_boards (
            board_id uuid PRIMARY KEY,
            slug varchar(64) NOT NULL UNIQUE,
            name varchar(80) NOT NULL,
            description varchar(500) NOT NULL DEFAULT '',
            sort_order integer NOT NULL DEFAULT 0,
            status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'hidden')),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # ------------------------------------------------------------------
    # community_posts（§7.2）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE community_posts (
            post_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            author_display_name varchar(80) NOT NULL,
            board_id uuid NOT NULL REFERENCES community_boards(board_id),
            title varchar(200) NOT NULL,
            body text NOT NULL,
            content_hash char(64) NOT NULL,
            status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'hidden', 'deleted')),
            discussion_status text NOT NULL DEFAULT 'open'
                CHECK (discussion_status IN ('open', 'closed')),
            eligible_for_memory boolean NOT NULL DEFAULT true,
            pinned boolean NOT NULL DEFAULT false,
            solved_reply_id uuid,
            solution_generation integer NOT NULL DEFAULT 0
                CHECK (solution_generation >= 0),
            reply_count integer NOT NULL DEFAULT 0 CHECK (reply_count >= 0),
            like_count integer NOT NULL DEFAULT 0 CHECK (like_count >= 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            last_activity_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz
        )
        """
    )
    # §7.2 列表索引：status=active 的默认列表排序键
    op.execute(
        "CREATE INDEX ix_community_posts_list ON community_posts "
        "(status, pinned, last_activity_at DESC, post_id DESC)"
    )
    op.execute(
        "CREATE INDEX ix_community_posts_user ON community_posts "
        "(user_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_community_posts_board ON community_posts "
        "(board_id) WHERE status = 'active'"
    )

    # ------------------------------------------------------------------
    # community_replies（§7.3）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE community_replies (
            reply_id uuid PRIMARY KEY,
            post_id uuid NOT NULL REFERENCES community_posts(post_id),
            user_id uuid NOT NULL,
            author_display_name varchar(80) NOT NULL,
            body text NOT NULL,
            content_hash char(64) NOT NULL,
            status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'hidden', 'deleted')),
            eligible_for_memory boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_community_replies_post ON community_replies "
        "(post_id, status, created_at ASC, reply_id ASC)"
    )
    op.execute(
        "CREATE INDEX ix_community_replies_user ON community_replies "
        "(user_id, created_at DESC)"
    )

    # 帖子 solved_reply_id 在回复表存在后才可加外键（循环引用拆两步）
    op.execute(
        "ALTER TABLE community_posts ADD CONSTRAINT fk_community_posts_solved_reply "
        "FOREIGN KEY (solved_reply_id) REFERENCES community_replies(reply_id)"
    )

    # ------------------------------------------------------------------
    # community_post_likes（§7.4）：主键 (post_id, user_id) 保证唯一点赞
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE community_post_likes (
            post_id uuid NOT NULL REFERENCES community_posts(post_id),
            user_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (post_id, user_id)
        )
        """
    )

    # ------------------------------------------------------------------
    # community_outbox（§7.5，v1.6 列集冻结）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE community_outbox (
            event_id uuid PRIMARY KEY,
            event_type text NOT NULL CHECK (event_type IN
                ('community.post_created', 'community.reply_created',
                 'community.source_deleted')),
            aggregate_type text NOT NULL CHECK (aggregate_type IN ('post', 'reply')),
            aggregate_id text NOT NULL,
            user_id uuid NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(500) NOT NULL,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN
                       ('pending', 'processing', 'delivered', 'retry_wait', 'dead_letter')),
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            lease_owner varchar(200),
            lease_generation integer NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
            lease_expires_at timestamptz,
            last_error_code varchar(100),
            delivery_result text
                CHECK (delivery_result IN ('published', 'skipped_source_deleted')),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            delivered_at timestamptz,
            UNIQUE (idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_community_outbox_claimable ON community_outbox "
        "(status, next_attempt_at) WHERE status IN ('pending', 'retry_wait')"
    )

    # ------------------------------------------------------------------
    # community_idempotency_requests（§7.6）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE community_idempotency_requests (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL,
            operation text NOT NULL CHECK (operation IN ('create_post', 'create_reply')),
            idempotency_key varchar(200) NOT NULL,
            payload_hash char(64) NOT NULL,
            resource_type text NOT NULL CHECK (resource_type IN ('post', 'reply')),
            resource_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            UNIQUE (user_id, operation, idempotency_key)
        )
        """
    )

    # ------------------------------------------------------------------
    # community_notifications（§7.7）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE community_notifications (
            notification_id uuid PRIMARY KEY,
            recipient_user_id uuid NOT NULL,
            actor_user_id uuid NOT NULL,
            event_type text NOT NULL CHECK (event_type IN
                ('post_replied', 'reply_marked_solved')),
            post_id uuid NOT NULL,
            reply_id uuid NOT NULL,
            title varchar(200) NOT NULL,
            body varchar(300) NOT NULL,
            dedupe_key varchar(500) NOT NULL,
            read_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (dedupe_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_community_notifications_recipient ON community_notifications "
        "(recipient_user_id, created_at DESC)"
    )

    # ------------------------------------------------------------------
    # 板块 seed（§7.1 冻结：固定 UUID + ON CONFLICT 幂等；不覆盖 status）
    # 常量来自本文件顶部冻结元组（无用户输入），直接内联避免 alembic
    # op.execute 不支持参数绑定（仅接受字符串）。
    # ------------------------------------------------------------------
    for board_id, slug, name, description, sort_order in BOARDS_SEED:
        op.execute(
            "INSERT INTO community_boards (board_id, slug, name, description, sort_order) "
            f"VALUES ('{board_id}', '{slug}', '{name}', '{description}', {sort_order}) "
            "ON CONFLICT (slug) DO UPDATE SET "
            "name = EXCLUDED.name, "
            "description = EXCLUDED.description, "
            "sort_order = EXCLUDED.sort_order"
        )


def downgrade() -> None:
    """逆序删除 Community 表（不回滚业务数据，仅用于开发）。"""
    for table in (
        "community_notifications",
        "community_idempotency_requests",
        "community_outbox",
        "community_post_likes",
        "community_replies",
        "community_posts",
        "community_boards",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
