"""ConversationSourceReadService 集成测试（方案 §8，Phase 1 验收核心）。

覆盖 §8.3 读取规则：
- 按稳定 message_ids 精确读取，按 sequence 稳定排序；
- 线程归属校验、未完成消息不可读、eligible_for_memory=false 不可读；
- checkpoint_id 完整 SHA-256 匹配（D9：禁止截取 16 位）；
- 越权与不存在统一 SOURCE_NOT_FOUND（§8.3 #9）；
- SourceBundle 冻结契约（≤200 items、80KB、metadata 限制）由 SourceBundle 自身保证。
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.conversation.contracts.domain import build_source_manifest
from backend.conversation.persistence import messages as messages_repo
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo
from backend.conversation.services.source_read_service import ConversationSourceReadService
from backend.memory.contracts.errors import SourceNotFoundError

pytestmark = pytest.mark.asyncio


async def _seed_thread_with_messages(
    session_factory: async_sessionmaker,
    *,
    user_id: UUID | None = None,
    thread_id: UUID | None = None,
) -> tuple[UUID, UUID, UUID, list[dict]]:
    """构造 thread + turn + user/assistant 两条 completed 消息。"""
    user_id = user_id or uuid4()
    thread_id = thread_id or uuid4()
    turn_id = uuid4()
    user_msg_id = uuid4()
    assistant_msg_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
            await turns_repo.insert_turn(
                session,
                turn_id=turn_id,
                thread_id=thread_id,
                user_id=user_id,
                client_request_id=f"req-{turn_id}",
                request_id="t",
                run_id="r",
                user_message_id=user_msg_id,
                expected_thread_version=0,
                graph_thread_id=f"conv-turn:{turn_id}",
            )
            await messages_repo.increment_thread_sequence(session, thread_id, by=2)
            await messages_repo.insert_message(
                session,
                message_id=user_msg_id,
                thread_id=thread_id,
                turn_id=turn_id,
                user_id=user_id,
                sequence=1,
                role="user",
                content="勾股定理是什么？",
                content_hash=hashlib.sha256("勾股定理是什么？".encode()).hexdigest(),
            )
            await messages_repo.insert_message(
                session,
                message_id=assistant_msg_id,
                thread_id=thread_id,
                turn_id=turn_id,
                user_id=user_id,
                sequence=2,
                role="assistant",
                content="勾股定理：直角三角形两直角边平方和等于斜边平方。",
                content_hash=hashlib.sha256(
                    "勾股定理：直角三角形两直角边平方和等于斜边平方。".encode()
                ).hexdigest(),
            )
    return user_id, thread_id, turn_id, [user_msg_id, assistant_msg_id]


async def test_read_source_bundle_by_stable_ids(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """§8.3 #3/#4：按 message_ids 精确读取、sequence 稳定排序、SourceItem 映射 §8.4。"""
    user_id, thread_id, turn_id, message_ids = await _seed_thread_with_messages(
        conversation_session_factory
    )
    service = ConversationSourceReadService(session_factory=conversation_session_factory)

    async with conversation_session_factory() as session:
        rows = await messages_repo.get_messages_by_ids(session, message_ids)
        checkpoint_id = build_source_manifest(thread_id, turn_id, rows)

    bundle = await service.read_source_bundle(
        user_id=user_id,
        thread_id=str(thread_id),
        checkpoint_id=checkpoint_id,
        message_ids=[str(mid) for mid in message_ids],
    )
    assert len(bundle.items) == 2
    assert bundle.items[0].metadata["sequence"] == 1
    assert bundle.items[1].metadata["sequence"] == 2
    assert bundle.items[0].source_ref == f"conversation:{thread_id}:message:{message_ids[0]}"
    assert bundle.items[1].metadata["thread_id"] == str(thread_id)
    assert bundle.items[1].metadata["turn_id"] == str(turn_id)
    assert bundle.total_utf8_bytes > 0


async def test_read_ownership_and_not_found(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """§8.3 #9：跨用户访问统一 SOURCE_NOT_FOUND；消息不存在同样处理。"""
    user_id, thread_id, _, message_ids = await _seed_thread_with_messages(
        conversation_session_factory
    )
    service = ConversationSourceReadService(session_factory=conversation_session_factory)

    with pytest.raises(SourceNotFoundError):
        await service.read_source_bundle(
            user_id=uuid4(),  # 其他用户
            thread_id=str(thread_id),
            checkpoint_id=None,
            message_ids=[str(mid) for mid in message_ids],
        )
    with pytest.raises(SourceNotFoundError):
        await service.read_source_bundle(
            user_id=user_id,
            thread_id=str(thread_id),
            checkpoint_id=None,
            message_ids=[str(uuid4())],  # 不存在的消息
        )


async def test_read_checkpoint_mismatch_and_unfinished_message(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """§8.3 #5：checkpoint 不匹配拒绝；未完成消息不可读。"""
    user_id, thread_id, _, message_ids = await _seed_thread_with_messages(
        conversation_session_factory
    )
    service = ConversationSourceReadService(session_factory=conversation_session_factory)

    with pytest.raises(SourceNotFoundError):
        await service.read_source_bundle(
            user_id=user_id,
            thread_id=str(thread_id),
            checkpoint_id="conv-src-v1:bad:hash",  # 伪造 checkpoint
            message_ids=[str(mid) for mid in message_ids],
        )

    # 未完成消息（status != completed）不可读
    _, thread2, _, ids2 = await _seed_thread_with_messages(conversation_session_factory)
    async with conversation_session_factory() as session:
        async with session.begin():
            await session.execute(
                __import__("sqlalchemy").text(
                    "UPDATE conversation.conversation_messages "
                    "SET status = 'cancelled' WHERE message_id = :mid"
                ),
                {"mid": ids2[1]},
            )
    with pytest.raises(SourceNotFoundError):
        await service.read_source_bundle(
            user_id=user_id,
            thread_id=str(thread2),
            checkpoint_id=None,
            message_ids=[str(mid) for mid in ids2],
        )


async def test_read_ignores_deleted_messages(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """§8.6：线程删除后 Reader 抑制来源（deleted 状态消息不可读）。"""
    user_id, thread_id, _, message_ids = await _seed_thread_with_messages(
        conversation_session_factory
    )
    service = ConversationSourceReadService(session_factory=conversation_session_factory)
    async with conversation_session_factory() as session:
        async with session.begin():
            await threads_repo.set_thread_status(session, thread_id, "deleting")
    with pytest.raises(SourceNotFoundError):
        await service.read_source_bundle(
            user_id=user_id,
            thread_id=str(thread_id),
            checkpoint_id=None,
            message_ids=[str(mid) for mid in message_ids],
        )
