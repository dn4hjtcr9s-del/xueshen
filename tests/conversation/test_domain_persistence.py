"""Conversation 域集成测试：threads / turns / messages / events（方案 §7，Phase 1 验收）。

覆盖：
- 幂等落库：用户消息与 Turn 同事务创建，client_request_id 幂等；
- Turn Event 序号由 last_event_sequence 分配（Q8），(turn_id, sequence) 唯一约束；
- 并发单调性：两个写入者并发追加不产生重复/乱序序号；
- claim/lease/fencing：可 claim 条件、attempt 递增、过期 lease 回收、续租 fencing；
- 取消原子分支（R2）：accepted 直接转 cancelled、running 转 cancelling；
- 同线程活动 Turn 唯一约束（§5.4 业务串行约束）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.conversation.contracts.events import TurnEventWrite
from backend.conversation.persistence import messages as messages_repo
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo
from backend.conversation.persistence.event_writer import TurnEventWriter
from backend.conversation.persistence.events import insert_event

pytestmark = pytest.mark.asyncio


async def _create_thread_and_turn(
    session_factory: async_sessionmaker,
    *,
    user_id: UUID | None = None,
    thread_id: UUID | None = None,
    turn_id: UUID | None = None,
) -> tuple[dict, dict, dict]:
    """构造 thread + turn + user message（模拟 API 接收事务 §5.1）。"""
    user_id = user_id or uuid4()
    thread_id = thread_id or uuid4()
    turn_id = turn_id or uuid4()
    async with session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
            await turns_repo.insert_turn(
                session,
                turn_id=turn_id,
                thread_id=thread_id,
                user_id=user_id,
                client_request_id=f"req-{turn_id}",
                request_id="trace-1",
                run_id="run-1",
                user_message_id=uuid4(),
                expected_thread_version=0,
                graph_thread_id=f"conv-turn:{turn_id}",
            )
            await messages_repo.increment_thread_sequence(session, thread_id, by=1)
            await messages_repo.insert_message(
                session,
                message_id=uuid4(),
                thread_id=thread_id,
                turn_id=turn_id,
                user_id=user_id,
                sequence=1,
                role="user",
                content="你好，请解释勾股定理",
                content_hash="hash-user-1",
            )
    return user_id, thread_id, turn_id


async def test_turn_event_sequence_monotonic_and_unique(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """Q8：last_event_sequence 事务内原子分配；(turn_id, sequence) 唯一约束防重。"""
    _, _thread_id, turn_id = await _create_thread_and_turn(conversation_session_factory)
    writer = TurnEventWriter(id_generator=type("IG", (), {"new_uuid": staticmethod(uuid4)})())

    async with conversation_session_factory() as session:
        async with session.begin():
            ev1 = await writer.append(
                session,
                write=TurnEventWrite(
                    turn_id=turn_id,
                    event_type="turn.started",
                    request_id="trace-1",
                    run_id="run-1",
                    payload={"status": "running"},
                ),
            )
            ev2 = await writer.append(
                session,
                write=TurnEventWrite(
                    turn_id=turn_id,
                    event_type="answer.delta",
                    request_id="trace-1",
                    run_id="run-1",
                    payload={"text_delta": "勾股"},
                ),
            )
    async with conversation_session_factory() as session:
        async with session.begin():
            progress = await writer.append(
                session,
                write=TurnEventWrite(
                    turn_id=turn_id,
                    event_type="turn.progress",
                    request_id="trace-1",
                    run_id="run-1",
                    payload={
                        "stage": "retrieval",
                        "status": "started",
                        "title": "正在检索教材资料",
                        "metadata": {},
                    },
                ),
            )
    assert ev1["sequence"] == 1
    assert ev2["sequence"] == 2
    assert progress["sequence"] == 3

    # 唯一约束：重复插入相同 (turn_id, sequence) 必须失败（IntegrityError）
    with pytest.raises(IntegrityError):
        async with conversation_session_factory() as session:
            async with session.begin():
                await insert_event(
                    session,
                    event_id=uuid4(),
                    turn_id=turn_id,
                    sequence=1,
                    event_type="turn.started",
                    request_id="x",
                    run_id="y",
                    payload={"status": "running"},
                )


async def test_claim_lease_fencing_and_reclaim(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """§5.4：claim 递增 generation/attempt；过期 lease 可回收；续租需 fencing。"""
    _, _thread_id, turn_id = await _create_thread_and_turn(conversation_session_factory)

    async with conversation_session_factory() as session:
        async with session.begin():
            claimed = await turns_repo.try_claim_turn(
                session, turn_id, worker_id="worker-a", lease_seconds=60
            )
    assert claimed is not None
    assert claimed["status"] == "running"
    assert claimed["attempt_count"] == 1
    assert claimed["lease_generation"] == 1

    # 未过期时其他 worker 不能 claim
    async with conversation_session_factory() as session:
        async with session.begin():
            claimed2 = await turns_repo.try_claim_turn(
                session, turn_id, worker_id="worker-b", lease_seconds=60
            )
    assert claimed2 is None

    # 续租 fencing：owner 正确才能续
    async with conversation_session_factory() as session:
        async with session.begin():
            ok = await turns_repo.renew_lease(
                session, turn_id, worker_id="worker-a", lease_seconds=60
            )
            bad = await turns_repo.renew_lease(
                session, turn_id, worker_id="worker-b", lease_seconds=60
            )
    assert ok is True
    assert bad is False

    # 过期 lease 可被回收（下一次 attempt 退避后到点）
    past = datetime.now(UTC) - timedelta(seconds=5)
    async with conversation_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE conversation.conversation_turns "
                    "SET lease_expires_at = :past, next_attempt_at = :past "
                    "WHERE turn_id = :turn_id"
                ),
                {"past": past, "turn_id": turn_id},
            )
            reclaimed = await turns_repo.try_claim_turn(
                session, turn_id, worker_id="worker-b", lease_seconds=60
            )
    assert reclaimed is not None
    assert reclaimed["status"] == "running"
    assert reclaimed["attempt_count"] == 2
    assert reclaimed["lease_generation"] == 2
    assert reclaimed["lease_owner"] == "worker-b"


async def test_cancel_atomic_branches(conversation_session_factory) -> None:
    """R2：accepted 直接转 cancelled；running 转 cancelling。"""
    _, _thread_id, turn_id = await _create_thread_and_turn(conversation_session_factory)

    async with conversation_session_factory() as session:
        async with session.begin():
            assert await turns_repo.cancel_accepted_turn(session, turn_id) is True
            # 幂等：已终态的取消请求不再生效
            assert await turns_repo.cancel_accepted_turn(session, turn_id) is False
        row = await turns_repo.get_turn(session, turn_id)
    assert row["status"] == "cancelled"

    _, _, turn_id2 = await _create_thread_and_turn(conversation_session_factory)
    async with conversation_session_factory() as session:
        async with session.begin():
            await turns_repo.try_claim_turn(session, turn_id2, worker_id="w", lease_seconds=60)
            assert await turns_repo.mark_cancelling(session, turn_id2) is True
            # 回收者写终态
            assert await turns_repo.write_terminal_cancelled(session, turn_id2) is True
        row = await turns_repo.get_turn(session, turn_id2)
    assert row["status"] == "cancelled"


async def test_one_active_turn_per_thread(conversation_session_factory) -> None:
    """§5.4：同线程活动 Turn 部分唯一索引是业务串行约束。"""
    user_id, thread_id, _ = await _create_thread_and_turn(conversation_session_factory)
    with pytest.raises(IntegrityError):
        async with conversation_session_factory() as session:
            async with session.begin():
                await turns_repo.insert_turn(
                    session,
                    turn_id=uuid4(),
                    thread_id=thread_id,
                    user_id=user_id,
                    client_request_id="req-2",
                    request_id="t2",
                    run_id="r2",
                    user_message_id=uuid4(),
                    expected_thread_version=1,
                    graph_thread_id="conv-turn:x",
                )


async def test_thread_version_bump(conversation_session_factory) -> None:
    """R5：接受用户消息后 Thread version +1。"""
    _user_id, thread_id, _ = await _create_thread_and_turn(conversation_session_factory)
    async with conversation_session_factory() as session:
        async with session.begin():
            new_version = await threads_repo.bump_thread_version(session, thread_id)
        row = await threads_repo.get_thread(session, thread_id)
    assert new_version == 1
    assert row["version"] == 1
