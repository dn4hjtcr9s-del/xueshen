"""verify_checksums 维护分支集成测试（§14.3 / 评审 #14）：真实 PostgreSQL + 真实存储。

覆盖：健康文档 done；checksum 篡改检出；current/ 漂移检出并重新物化修复；
dry_run 不修复；有界 batch + cursor 续跑。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import MaintenanceCommand
from backend.memory.contracts.common import SYSTEM_MAINTENANCE_USER_ID
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.graph.state import MemoryRuntimeContext
from backend.memory.persistence import documents as docs_repo
from backend.memory.persistence import maintenance as maintenance_repo
from backend.memory.storage.base import logical_path_for
from backend.memory.storage.local_markdown import LocalMarkdownStore
from backend.memory.storage.markdown_schema import (
    LearnerDocument,
    MasteryDocument,
    render_learner,
    render_mastery,
)
from backend.settings import Settings
from tests.integration.graph_helpers import make_operation, persist_operation

NOW = datetime(2026, 8, 12, 4, 0, tzinfo=UTC)


def _learner_content(user_id: UUID) -> bytes:
    return render_learner(
        LearnerDocument(
            user_id=user_id,
            version=1,
            updated_at=NOW,
            preferences=["喜欢图形化讲解"],
            goals=["期中考试 90 分"],
            plans=["每天一道极限题"],
            evidence_refs=[],
            confidence=0.8,
        )
    ).encode()


def _mastery_content(user_id: UUID, topic_key: str) -> bytes:
    return render_mastery(
        MasteryDocument(
            user_id=user_id,
            topic_key=topic_key,
            topic_title=topic_key,
            version=1,
            updated_at=NOW,
            overview="能区分逐点收敛与一致收敛。",
            understood=["逐点收敛定义"],
            difficulties=[],
            review_advice=[],
            evidence_refs=[],
            confidence=None,
        )
    ).encode()


async def _seed_document(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    *,
    user_id: UUID,
    memory_id: str,
    memory_type: str,
    content: bytes,
    topic_key: str | None = None,
) -> None:
    """写入真实版本文件 + current/ 物化副本，并登记 memory_documents 活动指针。"""
    stored = await store.write_immutable_version(
        user_id=user_id, memory_id=memory_id, version=1, content=content
    )
    await store.materialize_current(user_id=user_id, memory_id=memory_id, content=content)
    async with session_factory() as session:
        async with session.begin():
            await docs_repo.upsert_document(
                session,
                user_id=user_id,
                memory_id=memory_id,
                memory_type=memory_type,
                topic_key=topic_key,
                topic_title=topic_key,
                logical_path=logical_path_for(memory_id),
            )
            await docs_repo.set_active_version(
                session,
                user_id=user_id,
                memory_id=memory_id,
                active_version=1,
                active_storage_key=stored.storage_key,
                active_checksum=stored.checksum,
            )


async def _run_verify(
    session_factory: async_sessionmaker[AsyncSession],
    runtime_context: MemoryRuntimeContext,
    *,
    batch_size: int = 100,
    cursor: str | None = None,
    dry_run: bool = False,
    idem_suffix: str,
) -> dict[str, Any]:
    """Scheduler 侧建 run + 关联 Graph operation，然后经 Graph 执行并返回 detail。"""
    operation = make_operation(
        user_id=UUID(SYSTEM_MAINTENANCE_USER_ID),
        actor_type="system",
        input_kind="maintenance",
        operation_type="verify_checksums",
        priority=0,
        payload=MaintenanceCommand(
            kind="verify_checksums", batch_size=batch_size, cursor=cursor, dry_run=dry_run
        ),
    )
    await persist_operation(session_factory, operation)
    async with session_factory() as session:
        async with session.begin():
            run, _created = await maintenance_repo.create_or_reuse_run(
                session,
                run_id=uuid4(),
                maintenance_type="verify_checksums",
                idempotency_key=f"verify-checksums:it-{idem_suffix}",
            )
            await maintenance_repo.attach_operation(
                session, run_id=run["run_id"], operation_id=operation.operation_id
            )
    runner = LocalLangGraphRunner(context=runtime_context)
    result = await runner.run(operation)
    assert result.status == "succeeded"
    async with session_factory() as session:
        run_row = await maintenance_repo.get_run_by_key(
            session, idempotency_key=f"verify-checksums:it-{idem_suffix}"
        )
    assert run_row is not None
    return dict(run_row["result"])


class TestVerifyChecksums:
    async def test_healthy_documents_done(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runtime_context: MemoryRuntimeContext,
        store: LocalMarkdownStore,
    ) -> None:
        user_id = uuid4()
        await _seed_document(
            session_factory,
            store,
            user_id=user_id,
            memory_id="learner",
            memory_type="learner",
            content=_learner_content(user_id),
        )
        detail = await _run_verify(session_factory, runtime_context, idem_suffix="healthy")
        assert detail["status"] == "done"
        assert detail["checked"] == 1
        assert detail["corrupted"] == []
        assert detail["rematerialized"] == 0

    async def test_checksum_mismatch_reported(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runtime_context: MemoryRuntimeContext,
        store: LocalMarkdownStore,
        settings: Settings,
    ) -> None:
        user_id = uuid4()
        original = _learner_content(user_id)
        await _seed_document(
            session_factory,
            store,
            user_id=user_id,
            memory_id="learner",
            memory_type="learner",
            content=original,
        )
        async with session_factory() as session:
            doc = await docs_repo.get_document(session, user_id=user_id, memory_id="learner")
        assert doc is not None
        # 篡改不可变版本文件（storage_key 相对存储根）
        version_path = Path(settings.memory_storage_root) / doc["active_storage_key"]
        version_path.write_bytes(b"tampered-content")

        detail = await _run_verify(session_factory, runtime_context, idem_suffix="mismatch")
        assert detail["status"] == "done"
        assert detail["checked"] == 1
        assert len(detail["corrupted"]) == 1
        issue = detail["corrupted"][0]
        assert issue["memory_id"] == "learner"
        assert "checksum_mismatch" in issue["reasons"]
        # current/ 与损坏版本不一致也会记 current_drift，但校验和不可信时不重新物化，
        # 避免把损坏内容传播到物化副本
        assert "current_drift" in issue["reasons"]
        assert detail["rematerialized"] == 0
        current = await store.read_current(user_id=user_id, memory_id="learner")
        assert current == original

    async def test_current_drift_rematerialized(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runtime_context: MemoryRuntimeContext,
        store: LocalMarkdownStore,
        settings: Settings,
    ) -> None:
        user_id = uuid4()
        content = _learner_content(user_id)
        await _seed_document(
            session_factory,
            store,
            user_id=user_id,
            memory_id="learner",
            memory_type="learner",
            content=content,
        )
        current_path = (
            Path(settings.memory_storage_root)
            / "users"
            / str(user_id)[:2]
            / str(user_id)
            / "current"
            / "learner.md"
        )
        current_path.write_bytes(b"stale-current")

        detail = await _run_verify(session_factory, runtime_context, idem_suffix="drift")
        assert detail["status"] == "done"
        issue = detail["corrupted"][0]
        assert issue["reasons"] == ["current_drift"]
        assert detail["rematerialized"] == 1
        # 物化副本已按活动版本修复
        assert current_path.read_bytes() == content

    async def test_dry_run_reports_without_repair(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runtime_context: MemoryRuntimeContext,
        store: LocalMarkdownStore,
        settings: Settings,
    ) -> None:
        user_id = uuid4()
        await _seed_document(
            session_factory,
            store,
            user_id=user_id,
            memory_id="learner",
            memory_type="learner",
            content=_learner_content(user_id),
        )
        current_path = (
            Path(settings.memory_storage_root)
            / "users"
            / str(user_id)[:2]
            / str(user_id)
            / "current"
            / "learner.md"
        )
        current_path.write_bytes(b"stale-current")

        detail = await _run_verify(
            session_factory, runtime_context, dry_run=True, idem_suffix="dry-run"
        )
        assert detail["corrupted"][0]["reasons"] == ["current_drift"]
        assert detail["rematerialized"] == 0
        assert current_path.read_bytes() == b"stale-current"

    async def test_bounded_batches_continue_with_cursor(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runtime_context: MemoryRuntimeContext,
        store: LocalMarkdownStore,
    ) -> None:
        user_id = uuid4()
        await _seed_document(
            session_factory,
            store,
            user_id=user_id,
            memory_id="learner",
            memory_type="learner",
            content=_learner_content(user_id),
        )
        await _seed_document(
            session_factory,
            store,
            user_id=user_id,
            memory_id="mastery:一致收敛",
            memory_type="mastery",
            topic_key="一致收敛",
            content=_mastery_content(user_id, "一致收敛"),
        )
        first = await _run_verify(
            session_factory, runtime_context, batch_size=1, idem_suffix="batch-1"
        )
        assert first["status"] == "continue"
        assert first["checked"] == 1
        cursor = first["next_cursor"]
        assert cursor == f"{user_id}:learner"

        second = await _run_verify(
            session_factory, runtime_context, batch_size=1, cursor=cursor, idem_suffix="batch-2"
        )
        assert second["status"] == "continue"
        assert second["checked"] == 1
        assert second["next_cursor"] == f"{user_id}:mastery:一致收敛"

        # 与 purge_tombstones 同语义：末尾恰满批时需一个空批次确认收尾
        third = await _run_verify(
            session_factory,
            runtime_context,
            batch_size=1,
            cursor=second["next_cursor"],
            idem_suffix="batch-3",
        )
        assert third["status"] == "done"
        assert third["checked"] == 0
        assert third["next_cursor"] is None
