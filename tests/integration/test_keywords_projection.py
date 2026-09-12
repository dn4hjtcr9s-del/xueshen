"""keywords 投影链路集成测试（memory-rebuild §2.3 / §3.4；真实 PG + 真实文件存储）。

覆盖用户 2026-09-12 裁决 A（keywords 是 v2 frontmatter 字段，文档是唯一事实源）：

- 真实 commit：keywords 从文档投影进 PG ``memory_index_entries.keywords``；
- ``rebuild_index``：index.md 的 v2 块里 ``- keywords:`` / ``- description:`` /
  ``- aliases:`` 确实有值（这曾经是空投影的缺口）；
- ``frontmatter_patch`` 只改 keywords：文档升版、**旧版本文件字节级不变**；
- forget → restore：恢复路径同样从文档重新投影 keywords。
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import (
    CommitMutationPlan,
    FrontMatterPatch,
    MasteryPatch,
)
from backend.memory.services.memory_service import MemoryService
from backend.memory.storage.local_markdown import LocalMarkdownStore
from backend.memory.storage.markdown_schema import (
    SCHEMA_VERSION_V2,
    parse_index,
    parse_mastery,
)

USER = UUID("00000000-0000-4000-8000-0000000000a7")
MEMORY_ID = "mastery:椭圆"
KEYWORDS = ["焦点", "准线", "离心率 e"]
ALIASES = ["ellipse", "椭圆形"]


async def _insert_operation(session: AsyncSession, operation_id: UUID) -> None:
    """插入最小 operation 行满足 memory_commits 外键。"""
    await session.execute(
        text(
            "INSERT INTO memory_operations ("
            "operation_id, user_id, actor_type, input_kind, operation_type,"
            "idempotency_key, idempotency_payload_hash, priority, status,"
            "payload, trace_id, graph_thread_id, occurred_at, max_attempts"
            ") VALUES ("
            ":operation_id, :user_id, 'user', 'command', 'correct_memory',"
            ":idem, :idem_hash, 10, 'running',"
            "'{}'::jsonb, :trace_id, 'graph-test', now(), 3)"
        ),
        {
            "operation_id": operation_id,
            "user_id": USER,
            "idem": f"idem-{operation_id}",
            "idem_hash": "0" * 64,
            "trace_id": uuid4().hex + uuid4().hex,
        },
    )


async def _new_operation(session_factory: async_sessionmaker[AsyncSession]) -> UUID:
    operation_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, operation_id)
    return operation_id


async def _create_mastery_with_keywords(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    plan = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        topic_title="椭圆",
        action="create",
        mastery_patch=MasteryPatch(
            overview="圆锥曲线之一，焦点性质易与抛物线混淆",
            understood_to_add=["掌握第一定义"],
        ),
        frontmatter_patch=FrontMatterPatch(
            name="椭圆",
            description="圆锥曲线之一",
            aliases=list(ALIASES),
            keywords=list(KEYWORDS),
        ),
    )
    outcome = await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[plan],
    )
    assert outcome.mutations[0].after_version == 1


async def _index_row(
    session_factory: async_sessionmaker[AsyncSession], memory_id: str
) -> dict | None:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT title, summary, keywords, aliases, related_topic_keys,"
                " source_version FROM memory_index_entries"
                " WHERE user_id = :u AND memory_id = :m"
            ),
            {"u": USER, "m": memory_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def _active_storage_key(
    session_factory: async_sessionmaker[AsyncSession], memory_id: str
) -> str:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT active_storage_key FROM memory_documents"
                " WHERE user_id = :u AND memory_id = :m"
            ),
            {"u": USER, "m": memory_id},
        )
        return str(result.scalar_one())


async def _rebuild_index(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> dict:
    return await memory_service.rebuild_index(
        user_id=USER, operation_id=await _new_operation(session_factory)
    )


async def test_commit_projects_keywords_into_pg_and_index_md(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
) -> None:
    """真实 commit 的 keywords 必须同时落到文档、PG 索引列与 index.md。"""
    await _create_mastery_with_keywords(memory_service, session_factory)

    # 1) 文档（唯一事实源）已是 v2 且带 keywords
    document = (await store.read_current(user_id=USER, memory_id=MEMORY_ID)).decode("utf-8")
    parsed = parse_mastery(document)
    assert parsed.schema_version == SCHEMA_VERSION_V2
    assert parsed.keywords == KEYWORDS
    assert parsed.aliases == ALIASES

    # 2) PG 索引行有值（keywords 落 memory_index_entries.keywords）
    row = await _index_row(session_factory, MEMORY_ID)
    assert row is not None
    assert list(row["keywords"]) == KEYWORDS
    assert list(row["aliases"]) == ALIASES
    assert row["summary"] == "圆锥曲线之一，焦点性质易与抛物线混淆"
    assert row["source_version"] == 1

    # 3) rebuild_index 把 PG 投影写进 index.md 的 v2 块（缺口回归点）
    result = await _rebuild_index(memory_service, session_factory)
    assert result["rebuilt"] is True
    index_text = (await store.read_current(user_id=USER, memory_id="index")).decode("utf-8")
    assert "schema_version: 2" in index_text
    assert "### mastery:椭圆" in index_text
    assert f"- keywords: {' | '.join(KEYWORDS)}" in index_text
    assert f"- aliases: {' | '.join(ALIASES)}" in index_text
    # 注意：index 的 description 投影的是 PG ``summary`` 列（正文 overview），
    # **不是** frontmatter description —— 现状如此，差异记在交付报告里。
    assert f"- description: {row['summary']}" in index_text
    assert str(row["summary"]).strip(), "description 不能是空投影"

    index_doc = parse_index(index_text)
    entry = next(e for e in index_doc.mastery_entries if e.memory_id == MEMORY_ID)
    assert entry.keywords == KEYWORDS
    assert entry.aliases == ALIASES
    assert entry.description == str(row["summary"])
    # index 的 name 同样来自 memory_documents.topic_title（正文标题）
    assert entry.title == "椭圆"


async def test_keywords_only_frontmatter_patch_bumps_version_and_keeps_history(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
) -> None:
    """只补 keywords：文档升版、新旧关键词合并，旧版本文件字节级不变（§5.6）。

    这里用 ``action="merge"`` 承载纯 frontmatter 补丁（契约明示 create/merge 是
    frontmatter 的写入通道）；``action="frontmatter_patch"`` 目前被 DB CHECK 拦住，
    见 ``test_frontmatter_patch_action_*`` 的说明。
    """
    await _create_mastery_with_keywords(memory_service, session_factory)
    first_key = await _active_storage_key(session_factory, MEMORY_ID)
    first_bytes = await store.read_version(user_id=USER, storage_key=first_key)

    patch = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        action="merge",
        expected_version=1,
        frontmatter_patch=FrontMatterPatch(keywords=["判别式", "焦点"]),
    )
    outcome = await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[patch],
    )
    assert outcome.mutations[0].after_version == 2

    # 合并语义：保留原有 + 追加新词 + 去重保序；name/description/aliases 不受影响
    document = (await store.read_current(user_id=USER, memory_id=MEMORY_ID)).decode("utf-8")
    parsed = parse_mastery(document)
    assert parsed.keywords == ["焦点", "准线", "离心率 e", "判别式"]
    assert (parsed.name, parsed.description, parsed.aliases) == (
        "椭圆",
        "圆锥曲线之一",
        ALIASES,
    )
    assert parsed.schema_version == SCHEMA_VERSION_V2

    # 旧版本文件（versions/ 不可变历史）必须字节级不变
    assert await store.read_version(user_id=USER, storage_key=first_key) == first_bytes
    assert await _active_storage_key(session_factory, MEMORY_ID) != first_key

    # PG 投影同步更新，版本号随之前进
    row = await _index_row(session_factory, MEMORY_ID)
    assert row is not None
    assert list(row["keywords"]) == ["焦点", "准线", "离心率 e", "判别式"]
    assert row["source_version"] == 2


async def test_restore_reprojects_keywords_from_document(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
) -> None:
    """forget → restore：删除会清掉索引行，恢复路径必须从文档重新投影 keywords。"""
    await _create_mastery_with_keywords(memory_service, session_factory)

    forgotten = await memory_service.forget(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        expected_version=1,
        reason="测试 keywords 恢复投影",
    )
    assert forgotten.action == "forget"
    assert await _index_row(session_factory, MEMORY_ID) is None

    restored = await memory_service.restore(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        deleted_version=1,
    )
    assert restored.after_version == 2

    row = await _index_row(session_factory, MEMORY_ID)
    assert row is not None
    assert list(row["keywords"]) == KEYWORDS
    assert list(row["aliases"]) == ALIASES
    assert row["source_version"] == 2

    document = (await store.read_current(user_id=USER, memory_id=MEMORY_ID)).decode("utf-8")
    assert parse_mastery(document).keywords == KEYWORDS


async def test_index_rebuild_is_noop_without_dirty_mark(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
) -> None:
    """重建后 dirty 已清；再次 rebuild 不产生新版本（keywords 不会重复投影）。"""
    await _create_mastery_with_keywords(memory_service, session_factory)
    first = await _rebuild_index(memory_service, session_factory)
    assert first["rebuilt"] is True
    index_key = await _active_storage_key(session_factory, "index")

    second = await _rebuild_index(memory_service, session_factory)
    assert second == {"rebuilt": False, "reason": "not_dirty"}
    assert await _active_storage_key(session_factory, "index") == index_key
    index_text = (await store.read_current(user_id=USER, memory_id="index")).decode("utf-8")
    assert f"- keywords: {' | '.join(KEYWORDS)}" in index_text


async def _commit_action_constraint(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname = 'memory_commits_action_check'"
            )
        )
        return str(result.scalar_one_or_none() or "")


async def test_frontmatter_patch_action_projects_keywords(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
) -> None:
    """``action="frontmatter_patch"`` 端到端（consolidation 节点用的正是它）。

    迁移 0001 的 ``memory_commits_action_check`` 曾漏掉这个动作（Phase 4 加了契约
    却没人放行约束，提交会撞 IntegrityError），迁移 0011 已放行。这里显式断言约束
    仍然允许它：未来任何迁移若把这条通道收窄，本测试必须立刻变红。
    """
    definition = await _commit_action_constraint(session_factory)
    assert "frontmatter_patch" in definition, (
        "memory_commits.action CHECK 不放行 frontmatter_patch："
        "consolidation 的 keywords 补丁会在写 commit 行时整事务回滚"
    )

    await _create_mastery_with_keywords(memory_service, session_factory)
    first_key = await _active_storage_key(session_factory, MEMORY_ID)
    first_bytes = await store.read_version(user_id=USER, storage_key=first_key)

    outcome = await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="system",
        plans=[
            CommitMutationPlan(
                mutation_id=uuid4(),
                memory_id=MEMORY_ID,
                target_memory_type="mastery",
                action="frontmatter_patch",
                expected_version=1,
                frontmatter_patch=FrontMatterPatch(keywords=["判别式"]),
            )
        ],
    )
    assert outcome.mutations[0].after_version == 2
    assert await store.read_version(user_id=USER, storage_key=first_key) == first_bytes
    document = (await store.read_current(user_id=USER, memory_id=MEMORY_ID)).decode("utf-8")
    assert parse_mastery(document).keywords == [*KEYWORDS, "判别式"]
