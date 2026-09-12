"""memory_commits 放行 ``frontmatter_patch`` 动作（memory-rebuild §3.6① / §5.9①）。

**这是一个真实缺陷的修复，不是新功能**：Phase 4 把 ``frontmatter_patch`` 加进了
``contracts/commands.py::CommitMutationPlan.action`` 与 ``MutationResult.action``
（§3.6① 要求 planner 能维护 v2 frontmatter 的 name/description/aliases），但
``memory_commits.action`` 的 CHECK 约束仍是 0001 里那份 7 值清单，**没有同步**。

后果：任何真正走到提交的 ``frontmatter_patch`` 计划都会在写审计行时抛
``CheckViolation``（实测在 Phase 7 consolidation 的 keywords/aliases 治理提交上暴露：
`new row for relation "memory_commits" violates check constraint
"memory_commits_action_check"`）。契约、提示词、图节点三层都"支持"了这个动作，
只有数据库层拒绝——与 ADD-044 记录的"新增 operation_type 要同步 4 处"是同一类缺口。

修法与 ADD-044 一致：**新建迁移扩展约束**，不回头改 0001/0008 的历史迁移。
"""

from __future__ import annotations

from alembic import op

revision = "0011_memory_commits_fm_action"
down_revision = "0010_memory_dangling_links"
branch_labels = None
depends_on = None

_ACTIONS = (
    "create",
    "merge",
    "replace",
    "append_evidence",
    "forget",
    "restore",
    "rebuild_index",
    # memory-rebuild §3.6①（Phase 4 契约已含，DB 侧遗漏到 Phase 7 才补）
    "frontmatter_patch",
)

_CONSTRAINT = "memory_commits_action_check"


def upgrade() -> None:
    """把 ``frontmatter_patch`` 放行到 memory_commits.action。"""
    op.execute(f"ALTER TABLE memory_commits DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    allowed = ", ".join(f"'{value}'" for value in _ACTIONS)
    op.execute(
        f"ALTER TABLE memory_commits ADD CONSTRAINT {_CONSTRAINT} CHECK (action IN ({allowed}))"
    )


def downgrade() -> None:
    """收窄前先确认没有 frontmatter_patch 行，避免回滚静默丢审计语义。"""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM memory_commits WHERE action = 'frontmatter_patch'
            ) THEN
                RAISE EXCEPTION
                    '0011_memory_commits_fm_action 无法回滚：'
                    'memory_commits 已存在 frontmatter_patch 行';
            END IF;
        END $$
        """
    )
    op.execute(f"ALTER TABLE memory_commits DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    allowed = ", ".join(f"'{value}'" for value in _ACTIONS if value != "frontmatter_patch")
    op.execute(
        f"ALTER TABLE memory_commits ADD CONSTRAINT {_CONSTRAINT} CHECK (action IN ({allowed}))"
    )
