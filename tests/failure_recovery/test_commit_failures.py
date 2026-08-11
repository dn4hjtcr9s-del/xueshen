"""失败恢复测试（§23.4）：Markdown 写入与数据库事务故障注入。

注入点（本文件覆盖 1–4）：
1. 写临时 Markdown 前
2. 写不可变版本后、事务前
3. 事务中
4. 事务提交后、current 物化前

验收：不丢任务、不重复提交、不产生半个活动版本、重试（同一 mutation_id）
不造成重复业务效果。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import CommitMutationPlan, LearnerPatch, MasteryPatch
from backend.memory.persistence import outbox as outbox_repo
from backend.memory.services.memory_service import MemoryService
from backend.memory.storage.base import StoredVersion
from backend.memory.storage.local_markdown import LocalMarkdownStore

USER = UUID("00000000-0000-4000-8000-0000000000f1")


async def _insert_operation(session: AsyncSession, operation_id: UUID) -> None:
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


def _plans() -> list[CommitMutationPlan]:
    return [
        CommitMutationPlan(
            mutation_id=uuid4(),
            memory_id="learner",
            target_memory_type="learner",
            action="create",
            learner_patch=LearnerPatch(goals_to_add=["期中考试达到 90 分"]),
        ),
        CommitMutationPlan(
            mutation_id=uuid4(),
            memory_id="mastery:yici-hanshu",
            target_memory_type="mastery",
            topic_title="一次函数",
            action="create",
            mastery_patch=MasteryPatch(overview="基本掌握", understood_to_add=["定义"]),
        ),
    ]


async def _commit(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    plans: list[CommitMutationPlan],
    operation_id: UUID,
):
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, operation_id)
    return await memory_service.commit_plans(
        operation_id=operation_id, user_id=USER, actor_type="user", plans=plans
    )


async def _counts(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    tables = ("memory_documents", "memory_commits", "memory_outbox", "memory_index_entries")
    result: dict[str, int] = {}
    async with session_factory() as session:
        for table in tables:
            row = await session.execute(text(f"SELECT count(*) FROM {table}"))
            result[table] = int(row.scalar_one())
    return result


async def test_fail_before_temp_markdown_write_no_partial_state(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    monkeypatch: Any,
) -> None:
    """注入点 1：写临时 Markdown 前失败 → 无任何副作用；重试同一 mutation 只提交一次。"""
    plans = _plans()

    async def broken_write(**kwargs: Any) -> StoredVersion:
        raise OSError("injected: temp write failure")

    monkeypatch.setattr(store, "write_immutable_version", broken_write)
    with pytest.raises(OSError, match="injected"):
        await _commit(memory_service, session_factory, plans, uuid4())

    counts = await _counts(session_factory)
    assert counts == {table: 0 for table in counts}
    # 版本文件不得残留半个写入
    for memory_id in ("learner", "mastery:yici-hanshu"):
        orphans = await store.list_orphan_versions(
            user_id=USER, memory_id=memory_id, referenced_checksums=set()
        )
        assert orphans == []

    monkeypatch.undo()
    outcome = await _commit(memory_service, session_factory, plans, uuid4())
    assert len(outcome.mutations) == 2
    assert (await _counts(session_factory))["memory_commits"] == 2


async def test_fail_after_version_write_before_transaction_leaves_only_orphans(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    monkeypatch: Any,
) -> None:
    """注入点 2：版本写入后、事务前失败 → 只留下孤立版本文件，DB 无指针。"""
    plans = _plans()

    async def broken_mark(operation_id: UUID) -> None:
        raise RuntimeError("injected: pre-transaction failure")

    monkeypatch.setattr(memory_service, "_mark_commit_started", broken_mark)
    with pytest.raises(RuntimeError, match="injected"):
        await _commit(memory_service, session_factory, plans, uuid4())

    counts = await _counts(session_factory)
    assert counts == {table: 0 for table in counts}
    # 不可变版本已落盘但无数据库引用 → 全部判定为孤立版本，可被维护任务清理
    orphans: list[str] = []
    for memory_id in ("learner", "mastery:yici-hanshu"):
        orphans += await store.list_orphan_versions(
            user_id=USER, memory_id=memory_id, referenced_checksums=set()
        )
    assert len(orphans) == 2

    monkeypatch.undo()
    outcome = await _commit(memory_service, session_factory, plans, uuid4())
    assert len(outcome.mutations) == 2
    # 重试渲染的时间戳不同 → 失败尝试留下的版本文件保持孤立（维护任务可清理），
    # 新提交的活动版本不在孤立列表中
    async with session_factory() as session:
        rows = await session.execute(
            text("SELECT memory_id, active_checksum FROM memory_documents WHERE user_id = :u"),
            {"u": USER},
        )
        checksums = {str(r[0]): {str(r[1])} for r in rows.all() if r[1]}
    remaining: list[str] = []
    for memory_id in ("learner", "mastery:yici-hanshu"):
        remaining += await store.list_orphan_versions(
            user_id=USER,
            memory_id=memory_id,
            referenced_checksums=checksums.get(memory_id, set()),
        )
    assert sorted(remaining) == sorted(orphans)


async def test_fail_inside_transaction_rolls_back_all_documents(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """注入点 3：事务中（Outbox 事件写入）失败 → 整体回滚，不出现半个活动版本。"""
    plans = _plans()

    async def broken_insert(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected: outbox insert failure")

    monkeypatch.setattr(outbox_repo, "insert_event", broken_insert)
    with pytest.raises(RuntimeError, match="injected"):
        await _commit(memory_service, session_factory, plans, uuid4())

    counts = await _counts(session_factory)
    # commit 与 outbox 同事务：Outbox 失败则 commit 行与活动指针一并回滚
    assert counts == {table: 0 for table in counts}
    assert await memory_service.get_learner(user_id=USER) is None
    assert await memory_service.get_mastery(user_id=USER, topic_key="yici-hanshu") is None

    monkeypatch.undo()
    outcome = await _commit(memory_service, session_factory, plans, uuid4())
    assert len(outcome.mutations) == 2
    counts = await _counts(session_factory)
    assert counts["memory_commits"] == 2
    # learner.updated + memory.changed 两个事件，不重复
    assert counts["memory_outbox"] == 2


async def test_fail_after_commit_before_materialize_reads_still_correct(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    settings: Any,
) -> None:
    """注入点 4：事务提交后、current 物化前失败 → 读取仍返回正确版本，可修复。"""
    plans = _plans()
    original = store.materialize_current
    calls = {"n": 0}

    async def flaky_materialize(**kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("injected: materialize failure")
        await original(**kwargs)

    store.materialize_current = flaky_materialize  # type: ignore[method-assign]
    outcome = await _commit(memory_service, session_factory, plans, uuid4())
    assert any("物化失败" in warning for warning in outcome.warnings)

    # 读取以数据库活动指针 + 不可变版本为准，不依赖 current 物化副本（§8.6）
    learner = await memory_service.get_learner(user_id=USER)
    assert learner is not None
    assert learner.goals == ["期中考试达到 90 分"]
    mastery = await memory_service.get_mastery(user_id=USER, topic_key="yici-hanshu")
    assert mastery is not None and mastery.overview == "基本掌握"

    # 物化修复（§23.3：可修复 current）：按活动版本重新物化，内容与不可变版本一致
    from backend.memory.storage.base import logical_path_for

    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT active_checksum FROM memory_documents"
                    " WHERE user_id = :u AND memory_id = 'learner'"
                ),
                {"u": USER},
            )
        ).first()
    version_content = await store.read_version_by_id(
        user_id=USER, memory_id="learner", version=1, checksum=str(row[0])
    )
    await original(user_id=USER, memory_id="learner", content=version_content)
    current = (
        Path(settings.memory_storage_root)
        / "users"
        / str(USER)[:2]
        / str(USER)
        / "current"
        / logical_path_for("learner")
    )
    assert current.read_bytes() == version_content
