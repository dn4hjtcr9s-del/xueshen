"""MemoryService 提交协议集成测试（§23.3）：真实 PostgreSQL + 临时文件系统。

覆盖：多文档原子提交、冲突整体回滚、mutation_id 重放、expected_version 冲突、
forget→tombstone→restore、tombstone 过期拒绝恢复、Outbox 同事务、
幂等键冲突检测、index dirty 与重建。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import (
    CommitMutationPlan,
    LearnerPatch,
    MasteryPatch,
)
from backend.memory.contracts.errors import (
    MemoryRestoreExpiredError,
    MemoryVersionConflictError,
)
from backend.memory.services.memory_service import MemoryService

USER = UUID("00000000-0000-4000-8000-000000000001")


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


async def _doc_row(
    session_factory: async_sessionmaker[AsyncSession], memory_id: str
) -> dict | None:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT memory_type, active_version, deleted_version, deleted_at,"
                " tombstone_until, index_dirty_at"
                " FROM memory_documents WHERE user_id = :u AND memory_id = :m"
            ),
            {"u": USER, "m": memory_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def _count(session_factory: async_sessionmaker[AsyncSession], table: str) -> int:
    async with session_factory() as session:
        result = await session.execute(text(f"SELECT count(*) FROM {table}"))
        return int(result.scalar_one())


def _learner_create() -> CommitMutationPlan:
    return CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id="learner",
        target_memory_type="learner",
        action="create",
        learner_patch=LearnerPatch(goals_to_add=["期中考试达到 90 分"]),
    )


def _mastery_create(topic_key: str, title: str) -> CommitMutationPlan:
    return CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id=f"mastery:{topic_key}",
        target_memory_type="mastery",
        topic_title=title,
        action="create",
        mastery_patch=MasteryPatch(
            overview=f"{title}的基本掌握情况",
            understood_to_add=["基本概念"],
        ),
    )


async def _commit(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    plans: list[CommitMutationPlan],
    operation_id: UUID | None = None,
):
    op_id = operation_id or uuid4()
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, op_id)
    return await memory_service.commit_plans(
        operation_id=op_id, user_id=USER, actor_type="user", plans=plans
    )


async def test_multi_document_commit_atomic(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    plans = [
        _learner_create(),
        _mastery_create("quadratic", "二次函数"),
        _mastery_create("derivative", "导数"),
    ]
    outcome = await _commit(memory_service, session_factory, plans)

    assert len(outcome.mutations) == 3
    assert all(m.after_version == 1 for m in outcome.mutations)
    assert not outcome.replayed

    # 活动版本全部就位，内容可读
    learner = await memory_service.get_learner(user_id=USER)
    assert learner is not None and learner.version == 1
    mastery = await memory_service.get_mastery(user_id=USER, topic_key="quadratic")
    assert mastery is not None and mastery.topic_title == "二次函数"

    # commit 行与 Outbox 事件同事务写入（§23.3：Outbox 与 commit 同事务）
    assert await _count(session_factory, "memory_commits") == 3
    assert await _count(session_factory, "memory_outbox") >= 3

    # index dirty 已标记
    index_row = await _doc_row(session_factory, "index")
    assert index_row is not None and index_row["index_dirty_at"] is not None


async def test_conflict_rolls_back_all_documents(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _commit(memory_service, session_factory, [_mastery_create("quadratic", "二次函数")])

    bad_plan = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id="mastery:quadratic",
        target_memory_type="mastery",
        action="merge",
        expected_version=99,  # 陈旧令牌
        mastery_patch=MasteryPatch(understood_to_add=["求根公式"]),
    )
    with pytest.raises(MemoryVersionConflictError):
        await _commit(memory_service, session_factory, [_learner_create(), bad_plan])

    # 同一事务中的 learner 也不得产生半个活动版本
    assert await memory_service.get_learner(user_id=USER) is None
    assert await _count(session_factory, "memory_commits") == 1
    assert await _count(session_factory, "memory_outbox") == 1
    row = await _doc_row(session_factory, "mastery:quadratic")
    assert row is not None and row["active_version"] == 1


async def test_mutation_replay_returns_original_commit(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    plan = _mastery_create("quadratic", "二次函数")
    first = await _commit(memory_service, session_factory, [plan])
    assert first.mutations[0].after_version == 1

    # 同一 mutation_id 重放（新 operation_id）：返回原 commit，不产生新版本
    replay = await _commit(memory_service, session_factory, [plan])
    assert replay.replayed is True
    assert replay.mutations[0].after_version == 1
    assert await _count(session_factory, "memory_commits") == 1
    row = await _doc_row(session_factory, "mastery:quadratic")
    assert row is not None and row["active_version"] == 1


async def test_expected_version_conflict(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _commit(memory_service, session_factory, [_mastery_create("derivative", "导数")])
    merge = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id="mastery:derivative",
        target_memory_type="mastery",
        action="merge",
        expected_version=1,
        mastery_patch=MasteryPatch(understood_to_add=["链式法则"]),
    )
    outcome = await _commit(memory_service, session_factory, [merge])
    assert outcome.mutations[0].after_version == 2

    stale = merge.model_copy(update={"mutation_id": uuid4()})
    with pytest.raises(MemoryVersionConflictError):
        await _commit(memory_service, session_factory, [stale])


async def _commit_marker(
    session_factory: async_sessionmaker[AsyncSession], operation_id: UUID
) -> object:
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT commit_started_at FROM memory_operations WHERE operation_id = :o"),
            {"o": operation_id},
        )
        return result.scalar_one()


async def test_commit_marker_cleared_after_success(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """§11.6（裁决 2026-08-11）：commit 事务成功后清除 commit_started_at。"""
    op_id = uuid4()
    outcome = await _commit(
        memory_service, session_factory, [_learner_create()], operation_id=op_id
    )
    assert not outcome.replayed
    assert await _commit_marker(session_factory, op_id) is None


async def test_commit_marker_cleared_after_rollback(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """§11.6（裁决 2026-08-11）：提交事务回滚后同样清除 commit_started_at。"""
    await _commit(memory_service, session_factory, [_mastery_create("quadratic", "二次函数")])
    bad_plan = CommitMutationPlan(
        mutation_id=uuid4(),
        memory_id="mastery:quadratic",
        target_memory_type="mastery",
        action="merge",
        expected_version=99,  # 陈旧令牌 → 整事务回滚
        mastery_patch=MasteryPatch(understood_to_add=["求根公式"]),
    )
    op_id = uuid4()
    with pytest.raises(MemoryVersionConflictError):
        await _commit(memory_service, session_factory, [bad_plan], operation_id=op_id)
    assert await _commit_marker(session_factory, op_id) is None


async def test_forget_tombstone_then_restore_new_version(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _commit(memory_service, session_factory, [_mastery_create("quadratic", "二次函数")])

    forget_op = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, forget_op)
    forgotten = await memory_service.forget(
        operation_id=forget_op,
        user_id=USER,
        actor_type="user",
        mutation_id=uuid4(),
        memory_id="mastery:quadratic",
        expected_version=1,
        reason="用户主动删除",
    )
    assert forgotten.action == "forget"

    # 活动指针置空、tombstone 记录删除版本
    row = await _doc_row(session_factory, "mastery:quadratic")
    assert row is not None
    assert row["active_version"] is None
    assert row["deleted_at"] is not None
    assert row["deleted_version"] == 1
    assert row["tombstone_until"] is not None
    assert await memory_service.get_mastery(user_id=USER, topic_key="quadratic") is None

    # 恢复为递增新版本（max_version + 1 = 2），内容来自删除版本
    restore_op = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, restore_op)
    restored = await memory_service.restore(
        operation_id=restore_op,
        user_id=USER,
        actor_type="user",
        mutation_id=uuid4(),
        memory_id="mastery:quadratic",
        deleted_version=1,
    )
    assert restored.after_version == 2
    row = await _doc_row(session_factory, "mastery:quadratic")
    assert row is not None and row["active_version"] == 2 and row["deleted_at"] is None
    mastery = await memory_service.get_mastery(user_id=USER, topic_key="quadratic")
    assert mastery is not None and mastery.topic_title == "二次函数"


async def test_restore_rejected_after_tombstone_expiry(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _commit(memory_service, session_factory, [_mastery_create("quadratic", "二次函数")])
    forget_op = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, forget_op)
    await memory_service.forget(
        operation_id=forget_op,
        user_id=USER,
        actor_type="user",
        mutation_id=uuid4(),
        memory_id="mastery:quadratic",
        expected_version=1,
        reason=None,
    )

    # 模拟 tombstone 过期
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE memory_documents SET tombstone_until = :past"
                    " WHERE user_id = :u AND memory_id = 'mastery:quadratic'"
                ),
                {"past": datetime.now(UTC) - timedelta(days=1), "u": USER},
            )

    restore_op = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, restore_op)
    with pytest.raises(MemoryRestoreExpiredError):
        await memory_service.restore(
            operation_id=restore_op,
            user_id=USER,
            actor_type="user",
            mutation_id=uuid4(),
            memory_id="mastery:quadratic",
            deleted_version=1,
        )


async def test_index_dirty_and_rebuild(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _commit(
        memory_service,
        session_factory,
        [_learner_create(), _mastery_create("quadratic", "二次函数")],
    )

    async def _rebuild() -> dict:
        op_id = uuid4()
        async with session_factory() as session:
            async with session.begin():
                await _insert_operation(session, op_id)
        return await memory_service.rebuild_index(user_id=USER, operation_id=op_id)

    result = await _rebuild()
    assert result["rebuilt"] is True

    index_row = await _doc_row(session_factory, "index")
    assert index_row is not None
    assert index_row["index_dirty_at"] is None
    assert index_row["active_version"] == 1

    # 无 dirty 标记时重建为空操作
    again = await _rebuild()
    assert again["rebuilt"] is False
