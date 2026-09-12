"""mastery 提交的 KG 映射保持与索引投影三处缺陷的回归测试。

对应 review 的 I-3 与 I-11（真实 PG + 真实文件存储）：

- **I-3**：``frontmatter_patch`` 这类**不携带图谱信息**的提交曾无条件先
  ``deactivate_graph_links`` 再按 ``node_ids``（空）重建 → 该主题的 KG 映射整片
  ``active=false``，紧接着的 ``_dual_write_kg`` 因 ``active=true AND memory_version``
  不成立而必然 ``skipped/no_graph_mapping``。现在按既有 link 的 node_id 在新版本上
  重新 upsert（保持 active、memory_version 跟上），并且只有调用方**确实给出节点集合**
  时才把消失的节点置 inactive。
- **I-11②**：``related_topic_keys`` 曾取上一版解析结果的 ``.links``（patch 路径都不重算
  它）→ 首次写入含 ``[[抛物线]]`` 的正文投影为空、之后永远滞后一个提交。现在从**新渲染
  正文**现算。
- **I-11③**：v2 文档的注册表 ``title`` 曾取 ``topic_title`` → 改 ``name`` 的
  ``frontmatter_patch`` 到不了注册表与 search。现在 v2 取 frontmatter ``name``，
  v1 保持 ``topic_title`` 逐字不变。
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
from backend.memory.storage.markdown_schema import SCHEMA_VERSION_V2, parse_mastery

USER = UUID("00000000-0000-4000-8000-0000000000b2")
TOPIC_KEY = "抛物线"
MEMORY_ID = f"mastery:{TOPIC_KEY}"
NODE_ID = "n7711"
LINKED_TOPIC = "圆锥曲线"


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


async def _seed_graph_node(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO knowledge_graph_nodes (node_id, title, source_file, "
                    "source_checksum) VALUES (:n, :t, 'test.md', :ck) "
                    "ON CONFLICT (node_id) DO NOTHING"
                ),
                {"n": NODE_ID, "t": TOPIC_KEY, "ck": "0" * 64},
            )


async def _links(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[dict[str, object]]:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT node_id, memory_version, active, mapping_method, mapping_confidence "
                "FROM memory_graph_links WHERE user_id = :u AND memory_id = :m "
                "ORDER BY node_id"
            ),
            {"u": USER, "m": MEMORY_ID},
        )
        return [dict(row) for row in result.mappings().all()]


async def _index_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, object] | None:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT title, summary, keywords, aliases, related_topic_keys, source_version "
                "FROM memory_index_entries WHERE user_id = :u AND memory_id = :m"
            ),
            {"u": USER, "m": MEMORY_ID},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def _document_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT active_version FROM memory_documents WHERE user_id = :u AND memory_id = :m"
            ),
            {"u": USER, "m": MEMORY_ID},
        )
        return int(result.scalar_one())


async def _create_mastery_with_graph_link(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """建档：mastery 文档（含 ``[[圆锥曲线]]`` 链接）+ 一条 KG 映射（v1）。"""
    await _seed_graph_node(session_factory)
    plan = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        topic_title=TOPIC_KEY,
        action="create",
        mastery_patch=MasteryPatch(
            overview=f"与 [[{LINKED_TOPIC}]] 同族的圆锥曲线",
            understood_to_add=["掌握焦点与准线"],
        ),
    )
    outcome = await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[plan],
        graph_node_ids_by_plan=[[NODE_ID]],
        mapping_methods_by_plan=["exact_alias"],
        mapping_confidences_by_plan=[0.9],
    )
    assert outcome.mutations[0].after_version == 1


async def test_frontmatter_patch_keeps_graph_links_active(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """I-3 回归：只补 frontmatter 的提交不得摧毁 mastery↔KG 映射。"""
    await _create_mastery_with_graph_link(memory_service, session_factory)
    before = await _links(session_factory)
    assert [(row["node_id"], row["memory_version"], row["active"]) for row in before] == [
        (NODE_ID, 1, True)
    ]

    # 纯 frontmatter 补丁（不携带 graph_node_ids_by_plan，正是 consolidation 的 keywords /
    # aliases 治理路径）：描述与别名变了，映射语义没变
    patch = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        topic_title=TOPIC_KEY,
        action="merge",
        expected_version=1,
        frontmatter_patch=FrontMatterPatch(
            description="平面内到定点与定直线距离相等的点的轨迹",
            aliases=["parabola"],
        ),
    )
    outcome = await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[patch],
    )
    after_version = outcome.mutations[0].after_version
    assert after_version == 2

    after = await _links(session_factory)
    assert len(after) == 1
    row = after[0]
    # 映射仍然 active，且版本跟上了新活动版本（这正是 _dual_write_kg 的读取条件）
    assert row["node_id"] == NODE_ID
    assert row["active"] is True
    assert int(row["memory_version"]) == after_version
    assert row["mapping_method"] == "exact_alias"
    assert float(row["mapping_confidence"]) == 0.9


async def test_explicit_empty_node_list_is_the_only_way_to_drop_mappings(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """带节点集合的提交仍按"本次集合为准"处理：不在集合里的旧映射被置 inactive。"""
    await _create_mastery_with_graph_link(memory_service, session_factory)
    other_node = "n7712"
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO knowledge_graph_nodes (node_id, title, source_file, "
                    "source_checksum) VALUES (:n, :t, 'test.md', :ck) "
                    "ON CONFLICT (node_id) DO NOTHING"
                ),
                {"n": other_node, "t": "圆锥曲线", "ck": "0" * 64},
            )
    # 先把第二条映射也建起来（同一主题两个节点）
    plan = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        action="merge",
        expected_version=1,
        mastery_patch=MasteryPatch(understood_to_add=["掌握第二定义"]),
    )
    await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[plan],
        graph_node_ids_by_plan=[[NODE_ID, other_node]],
        mapping_methods_by_plan=["exact_alias"],
        mapping_confidences_by_plan=[0.8],
    )
    assert {row["node_id"] for row in await _links(session_factory)} == {NODE_ID, other_node}

    # 再提交一次，只保留 NODE_ID：消失的 other_node 必须被置 inactive
    drop = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        action="merge",
        expected_version=2,
        mastery_patch=MasteryPatch(understood_to_add=["复习准线"]),
    )
    await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[drop],
        graph_node_ids_by_plan=[[NODE_ID]],
        mapping_methods_by_plan=["exact_alias"],
        mapping_confidences_by_plan=[0.9],
    )
    by_node = {row["node_id"]: row for row in await _links(session_factory)}
    assert by_node[NODE_ID]["active"] is True
    assert int(by_node[NODE_ID]["memory_version"]) == 3
    assert by_node[other_node]["active"] is False


async def test_related_topic_keys_are_projected_on_first_commit(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """I-11② 回归：首次写入含 ``[[链接]]`` 的正文，投影必须当次就正确（不留滞后一版）。"""
    await _seed_graph_node(session_factory)
    plan = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        topic_title=TOPIC_KEY,
        action="create",
        mastery_patch=MasteryPatch(
            overview=f"与 [[{LINKED_TOPIC}]] 关系密切",
            difficulties_to_add=[f"与 [[{LINKED_TOPIC}]] 的判别条件容易混"],
        ),
    )
    await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[plan],
    )
    row = await _index_row(session_factory)
    assert row is not None
    assert row["source_version"] == 1
    assert list(row["related_topic_keys"]) == [LINKED_TOPIC]


async def test_v2_registry_title_uses_frontmatter_name(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
) -> None:
    """I-11③ 回归：v2 文档的注册表 title 取 frontmatter name，改 name 的补丁要能投影。"""
    await _seed_graph_node(session_factory)
    create = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        topic_title=TOPIC_KEY,
        action="create",
        mastery_patch=MasteryPatch(overview="圆锥曲线之一"),
        frontmatter_patch=FrontMatterPatch(name="抛物线的标准方程", description="圆锥曲线之一"),
    )
    await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[create],
    )
    document = (await store.read_current(user_id=USER, memory_id=MEMORY_ID)).decode("utf-8")
    parsed = parse_mastery(document)
    assert parsed.schema_version == SCHEMA_VERSION_V2
    assert parsed.name == "抛物线的标准方程"
    row = await _index_row(session_factory)
    assert row is not None
    assert row["title"] == "抛物线的标准方程"

    # 再改一次 name：注册表必须跟上（旧实现里 topic_title 才是 title，改 name 永远不可见）
    rename = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        action="merge",
        expected_version=1,
        frontmatter_patch=FrontMatterPatch(name="抛物线及其性质"),
    )
    await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[rename],
    )
    renamed = await _index_row(session_factory)
    assert renamed is not None
    assert renamed["title"] == "抛物线及其性质"
    assert renamed["source_version"] == 2
    assert await _document_version(session_factory) == 2


async def test_v1_document_registry_title_stays_topic_title(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """v1 文档没有 frontmatter name：注册表 title 仍取 topic_title（不回归成空标题）。"""
    await _seed_graph_node(session_factory)
    plan = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        topic_title=TOPIC_KEY,
        action="create",
        mastery_patch=MasteryPatch(overview="圆锥曲线之一"),
    )
    await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[plan],
    )
    row = await _index_row(session_factory)
    assert row is not None
    assert row["title"] == TOPIC_KEY


async def test_refresh_index_projection_backfills_v2_columns(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
) -> None:
    """I-11①：迁移路径可调用的投影刷新入口，能把 aliases/keywords/related 回填。"""
    await _seed_graph_node(session_factory)
    plan = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        topic_title=TOPIC_KEY,
        action="create",
        mastery_patch=MasteryPatch(overview=f"与 [[{LINKED_TOPIC}]] 关系密切"),
        frontmatter_patch=FrontMatterPatch(
            name="抛物线的标准方程",
            description="圆锥曲线之一",
            aliases=["parabola"],
            keywords=["焦点", "准线"],
        ),
    )
    await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[plan],
    )
    # 模拟"v1→v2 迁移只改了文档与活动版本、没有刷新投影"的历史状态
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE memory_index_entries SET aliases = ARRAY[]::text[], "
                    "keywords = ARRAY[]::text[], related_topic_keys = ARRAY[]::text[], title = :t "
                    "WHERE user_id = :u AND memory_id = :m"
                ),
                {"u": USER, "m": MEMORY_ID, "t": TOPIC_KEY},
            )
            await session.execute(
                text(
                    "UPDATE memory_documents SET index_dirty_at = NULL "
                    "WHERE user_id = :u AND memory_id = :m"
                ),
                {"u": USER, "m": MEMORY_ID},
            )
    assert list((await _index_row(session_factory))["aliases"]) == []  # type: ignore[index]

    # 迁移处理器要调用的那一行：refresh_index_projection（与 set_active_version 同事务）
    async with session_factory() as session:
        async with session.begin():
            refreshed = await memory_service.refresh_index_projection(
                session, user_id=USER, memory_id=MEMORY_ID
            )
    assert refreshed is True

    row = await _index_row(session_factory)
    assert row is not None
    assert list(row["aliases"]) == ["parabola"]
    assert list(row["keywords"]) == ["焦点", "准线"]
    assert list(row["related_topic_keys"]) == [LINKED_TOPIC]
    assert row["title"] == "抛物线的标准方程"
    # index dirty 标在 index 文档行上（rebuild_index 的触发条件）；`mark_index_dirty`
    # 自己会 upsert index 文档行，所以这里不需要先造行。
    async with session_factory() as session:
        dirty = await session.execute(
            text(
                "SELECT index_dirty_at FROM memory_documents "
                "WHERE user_id = :u AND memory_id = 'index'"
            ),
            {"u": USER},
        )
        assert dirty.scalar_one() is not None, "刷新投影后必须标 index dirty（等 rebuild）"
    # 文档本体不受影响（迁移只追加版本，本入口只刷投影）
    document = (await store.read_current(user_id=USER, memory_id=MEMORY_ID)).decode("utf-8")
    assert parse_mastery(document).name == "抛物线的标准方程"
