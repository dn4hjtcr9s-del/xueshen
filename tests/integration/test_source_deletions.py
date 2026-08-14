"""Source deletion 集成测试（§17.3 / §23.3）：真实 PostgreSQL 记录与 Reader 抑制。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.errors import SourceDeletedError
from backend.memory.contracts.evidence import SourceDeletedEvent, SourceItem
from backend.memory.readers.filtering import DeletionAwareConversationReader
from backend.memory.readers.handler import RecordingSourceDeletionHandler
from backend.memory.readers.testing import FakeConversationReader

USER = UUID("00000000-0000-4000-8000-000000000010")
NOW = datetime(2026, 8, 11, 8, 0, 0, tzinfo=UTC)


def _event(ref: str, version: str | None = None) -> SourceDeletedEvent:
    return SourceDeletedEvent(
        event_id=uuid4(),
        source_system="conversation",
        source_ref=ref,
        source_version=version,
        deleted_at=NOW,
    )


def _item(ref: str, content: str) -> SourceItem:
    return SourceItem(source_ref=ref, role="user", content=content, occurred_at=NOW)


async def test_handler_records_and_dedupes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    handler = RecordingSourceDeletionHandler(session_factory=session_factory)
    event = _event("m1")
    assert await handler.handle(user_id=USER, event=event) == "recorded"
    assert await handler.handle(user_id=USER, event=event) == "duplicate"

    async with session_factory() as session:
        from sqlalchemy import text

        result = await session.execute(
            text("SELECT count(*) FROM source_deletions WHERE user_id = :u"), {"u": USER}
        )
        assert result.scalar_one() == 1


async def test_deletion_aware_reader_partial_and_full(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    handler = RecordingSourceDeletionHandler(session_factory=session_factory)
    await handler.handle(user_id=USER, event=_event("m2"))

    inner = FakeConversationReader()
    inner.add_message("t1", _item("m1", "我懂了顶点式"))
    inner.add_message("t1", _item("m2", "已删除的内容"))
    reader = DeletionAwareConversationReader(inner=inner, session_factory=session_factory)

    # 部分删除：过滤 + deleted_refs（裁决 A）
    bundle = await reader.read(
        user_id=USER, thread_id="t1", checkpoint_id=None, message_ids=["m1", "m2"]
    )
    assert [i.source_ref for i in bundle.items] == ["m1"]
    assert bundle.deleted_refs == ["m2"]

    # 全部删除：SOURCE_DELETED
    await handler.handle(user_id=USER, event=_event("m1"))
    with pytest.raises(SourceDeletedError):
        await reader.read(
            user_id=USER, thread_id="t1", checkpoint_id=None, message_ids=["m1", "m2"]
        )

    # 其他用户不受该删除事实影响（用户隔离）
    bundle = await reader.read(
        user_id=UUID("00000000-0000-4000-8000-000000000011"),
        thread_id="t1",
        checkpoint_id=None,
        message_ids=["m1", "m2"],
    )
    assert len(bundle.items) == 2


async def test_deletion_aware_activity_reader_filters(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Community §10.4 第 6 条/§15.2：DeletionAwareActivityReader 过滤删除事实。

    覆盖：部分删除 → 过滤 + deleted_refs；全部删除 → SOURCE_DELETED；
    用户隔离；source_system=activity 独立于 conversation 删除。
    """
    from backend.memory.readers.filtering import DeletionAwareActivityReader
    from backend.memory.readers.handler import RecordingSourceDeletionHandler
    from backend.memory.readers.testing import FakeActivityReader

    handler = RecordingSourceDeletionHandler(session_factory=session_factory)
    await handler.handle(
        user_id=USER,
        event=SourceDeletedEvent(
            event_id=uuid4(),
            source_system="activity",
            source_ref="post:p2",
            source_version=None,
            deleted_at=NOW,
        ),
    )

    inner = FakeActivityReader()
    # FakeActivityReader 以 activity_id（post:p1 等）为 key
    inner.add_activity(_item("post:p1", "p1 内容"))
    inner.add_activity(_item("post:p2", "p2 已删除内容"))
    reader = DeletionAwareActivityReader(inner=inner, session_factory=session_factory)

    # 部分删除：过滤 + deleted_refs
    bundle = await reader.read(
        user_id=USER,
        activity_type="forum_post",
        activity_ids=["post:p1", "post:p2"],
        content_ref=None,
    )
    assert [i.source_ref for i in bundle.items] == ["post:p1"]
    assert bundle.deleted_refs == ["post:p2"]

    # 全部删除：SOURCE_DELETED
    await handler.handle(
        user_id=USER,
        event=SourceDeletedEvent(
            event_id=uuid4(),
            source_system="activity",
            source_ref="post:p1",
            source_version=None,
            deleted_at=NOW,
        ),
    )
    with pytest.raises(SourceDeletedError):
        await reader.read(
            user_id=USER,
            activity_type="forum_post",
            activity_ids=["post:p1", "post:p2"],
            content_ref=None,
        )

    # 其他用户不受该删除事实影响（用户隔离）
    bundle = await reader.read(
        user_id=UUID("00000000-0000-4000-8000-000000000011"),
        activity_type="forum_post",
        activity_ids=["post:p1", "post:p2"],
        content_ref=None,
    )
    assert len(bundle.items) == 2

    # conversation 删除事实不影响 activity 读取（source_system 隔离）
    async with session_factory() as session:
        from sqlalchemy import text

        await session.execute(
            text(
                "INSERT INTO source_deletions "
                "(source_deletion_id, user_id, source_system, source_ref, source_version, "
                " deleted_at, idempotency_hash) "
                "VALUES (:id, :u, 'conversation', 'post:p1', NULL, :at, :hash)"
            ),
            {
                "id": uuid4(),
                "u": UUID("00000000-0000-4000-8000-000000000011"),
                "hash": f"conv-{uuid4()}",
                "at": NOW,
            },
        )
        await session.commit()
    bundle = await reader.read(
        user_id=UUID("00000000-0000-4000-8000-000000000011"),
        activity_type="forum_post",
        activity_ids=["post:p1"],
        content_ref=None,
    )
    assert len(bundle.items) == 1
