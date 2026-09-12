"""mastery 提交的 KG 映射保持与索引投影三处缺陷的回归测试。

对应 review 的 I-3、I-11 与 review-3 残留（真实 PG + 真实文件存储）：

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
- **review-3 残留**：读侧谓词曾要求"链接版本恰好等于 ``active_version - 1``"。文档版本
  会被**非提交**路径整格甚至多格跳跃（``migrate_markdown_schema_v2`` 升版却从不碰
  ``memory_graph_links``、restore、运维手工修版本、未来的批量回填），一旦落后 ≥2 版，
  无节点提交就再也找不到既有映射，该主题的 KG 双写**永久** ``no_graph_mapping``。
  现在按"``active`` 行里**最高版本**的那一批"取既有映射并对齐到新活动版本；迁移路径
  额外在同一事务里主动对齐一次（既收敛窗口，也让"重跑迁移"成为存量数据的修复入口）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import (
    CommitMutationPlan,
    FrontMatterPatch,
    MaintenanceCommand,
    MasteryPatch,
)
from backend.memory.contracts.common import SYSTEM_MAINTENANCE_USER_ID
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.graph.state import MemoryRuntimeContext
from backend.memory.persistence import documents as docs_repo
from backend.memory.persistence import graph_states as gs_repo
from backend.memory.persistence import maintenance as maintenance_repo
from backend.memory.services.kg_dual_write import DualWriteOutcome, after_consolidation
from backend.memory.services.memory_service import MemoryService
from backend.memory.storage.base import logical_path_for
from backend.memory.storage.local_markdown import LocalMarkdownStore
from backend.memory.storage.markdown_schema import (
    SCHEMA_VERSION_V1,
    SCHEMA_VERSION_V2,
    MasteryDocument,
    parse_mastery,
    render_mastery,
)
from backend.settings import Settings
from tests.integration.graph_helpers import make_operation, persist_operation

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


async def _seed_graph_node(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    node_id: str = NODE_ID,
    title: str = TOPIC_KEY,
) -> None:
    """注册一个 KG 节点（knowledge_graph_nodes 在测试间保留，故用 DO NOTHING）。"""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO knowledge_graph_nodes (node_id, title, source_file, "
                    "source_checksum) VALUES (:n, :t, 'test.md', :ck) "
                    "ON CONFLICT (node_id) DO NOTHING"
                ),
                {"n": node_id, "t": title, "ck": "0" * 64},
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


def _snapshot(rows: list[dict[str, object]]) -> list[tuple[str, int, bool]]:
    """``(node_id, memory_version, active)`` 视图：让"链接推进到哪"的断言逐元素可比。"""
    return [
        (str(row["node_id"]), int(row["memory_version"]), bool(row["active"]))  # type: ignore[arg-type]
        for row in rows
    ]


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
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    schema_v2: bool = False,
) -> None:
    """建档：mastery 文档（含 ``[[圆锥曲线]]`` 链接）+ 一条 KG 映射（v1）。

    ``schema_v2=True`` 时带完整的 ``frontmatter_patch``（name + description），正文
    因此是 v2——用于"文档已是 v2、迁移只走回填分支"的场景。注意半套 frontmatter
    （只给 name）按 ``apply_frontmatter_patch`` 的设计**不升 v2**（v2 解析器要求两者
    齐备），只给 ``mastery_patch`` 的建档同样 render 成 v1 正文。
    """
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
        frontmatter_patch=(
            FrontMatterPatch(name="抛物线的标准方程", description="圆锥曲线之一")
            if schema_v2
            else None
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
    # review-2 新发现 1（Critical）：**每个计划内节点都必须 active=True**。
    # 只断言行集合会让"两行都在、但两条都 active=false"的回归照样通过——正是上一轮
    # `for kept in node_ids: deactivate(except_node_id=kept)` 循环实现漏掉的那一点。
    after_two = await _links(session_factory)
    assert {row["node_id"] for row in after_two} == {NODE_ID, other_node}
    for row in after_two:
        assert row["active"] is True, (
            f"计划内节点 {row['node_id']} 被置为 inactive（多节点提交把所有映射杀掉了）"
        )
        assert int(row["memory_version"]) == 2

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


async def test_planned_node_without_new_method_is_reactivated(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """计划内节点即便本轮没带 mapping_method，也要沿用历史行重新激活（真值表第 4 行）。

    旧实现只在"上一版活动的行"里找 method：节点被上一轮 drop 掉（inactive）后再被
    重新声明时找不到来源，于是它被集合排除挡在外面、永远停在 inactive。现在回退来源
    扩大到"该节点的任意历史行"。
    """
    await _create_mastery_with_graph_link(memory_service, session_factory)
    other_node = "n7713"
    await _seed_graph_node(session_factory, node_id=other_node, title="圆锥曲线")
    # v2：把 two-node 映射建起来
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
        mapping_methods_by_plan=["model_candidate"],
        mapping_confidences_by_plan=[0.6],
    )
    # v3：drop 掉 other_node（active=false，但它那一行的 method 仍在）
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
    assert {row["node_id"]: row["active"] for row in await _links(session_factory)}[other_node] is (
        False
    )

    # v4：重新声明两个节点但**不带任何元数据** → 必须沿用历史行的 method/confidence
    revive = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        action="merge",
        expected_version=3,
        mastery_patch=MasteryPatch(understood_to_add=["综合练习"]),
    )
    await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[revive],
        graph_node_ids_by_plan=[[NODE_ID, other_node]],
    )
    revived = {row["node_id"]: row for row in await _links(session_factory)}
    assert revived[other_node]["active"] is True, "计划内节点必须重新激活"
    assert int(revived[other_node]["memory_version"]) == 4
    assert revived[other_node]["mapping_method"] == "model_candidate"
    assert float(revived[other_node]["mapping_confidence"]) == 0.6
    assert revived[NODE_ID]["active"] is True
    assert int(revived[NODE_ID]["memory_version"]) == 4


async def test_all_planned_nodes_stay_active_without_graph_metadata(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """真值表边界：节点从未映射过且本轮没有 method → 表里不留行，也不影响其它节点。"""
    await _create_mastery_with_graph_link(memory_service, session_factory)
    unknown_node = "n7714"
    await _seed_graph_node(session_factory, node_id=unknown_node, title="未映射主题")
    plan = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        action="merge",
        expected_version=1,
        mastery_patch=MasteryPatch(understood_to_add=["新知识点"]),
    )
    await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[plan],
        graph_node_ids_by_plan=[[unknown_node]],
        # 不带 mapping_methods_by_plan：新节点无来源可继承
    )
    rows = {row["node_id"]: row for row in await _links(session_factory)}
    assert unknown_node not in rows, "无 mapping_method 的新节点不应落库（NOT NULL 约束）"
    # 计划集合以本次为准：旧节点不在集合里 → 必须被置 inactive（不是被"保护"）
    assert rows[NODE_ID]["active"] is False


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


async def test_rename_then_forget_then_restore_keeps_frontmatter_name(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """review-2 新发现 7 的端到端回归：改 name → 删除 → 恢复，注册表标题不得回退。

    恢复路径曾自己拼一份投影、title 取 ``parsed.topic_title``（v2 文档的
    ``memory_documents.topic_title`` 只在 create 时写过一次），于是"撤销删除"会把
    注册表 / prime / search 看到的标题打回旧值。
    """
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
    assert (await _index_row(session_factory))["title"] == "抛物线及其性质"  # type: ignore[index]

    deleted_version = await _document_version(session_factory)
    await memory_service.forget(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        expected_version=deleted_version,
        reason="用户要求删除",
    )
    assert await _index_row(session_factory) is None, "删除同事务移除索引行（§8.7）"

    await memory_service.restore(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        deleted_version=deleted_version,
    )
    restored = await _index_row(session_factory)
    assert restored is not None
    assert restored["title"] == "抛物线及其性质", (
        "恢复路径必须与提交路径同源投影：title 取 frontmatter name，而不是 topic_title"
    )
    assert restored["source_version"] == deleted_version + 1
    # 这两条计划没有写过 `[[link]]`，所以 related 为空（有链接的场景由 I-11② 的用例覆盖）
    assert list(restored["related_topic_keys"]) == []
    document = (await memory_service.store.read_current(user_id=USER, memory_id=MEMORY_ID)).decode(
        "utf-8"
    )
    assert parse_mastery(document).name == "抛物线及其性质"


async def test_rebuild_index_uses_registry_title_not_topic_title(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """review-2 新发现 7 第二处：index.md 的 `- name` 必须与注册表投影一致。"""
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
    # 提交路径会标 index dirty；重建走真实 render_index + _index_projection
    rebuilt = await memory_service.rebuild_index(
        user_id=USER, operation_id=await _new_operation(session_factory)
    )
    assert rebuilt["rebuilt"] is True, rebuilt
    index_doc, _stale = await memory_service.get_index(user_id=USER)
    assert index_doc is not None and index_doc.mastery_entries
    entry = index_doc.mastery_entries[0]
    assert entry.title == "抛物线的标准方程"
    assert entry.title != TOPIC_KEY, "index.md 不得再用 memory_documents.topic_title"
    assert entry.description == "圆锥曲线之一"
    assert entry.aliases == []
    assert entry.related_topic_keys == []


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


# ---------------------------------------------------------------------------
# review-3 残留：链接落后文档 >1 个版本时，"无节点提交"必须仍能推进它
# ---------------------------------------------------------------------------


async def _jump_document_version(
    session_factory: async_sessionmaker[AsyncSession], version: int
) -> None:
    """人为把活动版本跳到 ``version``，**完全不碰** ``memory_graph_links``。

    这是"版本跳跃来源"的最小替身：``migrate_markdown_schema_v2`` 升版时一行链接都
    不改（真实迁移路径另有用例覆盖），restore 与运维手工修版本同样只动文档行——
    文档版本与链接版本之间没有任何事务性绑定，任何一处 +1 都可能让链接落后一格以上。
    """
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE memory_documents SET active_version = :v "
                    "WHERE user_id = :u AND memory_id = :m"
                ),
                {"v": version, "u": USER, "m": MEMORY_ID},
            )


async def _stale_link_count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """``active=true`` 但版本不等于文档活动版本的链接数（目标恒为 0）。

    这正是 ``_load_active_links`` 的谓词（``active = true AND memory_version = 活动版本``）：
    计数 > 0 就意味着这部分映射对 KG 双写不可见。
    """
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT count(*) FROM memory_graph_links l "
                "JOIN memory_documents d ON d.user_id = l.user_id AND d.memory_id = l.memory_id "
                "WHERE l.user_id = :u AND l.memory_id = :m "
                "AND l.active = true AND l.memory_version <> d.active_version"
            ),
            {"u": USER, "m": MEMORY_ID},
        )
        return int(result.scalar_one())


async def _document_checksum(session_factory: async_sessionmaker[AsyncSession]) -> str:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT active_checksum FROM memory_documents WHERE user_id = :u AND memory_id = :m"
            ),
            {"u": USER, "m": MEMORY_ID},
        )
        return str(result.scalar_one())


async def _commit_frontmatter_only(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    expected_version: int,
    description: str = "平面内到定点与定直线距离相等的点的轨迹",
) -> int:
    """一次**不携带图谱信息**的提交（frontmatter_patch），返回新活动版本。"""
    plan = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        topic_title=TOPIC_KEY,
        action="merge",
        expected_version=expected_version,
        frontmatter_patch=FrontMatterPatch(description=description),
    )
    outcome = await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[plan],
    )
    return int(outcome.mutations[0].after_version)


async def _dual_write_for_version(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    version: int,
) -> DualWriteOutcome:
    """走真实 KG 双写入口（参数形态与 consolidation 的调用一致）。"""
    enabled = Settings(
        app_env=settings.app_env,
        memory_storage_root=settings.memory_storage_root,
        memory_kg_dual_write_enabled=True,
    )
    operation_id = await _new_operation(session_factory)
    return await after_consolidation(
        user_id=USER,
        batch_operation_id=operation_id,
        operation_id=operation_id,
        changed_topics=[
            {
                "memory_id": MEMORY_ID,
                "topic_key": TOPIC_KEY,
                "version": version,
                "checksum": await _document_checksum(session_factory),
                "keywords": [TOPIC_KEY],
                "direction": "positive",
                "strength": 0.8,
                "occurred_at": datetime(2026, 9, 12, 3, 0, tzinfo=UTC),
            }
        ],
        conflicts=[],
        settings=enabled,
        session_factory=session_factory,
        logger=logging.getLogger("test.mastery_kg_links.dual_write"),
    )


async def _overlay(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any] | None:
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT * FROM graph_user_states WHERE user_id = :u AND node_id = :n"),
            {"u": USER, "n": NODE_ID},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def test_no_graph_commit_advances_links_after_one_version_jump(
    settings: Settings,
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """探针场景回归：v1 带映射 → 跳版到 v2 → 无节点提交到 v3，链接必须跟到 v3。

    旧实现的读侧谓词是 ``memory_version == active_version - 1``：提交到 v3 时它去找
    "v2 的活动行"，表里却只有停在 v1 的那一行，于是什么都没推进——链接永久停在 v1，
    紧随其后的 KG 双写因 ``active = true AND memory_version = 3`` 不成立而
    ``skipped/no_graph_mapping``。
    """
    await _create_mastery_with_graph_link(memory_service, session_factory)
    await _jump_document_version(session_factory, 2)
    assert await _document_version(session_factory) == 2
    assert _snapshot(await _links(session_factory)) == [(NODE_ID, 1, True)]
    assert await _stale_link_count(session_factory) == 1, "跳版后链接必然落后（前提状态）"

    after_version = await _commit_frontmatter_only(
        memory_service, session_factory, expected_version=2
    )
    assert after_version == 3

    assert _snapshot(await _links(session_factory)) == [(NODE_ID, 3, True)]
    assert await _stale_link_count(session_factory) == 0

    # 紧随其后的 KG 双写必须真的拿到映射（旧实现这里恒为 skipped/no_graph_mapping）
    outcome = await _dual_write_for_version(settings, session_factory, version=after_version)
    assert outcome.status == "applied", outcome
    assert outcome.reason is None
    assert await _overlay(session_factory) is not None


async def test_no_graph_commit_advances_links_after_multi_version_jump(
    settings: Settings,
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """多级跳跃（v1 → v4）同样成立：判据与"差几版"完全无关。"""
    await _create_mastery_with_graph_link(memory_service, session_factory)
    await _jump_document_version(session_factory, 4)
    assert await _stale_link_count(session_factory) == 1

    after_version = await _commit_frontmatter_only(
        memory_service, session_factory, expected_version=4
    )
    assert after_version == 5
    assert _snapshot(await _links(session_factory)) == [(NODE_ID, 5, True)]
    assert await _stale_link_count(session_factory) == 0

    outcome = await _dual_write_for_version(settings, session_factory, version=after_version)
    assert outcome.status == "applied", outcome


async def test_no_graph_commit_never_invents_links_without_history(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """反向：该记忆**从来没有** KG 映射 → 无节点提交（含跳版后）不得凭空造映射。

    这是 I-3 情况 3 的"完全不碰"语义：放宽版本谓词不能变成"顺手建一条空映射"。
    """
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
    assert await _links(session_factory) == []

    await _jump_document_version(session_factory, 2)
    after_version = await _commit_frontmatter_only(
        memory_service, session_factory, expected_version=2
    )
    assert after_version == 3
    assert await _links(session_factory) == [], "从来没映射 → 不得凭空造映射"
    assert await _stale_link_count(session_factory) == 0


async def test_dropped_node_stays_inactive_across_version_jumps(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """反向：显式节点集合里"消失的节点"仍被置 inactive，且跳版后也不会被复活。

    放宽读侧谓词只推进 ``active=true`` 的行，因此"本次集合为准"的取消语义不受影响；
    紧随其后的无节点提交也不能把已经取消的映射拉回来。
    """
    await _create_mastery_with_graph_link(memory_service, session_factory)
    other_node = "n7731"
    await _seed_graph_node(session_factory, node_id=other_node, title=LINKED_TOPIC)
    await _jump_document_version(session_factory, 2)

    # v3：显式给出新集合 [other_node] → NODE_ID 必须被置 inactive
    plan = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        target_memory_type="mastery",
        topic_title=TOPIC_KEY,
        action="merge",
        expected_version=2,
        mastery_patch=MasteryPatch(understood_to_add=["掌握第二定义"]),
    )
    await memory_service.commit_plans(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        plans=[plan],
        graph_node_ids_by_plan=[[other_node]],
        mapping_methods_by_plan=["model_candidate"],
        mapping_confidences_by_plan=[0.7],
    )
    rows = {row["node_id"]: row for row in await _links(session_factory)}
    assert rows[NODE_ID]["active"] is False
    assert rows[other_node]["active"] is True
    assert int(rows[other_node]["memory_version"]) == 3
    assert await _stale_link_count(session_factory) == 0

    # v4：无图谱信息的提交 —— 剩下的活动映射前进，被取消的那条不得复活
    after_version = await _commit_frontmatter_only(
        memory_service, session_factory, expected_version=3
    )
    assert after_version == 4
    rows = {row["node_id"]: row for row in await _links(session_factory)}
    assert rows[NODE_ID]["active"] is False
    assert int(rows[NODE_ID]["memory_version"]) == 1, "inactive 行不该被顺手改写"
    assert rows[other_node]["active"] is True
    assert int(rows[other_node]["memory_version"]) == 4
    assert await _stale_link_count(session_factory) == 0


async def test_align_rule_promotes_the_latest_active_snapshot(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """锁住"最近快照"这条判据本身：active 行混版时只推进**最高版本**那一批。

    混版只可能来自越界写入（当前所有写侧都是集合语义整批写，active 行版本恒一致）。
    此时"最近一次写入的快照"才是可解释的映射集：更旧的活动行既不拉进映射集、也不
    被删除——宁可留一行不可见的旧行，也不能凭猜测取消一条映射（取消映射只有一个
    合法入口：显式节点集合 / forget）。
    """
    await _create_mastery_with_graph_link(memory_service, session_factory)
    old_node, new_node = "n7741", "n7742"
    await _seed_graph_node(session_factory, node_id=old_node, title="旧快照节点")
    await _seed_graph_node(session_factory, node_id=new_node, title="新快照节点")
    async with session_factory() as session:
        async with session.begin():
            await gs_repo.upsert_graph_link(
                session,
                user_id=USER,
                memory_id=MEMORY_ID,
                node_id=old_node,
                memory_version=1,
                mapping_method="exact_alias",
                mapping_confidence=0.5,
            )
            await gs_repo.upsert_graph_link(
                session,
                user_id=USER,
                memory_id=MEMORY_ID,
                node_id=new_node,
                memory_version=2,
                mapping_method="exact_alias",
                mapping_confidence=0.5,
            )
    await _jump_document_version(session_factory, 3)

    async with session_factory() as session:
        async with session.begin():
            promoted = await gs_repo.align_active_graph_links(
                session, user_id=USER, memory_id=MEMORY_ID, memory_version=3
            )
    assert promoted == [new_node], "只推进最高版本那一批（按 node_id 稳定排序）"
    rows = {row["node_id"]: row for row in await _links(session_factory)}
    assert int(rows[new_node]["memory_version"]) == 3
    assert rows[new_node]["active"] is True
    assert int(rows[old_node]["memory_version"]) == 1, "更旧的活动行保持原状"
    assert rows[old_node]["active"] is True, "不猜测、不删除：取消映射只能由调用方显式发起"
    assert int(rows[NODE_ID]["memory_version"]) == 1

    # 记忆版本**倒退**（链接比文档新）时同样对齐：链接上的版本只是"针对哪个文档版本
    # 计算"的戳记，对齐后不变量重新成立
    async with session_factory() as session:
        async with session.begin():
            await gs_repo.align_active_graph_links(
                session, user_id=USER, memory_id=MEMORY_ID, memory_version=1
            )
    rows = {row["node_id"]: row for row in await _links(session_factory)}
    assert int(rows[new_node]["memory_version"]) == 1
    assert rows[new_node]["active"] is True


# ---------------------------------------------------------------------------
# 写侧收敛：真实 migrate_markdown_schema_v2 与索引投影一起对齐链接版本
# ---------------------------------------------------------------------------


async def _seed_legacy_v1_document(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
) -> None:
    """造"v1 建档 + 已有 KG 映射"的历史状态（迁移前的真实形态）。

    v1 文档只能这样造：``commit_plans`` 现在原生渲染 v2，v1 只存在于历史数据里。
    """
    content = render_mastery(
        MasteryDocument(
            user_id=USER,
            topic_key=TOPIC_KEY,
            topic_title=TOPIC_KEY,
            version=1,
            updated_at=datetime(2026, 9, 1, tzinfo=UTC),
            schema_version=SCHEMA_VERSION_V1,
            overview=f"与 [[{LINKED_TOPIC}]] 同族的圆锥曲线",
            understood=["掌握焦点与准线"],
        )
    ).encode("utf-8")
    stored = await store.write_immutable_version(
        user_id=USER, memory_id=MEMORY_ID, version=1, content=content
    )
    await store.materialize_current(user_id=USER, memory_id=MEMORY_ID, content=content)
    async with session_factory() as session:
        async with session.begin():
            await docs_repo.upsert_document(
                session,
                user_id=USER,
                memory_id=MEMORY_ID,
                memory_type="mastery",
                topic_key=TOPIC_KEY,
                topic_title=TOPIC_KEY,
                logical_path=logical_path_for(MEMORY_ID),
            )
            await docs_repo.set_active_version(
                session,
                user_id=USER,
                memory_id=MEMORY_ID,
                active_version=1,
                active_storage_key=stored.storage_key,
                active_checksum=stored.checksum,
            )
            await gs_repo.upsert_graph_link(
                session,
                user_id=USER,
                memory_id=MEMORY_ID,
                node_id=NODE_ID,
                memory_version=1,
                mapping_method="exact_alias",
                mapping_confidence=0.9,
            )


async def _run_schema_migration(
    session_factory: async_sessionmaker[AsyncSession],
    runtime_context: MemoryRuntimeContext,
    *,
    idem_suffix: str,
) -> dict[str, Any]:
    """建 run + 关联 operation，经真实 Graph 执行迁移并返回 run detail。"""
    operation = make_operation(
        user_id=UUID(SYSTEM_MAINTENANCE_USER_ID),
        actor_type="system",
        input_kind="maintenance",
        operation_type="migrate_markdown_schema_v2",
        priority=0,
        payload=MaintenanceCommand(kind="migrate_markdown_schema_v2", batch_size=100),
    )
    await persist_operation(session_factory, operation)
    key = f"migrate-schema-v2:links-{idem_suffix}"
    async with session_factory() as session:
        async with session.begin():
            run, _created = await maintenance_repo.create_or_reuse_run(
                session,
                run_id=uuid4(),
                maintenance_type="migrate_markdown_schema_v2",
                idempotency_key=key,
            )
            await maintenance_repo.attach_operation(
                session, run_id=run["run_id"], operation_id=operation.operation_id
            )
    result = await LocalLangGraphRunner(context=runtime_context).run(operation)
    assert result.status == "succeeded", result.error
    async with session_factory() as session:
        run_row = await maintenance_repo.get_run_by_key(session, idempotency_key=key)
    assert run_row is not None
    return dict(run_row["result"] or {})


async def test_migration_aligns_graph_links_with_the_new_version(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    runtime_context: MemoryRuntimeContext,
) -> None:
    """(b) 写侧收敛：迁移在同一事务里把链接对齐到新活动版本（不留 1 版窗口）。"""
    await _seed_graph_node(session_factory)
    await _seed_legacy_v1_document(session_factory, store)

    detail = await _run_schema_migration(session_factory, runtime_context, idem_suffix="upgrade")

    assert detail["migrated"] >= 1, detail
    assert await _document_version(session_factory) == 2
    assert _snapshot(await _links(session_factory)) == [(NODE_ID, 2, True)]
    assert await _stale_link_count(session_factory) == 0


async def test_migration_repairs_stale_links_for_already_v2_document(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    runtime_context: MemoryRuntimeContext,
) -> None:
    """已是 v2 的文档（frontmatter 原生升版 / 早期迁移跑过）也会在回填时被修复。

    ``skipped_already_v2`` 分支本来是"重跑迁移即全量回填投影"的入口，链接版本同样
    在这里对齐——否则存量 stale 数据只能等下一次该主题的提交，或干脆永远修不好。
    """
    await _create_mastery_with_graph_link(memory_service, session_factory, schema_v2=True)
    await _jump_document_version(session_factory, 2)
    assert await _stale_link_count(session_factory) == 1

    detail = await _run_schema_migration(session_factory, runtime_context, idem_suffix="repair")

    assert detail["skipped_already_v2"] >= 1, detail
    assert detail["graph_links_aligned"] >= 1, detail
    assert _snapshot(await _links(session_factory)) == [(NODE_ID, 2, True)]
    assert await _stale_link_count(session_factory) == 0


# ---------------------------------------------------------------------------
# 同一根因的第二个站点：forget 的事件候选
# ---------------------------------------------------------------------------


async def test_forget_reports_graph_candidates_after_version_jump(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """跳版后 ``forget`` 的 ``memory.deleted`` 事件仍必须带上图谱候选。

    候选列表原用严格的"版本 = ``deleted_version``"谓词：链接停在更旧版本时读到的
    是空列表，``_deliver_summary_projection``（§14.4）对空候选直接幂等返回、不建
    "删除后重算"投影，该节点的 overlay 就一直留着这条已删记忆的贡献。删除映射本身
    （无条件把全部行置 inactive）一字未动。
    """
    await _create_mastery_with_graph_link(memory_service, session_factory)
    await _jump_document_version(session_factory, 2)
    assert await _stale_link_count(session_factory) == 1

    outcome = await memory_service.forget(
        operation_id=await _new_operation(session_factory),
        user_id=USER,
        actor_type="user",
        mutation_id=uuid4(),
        memory_id=MEMORY_ID,
        expected_version=2,
        reason="review-3 同类站点回归",
    )
    assert outcome.after_version is None

    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT payload FROM memory_outbox WHERE user_id = :u "
                "AND event_type = 'memory.deleted' ORDER BY created_at DESC LIMIT 1"
            ),
            {"u": USER},
        )
        payload = dict(result.mappings().one()["payload"])
    assert payload["deleted_version"] == 2
    assert payload["graph_projection_candidates"] == [NODE_ID]
    # 删除映射的语义不变：所有行都 inactive（不是"只把版本推上去"）
    rows = {row["node_id"]: row for row in await _links(session_factory)}
    assert rows[NODE_ID]["active"] is False
