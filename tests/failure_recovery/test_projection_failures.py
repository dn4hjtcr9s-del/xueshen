"""失败恢复测试（§23.4）：Graph projection 提交前后故障注入。

注入点 7：projection 事务提交前失败 → 重试后成功；重复执行同一 projection
不造成重复业务效果（确定性评估 + 版本守卫，叠加态不产生重复审计/通知）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import (
    CommitMutationPlan,
    GraphProjectionEvidence,
    MasteryPatch,
    ProjectSummaryToGraphCommand,
)
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.services.graph_state_service import KnowledgeGraphStateService
from backend.memory.services.memory_service import MemoryService
from tests.integration.graph_helpers import make_operation, persist_operation

USER = UUID("00000000-0000-4000-8000-0000000000f7")


async def _create_mastery(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """提交 mastery 活动版本 1，作为 projection 的来源。"""
    op_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
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
                    "operation_id": str(op_id),
                    "user_id": str(USER),
                    "idem": f"idem-{uuid4().hex[:8]}",
                    "idem_hash": "0" * 64,
                    "trace_id": uuid4().hex + uuid4().hex,
                },
            )
    await memory_service.commit_plans(
        operation_id=op_id,
        user_id=USER,
        actor_type="user",
        plans=[
            CommitMutationPlan(
                mutation_id=uuid4(),
                memory_id="mastery:jixian",
                target_memory_type="mastery",
                topic_title="极限",
                action="create",
                mastery_patch=MasteryPatch(overview="基本掌握", understood_to_add=["定义"]),
            )
        ],
    )


def _projection_operation(node_id: str) -> Any:
    return make_operation(
        user_id=USER,
        actor_type="summary_projection",
        input_kind="projection",
        operation_type="project_summary_to_graph",
        priority=50,
        payload=ProjectSummaryToGraphCommand(
            trigger_event_type="memory.changed",
            projection_action="apply_active_version",
            source_memory_id="mastery:jixian",
            source_version=1,
            node_id=node_id,
            mapping_method="exact_alias",
            mapping_confidence=0.95,
            evidence=[
                GraphProjectionEvidence(
                    evidence_ref="conv:t1:m1",
                    direction="positive",
                    strength=0.9,
                    occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
                ),
                GraphProjectionEvidence(
                    evidence_ref="conv:t1:m2",
                    direction="positive",
                    strength=0.85,
                    occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
                ),
            ],
        ),
    )


async def _overlay_row(
    session_factory: async_sessionmaker[AsyncSession], node_id: str
) -> dict[str, Any] | None:
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT status, version FROM graph_user_states"
                        " WHERE user_id = :u AND node_id = :n"
                    ),
                    {"u": USER, "n": node_id},
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


async def _audit_count(session_factory: async_sessionmaker[AsyncSession], node_id: str) -> int:
    async with session_factory() as session:
        row = await session.execute(
            text("SELECT COUNT(*) FROM graph_state_audit WHERE user_id = :u AND node_id = :n"),
            {"u": USER, "n": node_id},
        )
        return int(row.scalar_one())


async def test_projection_commit_failure_retry_then_idempotent(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        node_id = str(
            (
                await session.execute(text("SELECT node_id FROM knowledge_graph_nodes LIMIT 1"))
            ).scalar_one()
        )
    await _create_mastery(memory_service, session_factory)

    # 提交前失败：projection 事务整体注入故障 → 无 Overlay、无审计
    async def broken_apply(self: Any, **kwargs: Any) -> Any:
        raise RuntimeError("injected: projection commit failure")

    monkeypatch.setattr(KnowledgeGraphStateService, "apply_projection", broken_apply)
    operation = _projection_operation(node_id)
    await persist_operation(session_factory, operation)
    with pytest.raises(RuntimeError, match="injected"):
        await runner.run(operation)
    assert await _overlay_row(session_factory, node_id) is None
    assert await _audit_count(session_factory, node_id) == 0

    # 恢复后重试：projection 成功，两条 positive 证据 → proficient
    monkeypatch.undo()
    result = await runner.run(operation)
    assert result.status == "succeeded"
    overlay = await _overlay_row(session_factory, node_id)
    assert overlay is not None and overlay["status"] == "proficient"
    audit_after_success = await _audit_count(session_factory, node_id)
    assert audit_after_success >= 1

    # 同一 projection 重复执行：状态不变 → 无新审计、无重复效果
    result2 = await runner.run(operation)
    assert result2.status == "succeeded"
    overlay2 = await _overlay_row(session_factory, node_id)
    assert overlay2 is not None and overlay2["version"] == overlay["version"]
    assert await _audit_count(session_factory, node_id) == audit_after_success
