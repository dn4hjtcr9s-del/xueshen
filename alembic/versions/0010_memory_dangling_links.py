"""memory_dangling_links 悬空链接两级制计数表（memory-rebuild §3.2 / §5.9④ / 决议表 E 组）。

§5.9④：`[[link]]` 悬空目标**第一次出现只加入 index 的候选主题区，累计至少两个
批次出现才创建正式 mastery 文档**（决议表 E 组"悬空链接两级制"）。判定"累计两个
批次"需要**跨批次**计数，因此单开一张表承载它（2026-09-12 裁决）：

- 不写进 ``index.md``：index 是可再生派生物，``rebuild_index`` 会整份重写，
  计数必然丢失；
- 不复用 ``memory_review_candidates``：那是人工审核队列（pending/accepted/
  corrected/rejected），语义是"等人裁决"，与"自动累计出现批次数"不是一回事。

列的语义要点：

- ``target`` 是 ``[[...]]`` 里的原始文本（展示用）；``target_key`` 是规范化比较键
  （``contracts/common.normalize_topic_title``：NFKC + 去首尾空白，**不改大小写**），
  ``UNIQUE (user_id, target_key)`` 保证同一用户同一目标只有一行，跨批次靠 upsert 累加。
- ``sighting_batches`` 是**出现过多少个不同批次**，不是出现次数：同一批次里同一目标
  出现多次只算 1 次，同一批次重跑（幂等重试）也不得再 +1。
- ``first/last_batch_operation_id`` 让候选与正式主题都能从 ``batch_operation_id``
  回溯到来源批次（§5.9 验收）。刻意**不加**外键：这两个列是审计用的软引用，
  不能反过来阻塞 ``memory_operations`` 的清理（账号删除会物理删除 operation 行）。
- ``source_memory_ids`` 记录哪些文档里出现过该悬空链接（去重、上限 100，与
  ``memory_review_candidates.evidence_refs`` 的上限口径一致）。

回滚语义（与 0001/0004/0006 的"新表直接 DROP"惯例一致，memory-rebuild 第 1005 行
也要求"迁移可在空库和已有测试数据上分别执行 upgrade/downgrade"）：``downgrade``
直接删表，**已累计的批次数随之丢失**（不阻塞回滚，如需保留请先导出或等重建批次）。
"""

from __future__ import annotations

from alembic import op

revision = "0010_memory_dangling_links"
down_revision = "0009_memory_migrate_schema_op"
branch_labels = None
depends_on = None

#: source_memory_ids 的元素上限（与 0001 的 evidence_refs <= 100 保持同一口径）。
MAX_SOURCE_MEMORY_IDS = 100


def upgrade() -> None:
    """建悬空链接计数表与两个用户维度索引（幂等、可重复执行）。"""
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS memory_dangling_links (
            link_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            target varchar(160) NOT NULL,
            target_key varchar(160) NOT NULL,
            status text NOT NULL DEFAULT 'candidate' CHECK (status IN (
                'candidate', 'promoted', 'dismissed'
            )),
            sighting_batches integer NOT NULL DEFAULT 0 CHECK (sighting_batches >= 0),
            first_seen_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            first_batch_operation_id uuid,
            last_batch_operation_id uuid,
            source_memory_ids jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
                jsonb_typeof(source_memory_ids) = 'array'
                AND jsonb_array_length(source_memory_ids) <= {MAX_SOURCE_MEMORY_IDS}
            ),
            promoted_memory_id varchar(160),
            CONSTRAINT uq_memory_dangling_links_user_target UNIQUE (user_id, target_key)
        )
        """
    )
    # 运维排查：某用户当前有哪些候选/已建档/已否决。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_dangling_links_user_status "
        "ON memory_dangling_links (user_id, status)"
    )
    # consolidation 取候选：status='candidate' AND sighting_batches >= 2，
    # 按出现批次数倒序取前 N（§5.9④ 的建档门槛查询）。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_dangling_links_user_batches "
        "ON memory_dangling_links (user_id, sighting_batches DESC)"
    )


def downgrade() -> None:
    """删表回滚：索引随表删除，显式 DROP 只为让回滚脚本可独立重放。

    **已累计的 sighting_batches 与候选状态一并丢失**（无用户正文，不影响删除合规）；
    这是新表回滚的仓库惯例（0001/0004/0006），也让"带数据的库"仍可安全 downgrade。
    """
    op.execute("DROP INDEX IF EXISTS ix_memory_dangling_links_user_batches")
    op.execute("DROP INDEX IF EXISTS ix_memory_dangling_links_user_status")
    op.execute("DROP TABLE IF EXISTS memory_dangling_links")
