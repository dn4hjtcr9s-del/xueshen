"""KG 双路更新集成测试（memory-rebuild §3.5① / §5.9；决议 D 组）。

只有真实 PostgreSQL 才能证明的部分：

- **幂等重试不重复应用**：同一 ``batch_operation_id`` 重放两次，Overlay 版本、
  审计行数、Outbox 事件数与投影 operation_id 都不变；
- **KG 侧失败时长期记忆不受影响**：KG 写入抛错被吞成 ``status="failed"``，
  长期记忆文档/commits 原样，读路径仍能返回长期记忆值；
- **无映射优雅跳过**：``mastery`` 主题没有 ``memory_graph_links`` 行时
  ``skipped``/``no_graph_mapping``，不建 Overlay、不报错。

运行：`scripts/ci-local.sh backend-integration`，或显式注入 memory_test：
    DATABASE_URL=postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test \
    uv run pytest tests/integration/test_kg_dual_write.py -q
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.evidence import ConversationEvidence
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.services import kg_dual_write
from backend.memory.services.kg_dual_write import after_consolidation
from backend.memory.services.memory_service import MemoryService
from backend.settings import Settings
from tests.integration.graph_helpers import make_operation, persist_operation

USER = UUID("00000000-0000-4000-8000-0000000000c1")
BATCH_ID = UUID("00000000-0000-4000-8000-0000000000c2")
NODE_ID = "n9101"
TOPIC_KEY = "椭圆"
TOPIC = "椭圆"
CHECKSUM = "d" * 64
NOW = datetime(2026, 9, 12, 3, 30, tzinfo=UTC)
LOGGER = logging.getLogger("test.kg_dual_write.integration")


async def _seed_batch_operation(factory: async_sessionmaker[AsyncSession]) -> None:
    """把批次 operation 落库：``graph_state_audit.operation_id`` 是它的外键。"""
    operation = make_operation(
        user_id=USER,
        actor_type="system",
        input_kind="maintenance",
        operation_type="rebuild_index",
        priority=30,
        payload=ConversationEvidence(thread_id="t1", message_ids=["m1"], trigger="turn_boundary"),
    )
    batch_operation = MemoryOperation.model_validate(
        {**operation.model_dump(), "operation_id": BATCH_ID}
    )
    await persist_operation(factory, batch_operation)


async def _seed_graph_node(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO knowledge_graph_nodes (node_id, title, source_file, "
                    "source_checksum) VALUES (:n, :t, 'test.md', :ck) "
                    "ON CONFLICT (node_id) DO NOTHING"
                ),
                {"n": NODE_ID, "t": TOPIC, "ck": CHECKSUM},
            )


async def _commit_mastery(
    factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    *,
    version: int,
    document_checksum: str,
    topic_key: str = TOPIC_KEY,
) -> None:
    """把一条 mastery 文档推到指定活动版本（含 commit 与 link 语义所需的 operation）。"""
    operation = make_operation(
        user_id=USER,
        actor_type="conversation_agent",
        input_kind="evidence",
        operation_type="conversation_evidence",
        priority=50,
        payload=ConversationEvidence(thread_id="t1", message_ids=["m1"], trigger="turn_boundary"),
    )
    await persist_operation(factory, operation)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO memory_documents (user_id, memory_id, memory_type, topic_key,"
                    " topic_title, logical_path, active_version) "
                    "VALUES (:u, :m, 'mastery', :k, :title, :p, :v) "
                    "ON CONFLICT (user_id, memory_id) DO UPDATE "
                    "SET active_version = EXCLUDED.active_version"
                ),
                {
                    "u": USER,
                    "m": f"mastery:{topic_key}",
                    "k": topic_key,
                    "title": TOPIC,
                    "p": f"mastery/{topic_key}.md",
                    "v": version,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO memory_commits (commit_id, mutation_id, operation_id, user_id,"
                    " memory_id, action, before_version, after_version, storage_key, checksum,"
                    " actor_type, evidence_refs, commit_payload) VALUES (:cid, :mid, :oid, :u,"
                    " :m, 'create', NULL, :v, :sk, :ck, 'conversation_agent',"
                    " CAST(:refs AS jsonb), CAST(:payload AS jsonb))"
                ),
                {
                    "cid": uuid4(),
                    "mid": uuid4(),
                    "oid": operation.operation_id,
                    "u": USER,
                    "m": f"mastery:{topic_key}",
                    "v": version,
                    "sk": f"users/{USER}/mastery/{topic_key}/versions/{version}.md",
                    "ck": document_checksum,
                    "refs": '["conv:t1:m1"]',
                    "payload": '{"reason": "integration-seed"}',
                },
            )
            await session.execute(
                text(
                    "INSERT INTO memory_graph_links (user_id, memory_id, node_id, memory_version,"
                    " mapping_method, mapping_confidence, active) "
                    "VALUES (:u, :m, :n, :v, 'exact_alias', 0.9, true) "
                    "ON CONFLICT (user_id, memory_id, node_id) DO UPDATE "
                    "SET memory_version = EXCLUDED.memory_version, active = true"
                ),
                {"u": USER, "m": f"mastery:{topic_key}", "n": NODE_ID, "v": version},
            )


def _changed_topics(
    *,
    version: int = 1,
    direction: str = "positive",
    strength: float = 0.8,
    memory_id: str | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "memory_id": memory_id or f"mastery:{TOPIC_KEY}",
            "topic_key": TOPIC_KEY,
            "version": version,
            "checksum": CHECKSUM,
            "keywords": [TOPIC],
            "direction": direction,
            "strength": strength,
            "occurred_at": NOW,
        }
    ]


async def _count(factory: async_sessionmaker[AsyncSession], table: str, *, user_id: UUID) -> int:
    async with factory() as session:
        result = await session.execute(
            text(f"SELECT count(*) FROM {table} WHERE user_id = :u"), {"u": user_id}
        )
        return int(result.scalar_one())


async def _overlay(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any] | None:
    async with factory() as session:
        result = await session.execute(
            text("SELECT * FROM graph_user_states WHERE user_id = :u AND node_id = :n"),
            {"u": USER, "n": NODE_ID},
        )
        row = result.mappings().first()
        return dict(row) if row else None


def _enabled_settings(settings: Settings) -> Settings:
    return Settings(
        app_env=settings.app_env,
        memory_storage_root=settings.memory_storage_root,
        memory_kg_dual_write_enabled=True,
    )


async def test_retry_does_not_reapply_projection(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
) -> None:
    await _seed_graph_node(session_factory)
    await _seed_batch_operation(session_factory)
    await _commit_mastery(session_factory, memory_service, version=1, document_checksum=CHECKSUM)
    enabled = _enabled_settings(settings)

    first = await after_consolidation(
        user_id=USER,
        batch_operation_id=BATCH_ID,
        operation_id=BATCH_ID,
        changed_topics=_changed_topics(),
        conflicts=[],
        settings=enabled,
        session_factory=session_factory,
        logger=LOGGER,
    )
    assert first.status == "applied"
    assert len(first.projection_operation_ids) == 1
    applied_id = first.projection_operation_ids[0]

    overlay_after_first = await _overlay(session_factory)
    assert overlay_after_first is not None
    assert overlay_after_first["status"] == "learning"  # 单条 positive 不构成 proficient
    assert overlay_after_first["source_memory_id"] == f"mastery:{TOPIC_KEY}"
    assert overlay_after_first["source_memory_version"] == 1
    assert overlay_after_first["version"] == 1
    audits_after_first = await _count(session_factory, "graph_state_audit", user_id=USER)
    outbox_after_first = await _count(session_factory, "memory_outbox", user_id=USER)

    # 幂等重试：同一批次、同一版本、同一节点
    second = await after_consolidation(
        user_id=USER,
        batch_operation_id=BATCH_ID,
        operation_id=BATCH_ID,
        changed_topics=_changed_topics(),
        conflicts=[],
        settings=enabled,
        session_factory=session_factory,
        logger=LOGGER,
    )
    assert second.status == "skipped"
    assert second.reason == "no_effective_change"
    assert second.projection_operation_ids == []

    overlay_after_second = await _overlay(session_factory)
    assert overlay_after_second is not None
    assert overlay_after_second["version"] == 1  # 没有重复应用
    assert await _count(session_factory, "graph_state_audit", user_id=USER) == audits_after_first
    assert await _count(session_factory, "memory_outbox", user_id=USER) == outbox_after_first
    async with session_factory() as session:
        # 审计行挂在真实存在的批次 operation 上（外键约束要求），批次 = 回溯锚点
        result = await session.execute(
            text("SELECT operation_id FROM graph_state_audit WHERE user_id = :u"), {"u": USER}
        )
        assert {str(row[0]) for row in result.all()} == {str(BATCH_ID)}
        # 确定性幂等键落在 memory_operations 的终态追溯锚上
        anchors = await session.execute(
            text(
                "SELECT operation_id, status, idempotency_key FROM memory_operations "
                "WHERE actor_type = 'summary_projection' AND user_id = :u"
            ),
            {"u": USER},
        )
        rows = anchors.mappings().all()
    assert len(rows) == 1
    assert str(rows[0]["operation_id"]) == str(applied_id)
    assert rows[0]["status"] == "cancelled"
    assert rows[0]["idempotency_key"] == (f"kg-dual-write:{BATCH_ID}:mastery:{TOPIC_KEY}:1:n9101")


async def test_no_graph_mapping_skips_without_error(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
) -> None:
    await _seed_graph_node(session_factory)
    # 故意不建 memory_graph_links：mastery 主题与 KG 节点之间没有映射
    await _commit_mastery(session_factory, memory_service, version=1, document_checksum=CHECKSUM)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE memory_graph_links SET active = false WHERE user_id = :u"),
                {"u": USER},
            )

    outcome = await after_consolidation(
        user_id=USER,
        batch_operation_id=uuid4(),
        operation_id=BATCH_ID,
        changed_topics=_changed_topics(),
        conflicts=[],
        settings=_enabled_settings(settings),
        session_factory=session_factory,
        logger=LOGGER,
    )
    assert outcome.status == "skipped"
    assert outcome.reason == "no_graph_mapping"
    assert await _overlay(session_factory) is None
    assert await _count(session_factory, "graph_state_audit", user_id=USER) == 0


async def test_kg_failure_leaves_long_term_memory_intact(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_graph_node(session_factory)
    await _commit_mastery(session_factory, memory_service, version=1, document_checksum=CHECKSUM)
    commits_before = await _count(session_factory, "memory_commits", user_id=USER)

    class _ExplodingService:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def apply_projection(self, **kwargs: Any) -> Any:
            raise RuntimeError("KG 侧写库爆炸")

    monkeypatch.setattr(kg_dual_write, "KnowledgeGraphStateService", _ExplodingService)

    outcome = await after_consolidation(
        user_id=USER,
        batch_operation_id=uuid4(),
        operation_id=BATCH_ID,
        changed_topics=_changed_topics(),
        conflicts=[{"memory_ids": [f"mastery:{TOPIC_KEY}"], "description": "并列冲突"}],
        settings=_enabled_settings(settings),
        session_factory=session_factory,
        logger=LOGGER,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "kg_projection_failed"
    # 长期记忆侧不受影响：文档与 commits 原样
    assert await _count(session_factory, "memory_commits", user_id=USER) == commits_before
    assert await _overlay(session_factory) is None
    # 冲突标注仍透传给调用方（读路径可以继续用）
    assert outcome.conflicted_memory_ids == [f"mastery:{TOPIC_KEY}"]


# ---------------------------------------------------------------------------
# 读路径：LearningContextService.build 的双源协调（§3.5②）
# ---------------------------------------------------------------------------


async def test_context_build_reports_dual_source_conflict(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
) -> None:
    """记忆侧仍列困难 vs KG 侧熟练（同版本并列）→ 输出带 conflict 标记且两路值都在。"""
    from backend.memory.contracts.commands import CommitMutationPlan, MasteryPatch
    from backend.memory.contracts.context import LearningContextRequest
    from backend.memory.contracts.evidence import ConversationEvidence
    from backend.memory.services.context_service import LearningContextService

    await _seed_graph_node(session_factory)
    await _seed_batch_operation(session_factory)
    operation = make_operation(
        user_id=USER,
        actor_type="system",
        input_kind="evidence",
        operation_type="conversation_evidence",
        priority=50,
        payload=ConversationEvidence(thread_id="t1", message_ids=["m1"], trigger="turn_boundary"),
    )
    await persist_operation(session_factory, operation)
    # 长期记忆侧：列着"仍有困难"（difficulties 非空）
    await memory_service.commit_plans(
        operation_id=operation.operation_id,
        user_id=USER,
        actor_type="system",
        plans=[
            CommitMutationPlan(
                mutation_id=uuid4(),
                memory_id=f"mastery:{TOPIC_KEY}",
                target_memory_type="mastery",
                topic_title=TOPIC,
                action="create",
                mastery_patch=MasteryPatch(
                    overview="整体掌握",
                    understood_to_add=["定义"],
                    difficulties_to_add=["离心率计算"],
                ),
            )
        ],
    )
    mastery = await memory_service.get_mastery(user_id=USER, topic_key=TOPIC_KEY)
    assert mastery is not None and mastery.difficulties
    version = mastery.version

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO memory_graph_links (user_id, memory_id, node_id, memory_version,"
                    " mapping_method, mapping_confidence, active) "
                    "VALUES (:u, :m, :n, :v, 'exact_alias', 0.9, true)"
                ),
                {"u": USER, "m": f"mastery:{TOPIC_KEY}", "n": NODE_ID, "v": version},
            )
    # KG 侧由两条独立正向证据评估成 proficient（与记忆侧"仍有困难"结论不一致）
    base_topic = _changed_topics(version=version, direction="positive", strength=0.9)[0]
    outcome = await after_consolidation(
        user_id=USER,
        batch_operation_id=BATCH_ID,
        operation_id=BATCH_ID,
        changed_topics=[
            {**base_topic, "occurred_at": NOW},
            {**base_topic, "occurred_at": NOW.replace(minute=31)},
        ],
        conflicts=[],
        settings=_enabled_settings(settings),
        session_factory=session_factory,
        logger=LOGGER,
    )
    assert outcome.status == "applied"
    # 两条同时刻证据被去重成一次投影 → overlay 落到 proficient
    overlay = await _overlay(session_factory)
    assert overlay is not None and overlay["status"] == "proficient"
    service = LearningContextService(
        settings=settings, session_factory=session_factory, memory_service=memory_service
    )
    context = await service.build(
        user_id=USER, request=LearningContextRequest(query=TOPIC, topic_keys=[TOPIC_KEY])
    )
    assert context.graph_states, "图谱注入段不应为空"
    state = context.graph_states[0]
    assert state.node_id == NODE_ID
    # 并列（同版本/同刻）时的内部裁定是**长期记忆侧优先**（决议 D 只规定
    # "更新鲜者优先 + 并列标注"，没规定并列谁赢），并且必须显式标出冲突。
    assert state.status == "learning"
    assert state.conflict is True
    assert state.conflict_reason is not None and "tie" in state.conflict_reason
    assert "DUAL_SOURCE_CONFLICT" in state.reason_codes
    # 两路都出现在输出里：冲突标记 + 双方版本 + 双方时间
    assert state.memory_version == version
    assert state.kg_projected_version == version
    assert state.conflict is True
    assert state.conflict_reason == "version_tie:memory=learning,kg=proficient,memory_preferred"
    assert "DUAL_SOURCE_CONFLICT" in state.reason_codes
    assert state.memory_updated_at is not None
    assert state.kg_updated_at is not None
