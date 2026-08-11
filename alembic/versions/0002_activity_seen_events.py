"""0002 activity exposure 幂等去重表（裁决 A，2026-08-11）。

§10.3.1 要求 (user_id, node_id, activity_type, activity_id) 级 at-most-once 计数；
§13 冻结表清单无此存储，经裁决新增本表。seen 插入与计数 upsert 在同一事务。
"""

from __future__ import annotations

from alembic import op

revision = "0002_activity_seen_events"
down_revision = "0001_memory_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE graph_activity_seen_events (
            user_id uuid NOT NULL,
            node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id),
            activity_type text NOT NULL CHECK (activity_type IN (
                'page_view', 'bookmark', 'check_in'
            )),
            activity_id varchar(200) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, node_id, activity_type, activity_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_graph_activity_seen_user "
        "ON graph_activity_seen_events (user_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS graph_activity_seen_events")
