"""ConversationService：REST 写路径服务层（方案 §5.1 / §17.2 / §17.3 / §1.5）。

接收事务（§5.1，原子）：
1. 校验登录身份和会话归属；
2. client_request_id 幂等检查；
3. 检查同一 thread 是否已有运行中 Turn（§5.4 串行约束）；
4. 创建 conversation_turn；
5. 创建不可变用户消息和稳定 message_id；
6. 写入 turn.accepted 持久化事件；
7. 提交事务后由 conversation-worker 通过 DB 轮询 claim（API 不直接启动 Graph）。

版本语义（§1.5 R5）：非幂等 Turn 创建锁定 Thread 比较 version，
接受用户消息后 +1；client_request_id 幂等命中优先于 version 比较。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.contracts.errors import (
    ConversationNotFoundError,
    RequestIdempotencyConflictError,
    ThreadVersionConflictError,
    TurnAlreadyRunningError,
)
from backend.conversation.contracts.events import TurnEventWrite
from backend.conversation.persistence import messages as messages_repo
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo
from backend.conversation.persistence.event_writer import TurnEventWriter


class ConversationService:
    """Conversation 写路径服务（API 层调用；Graph 侧使用 ConversationRepository）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        turn_event_writer: TurnEventWriter,
    ) -> None:
        self._session_factory = session_factory
        self._turn_event_writer = turn_event_writer

    @property
    def turn_event_writer(self) -> TurnEventWriter:
        return self._turn_event_writer

    async def create_thread(self, *, user_id: UUID) -> dict[str, Any]:
        """创建会话（§17.1）：返回 thread_id + version=0。"""
        thread_id = uuid4()
        async with self._session_factory() as session:
            async with session.begin():
                await threads_repo.insert_thread(session, thread_id, user_id)
        return {"thread_id": thread_id, "version": 0}

    async def create_turn(
        self,
        *,
        user_id: UUID,
        thread_id: UUID,
        client_request_id: str,
        content: str,
        expected_thread_version: int,
        request_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """幂等创建 Turn + 用户消息（§5.1 / §17.2 / §17.3）。"""
        # Q4 / 评审 P1-4：服务层双重校验（§17.2：API Schema 与服务层都要校验，
        # 防止绕过 HTTP Schema 的内部调用写入超限消息）。strip 后非空且 ≤10000 字符。
        stripped = content.strip()
        if not stripped:
            from backend.memory.contracts.errors import InvalidPayloadError

            raise InvalidPayloadError("用户消息不能为空或纯空白", field="content")
        if len(stripped) > 10_000:
            from backend.memory.contracts.errors import InvalidPayloadError

            raise InvalidPayloadError("用户消息超过 10000 字符上限", field="content")
        async with self._session_factory() as session:
            async with session.begin():
                thread = await threads_repo.get_thread(session, thread_id, for_update=True)
                if thread is None or thread["user_id"] != user_id:
                    raise ConversationNotFoundError("会话不存在或无权访问")
                if thread["status"] != "active":
                    raise ConversationNotFoundError("会话不可用")

                # 幂等命中优先于 version 比较（§1.5 R5）
                existing = await turns_repo.get_turn_by_client_request(
                    session, thread_id, client_request_id
                )
                if existing is not None:
                    if existing["user_id"] != user_id:
                        raise RequestIdempotencyConflictError("幂等键归属不符")
                    return await self._turn_response(session, existing)

                # 同线程活动 Turn 检查（业务串行约束 §5.4）
                active = await turns_repo.get_active_turn(session, thread_id)
                if active is not None:
                    raise TurnAlreadyRunningError("该会话已有运行中的回答，请等待完成")

                # version 乐观锁（§1.5 R5）
                if thread["version"] != expected_thread_version:
                    raise ThreadVersionConflictError(
                        "会话版本已变化，请刷新后重试",
                        field="expected_thread_version",
                        current_version=int(thread["version"]),
                    )

                # 1. Turn（accepted，立即可 claim，附录 A.2）
                turn_id = uuid4()
                user_message_id = uuid4()
                graph_thread_id = f"conv-turn:{turn_id}"
                inserted = await turns_repo.insert_turn(
                    session,
                    turn_id=turn_id,
                    thread_id=thread_id,
                    user_id=user_id,
                    client_request_id=client_request_id,
                    request_id=request_id,
                    run_id=run_id,
                    user_message_id=user_message_id,
                    expected_thread_version=expected_thread_version,
                    graph_thread_id=graph_thread_id,
                )
                if not inserted:
                    raise RequestIdempotencyConflictError("幂等键已存在")

                # 2. 用户消息（completed，稳定 message_id，§7.3）
                sequence = await messages_repo.increment_thread_sequence(session, thread_id, by=1)
                await messages_repo.insert_message(
                    session,
                    message_id=user_message_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    user_id=user_id,
                    sequence=sequence,
                    role="user",
                    content=content,
                    content_hash=__import__("hashlib").sha256(content.encode()).hexdigest(),
                    status="completed",
                    eligible_for_context=True,
                    eligible_for_memory=True,
                )

                # 3. Thread version +1（接受用户消息后，§1.5 R5）
                new_version = await threads_repo.bump_thread_version(session, thread_id)

                # 4. turn.accepted 事件（§5.1 步骤 6）
                await self._turn_event_writer.append(
                    session,
                    write=TurnEventWrite(
                        turn_id=turn_id,
                        event_type="turn.accepted",
                        request_id=request_id,
                        run_id=run_id,
                        payload={"status": "accepted", "user_message_id": str(user_message_id)},
                    ),
                )
                row = await turns_repo.get_turn(session, turn_id)
                assert row is not None
                return {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "user_message_id": user_message_id,
                    "thread_version": new_version,
                    "status": row["status"],
                }

    async def _turn_response(self, session: AsyncSession, row: dict[str, Any]) -> dict[str, Any]:
        """幂等命中：返回原 Turn 当前状态（§17.3）。

        修复（评审 P1-3）：thread_version 返回线程**当前**版本而非
        expected_thread_version（旧版本会让前端下次发送必然 409）。
        """
        thread = await threads_repo.get_thread(session, row["thread_id"])
        current_version = int(thread["version"]) if thread else int(row["expected_thread_version"])
        return {
            "thread_id": row["thread_id"],
            "turn_id": row["turn_id"],
            "user_message_id": row["user_message_id"],
            "thread_version": current_version,
            "status": row["status"],
        }

    async def find_turn_by_client_request(
        self,
        *,
        user_id: UUID,
        thread_id: UUID,
        client_request_id: str,
    ) -> dict[str, Any] | None:
        """只读幂等预检（评审 P1-3 / Q13）：命中已有 Turn 返回其状态，不计数。

        归属校验：Turn 必须属于该用户与该线程。
        """
        async with self._session_factory() as session:
            existing = await turns_repo.get_turn_by_client_request(
                session, thread_id, client_request_id
            )
            if existing is None or existing["user_id"] != user_id:
                return None
            return await self._turn_response(session, existing)

    async def delete_thread(self, *, user_id: UUID, thread_id: UUID) -> None:
        """删除会话（§8.6 步骤 1）：置 deleting + 递增 generation + 写 Job。"""
        async with self._session_factory() as session:
            async with session.begin():
                thread = await threads_repo.get_thread(session, thread_id, for_update=True)
                if thread is None or thread["user_id"] != user_id:
                    raise ConversationNotFoundError("会话不存在或无权访问")
                if thread["status"] == "deleted":
                    return  # 幂等：已删除
                if thread["status"] == "deleting":
                    return  # 幂等：删除中
                await threads_repo.set_thread_status(
                    session, thread_id, "deleting", bump_deletion_generation=True
                )
                new_generation = int(thread["deletion_generation"]) + 1
                # 取消活动 Turn（R4：accepted 直接取消，running 置 cancelling）
                active = await turns_repo.get_active_turn(session, thread_id, for_update=True)
                if active is not None:
                    if active["status"] == "accepted":
                        await turns_repo.cancel_accepted_turn(session, active["turn_id"])
                    elif active["status"] == "running":
                        await turns_repo.mark_cancelling(session, active["turn_id"])
                # C7（评审）：写每个已投递来源的 memory.source_deleted Outbox
                # （§8.6 步骤 1：写入每个已投递来源的删除事件）。
                # source_ref 与 Reader 返回值一致；event_id 由客户端稳定生成以
                # 保证幂等重放（评审 P2：不得服务端随机生成）。
                await _write_source_deletion_outbox(
                    session,
                    thread_id=thread_id,
                    user_id=user_id,
                    deletion_generation=new_generation,
                )
                # 写 delete_thread Job（幂等：job_type+thread+generation 唯一）
                from backend.conversation.persistence import jobs as jobs_repo

                await jobs_repo.insert_job(
                    session,
                    job_id=uuid4(),
                    job_type="delete_thread",
                    thread_id=thread_id,
                    user_id=user_id,
                    deletion_generation=new_generation,
                )


async def _write_source_deletion_outbox(
    session: AsyncSession,
    *,
    thread_id: UUID,
    user_id: UUID,
    deletion_generation: int,
) -> None:
    """C7（评审 / §8.6 步骤 1）：只为**已投递过 Memory Evidence 的来源**
    写入删除 Outbox（第三轮评审 P2：cancelled/failed、从未投递过的消息
    不产生删除事件，避免向 Memory 域发送无意义的 SourceDeletedEvent）。

    已投递来源 = conversation_outbox 中 event_type='conversation_evidence'
    且 status='delivered' 的行携带的 message_ids（§8.6 步骤 1"每个已投递来源"）。
    source_ref 与 Reader 返回值严格一致（conversation:{thread_id}:message:{message_id}），
    event_id 由 deletion_generation + source_ref 稳定派生（幂等重放），
    aggregate_id 使用 deletion_generation 供 delete_thread Job 等待语义（R3/S3）。
    """
    from hashlib import sha256 as _sha256
    from uuid import NAMESPACE_URL, uuid5

    from backend.conversation.persistence import outbox as outbox_repo

    result = await session.execute(
        text(
            "SELECT DISTINCT unnest(message_ids) AS message_id "
            "FROM conversation.conversation_outbox "
            "WHERE thread_id = :thread_id "
            "  AND event_type = 'conversation_evidence' "
            "  AND status = 'delivered' "
            "ORDER BY message_id"
        ),
        {"thread_id": thread_id},
    )
    message_ids = [row["message_id"] for row in result.mappings()]
    for message_id in message_ids:
        stable = _sha256(f"{thread_id}:{deletion_generation}:{message_id}".encode()).hexdigest()
        event_id = uuid5(NAMESPACE_URL, f"conversation:source-deleted:{stable}")
        idempotency_key = f"source-deleted:{thread_id}:{deletion_generation}:{message_id}"
        await outbox_repo.insert_outbox(
            session,
            event_id=event_id,
            event_type="memory.source_deleted",
            aggregate_type="conversation_thread",
            aggregate_id=str(deletion_generation),
            aggregate_version=deletion_generation,
            idempotency_key=idempotency_key,
            user_id=user_id,
            thread_id=thread_id,
            turn_id=None,
            message_ids=[message_id],
            source_checkpoint_id=None,
            trigger=None,
            topic_hints=[],
            graph_node_hints=[],
        )
