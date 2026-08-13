"""conversation-outbox-publisher：向 Memory API 投递 Outbox（方案 §5.4 / §7.5 / §16）。

- claim/续租/投递后状态更新/dead-letter 全部携带 lease_generation fencing；
- 幂等键稳定：conversation-evidence:{turn_id}:{source_checkpoint_id}（§16.3）；
- 可重试失败按附录 A.1 退避（cap 1800s），永久错误直接 dead_letter；
- 显式记忆的快速投递由 Graph 内 MEMORYACK 节点执行；Publisher 只负责
  Outbox 投递与 memory.submission 状态更新（§5.4 / §16.4）。
- 不修改消息正文、Answer、Thread 状态、Turn 主生命周期或 Checkpoint（§7.5）。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.contracts.events import TurnEventWrite
from backend.conversation.persistence import outbox as outbox_repo
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo
from backend.conversation.persistence.event_writer import TurnEventWriter
from backend.memory.client import MemoryClient
from backend.settings import Settings

logger = logging.getLogger("conversation.publisher")


class OutboxPublisherConfig:
    def __init__(self, settings: Settings) -> None:
        self.poll_seconds = settings.conversation_outbox_poll_seconds
        self.lease_seconds = settings.conversation_outbox_lease_seconds
        self.max_attempts = settings.conversation_outbox_max_attempts


class ConversationOutboxPublisher:
    """Outbox 轮询投递器（独立进程边界，§5.4）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: OutboxPublisherConfig,
        memory_client: MemoryClient,
        turn_event_writer: TurnEventWriter,
        worker_id: str,
        source_delete_client: MemoryClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._memory_client = memory_client
        self._source_delete_client = source_delete_client or memory_client
        self._turn_event_writer = turn_event_writer
        self.worker_id = worker_id
        self._stop = asyncio.Event()
        self._rng = random.Random()

    def install_signal_handlers(self) -> None:
        import signal

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass

    async def run_forever(self) -> None:
        logger.info("Outbox publisher 启动: %s", self.worker_id)
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except Exception:
                logger.exception("publisher 轮询周期异常")
            await asyncio.sleep(self._config.poll_seconds)

    async def _poll_once(self) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                rows = await outbox_repo.claim_outbox(
                    session,
                    worker_id=self.worker_id,
                    lease_seconds=self._config.lease_seconds,
                    limit=20,
                )
        for row in rows:
            await self._deliver(row)

    async def _deliver(self, row: dict[str, Any]) -> None:
        """投递单条 Outbox（fencing 写回；事件语义 §7.5）。"""
        if row["event_type"] == "conversation_evidence":
            await self._deliver_evidence(row)
        elif row["event_type"] == "memory.source_deleted":
            await self._deliver_source_deletion(row)
        else:
            await self._dead_letter(row, "UNKNOWN_EVENT_TYPE")

    async def _deliver_evidence(self, row: dict[str, Any]) -> None:
        """提交 ConversationEvidence（§16.3/§16.4 / 第三轮评审 P2）。

        投递前检查 thread.status='active'（§8.6 步骤 2 fencing）：线程已
        deleting/deleted 时不投递，避免删除后继续形成新的 Memory Evidence。
        """
        idempotency_key = row["idempotency_key"]
        async with self._session_factory() as session:
            thread = await threads_repo.get_thread(session, row["thread_id"])
        if thread is None or thread["status"] != "active":
            # 删除竞态（§8.6 步骤 2）：线程已删除/删除中 → 不投递，直接标记
            # delivered（无意义保留在 outbox）；删除 Outbox 由删除链路负责。
            async with self._session_factory() as session:
                async with session.begin():
                    await outbox_repo.mark_delivered(
                        session, row["event_id"], worker_id=self.worker_id
                    )
            return
        try:
            result = await self._memory_client.submit_conversation_evidence(
                idempotency_key=idempotency_key,
                thread_id=str(row["thread_id"]),
                message_ids=[str(mid) for mid in row["message_ids"]],
                trigger=row["trigger"] or "turn_boundary",
                checkpoint_id=row["source_checkpoint_id"],
                topic_hints=list(row["topic_hints"]),
                graph_node_hints=list(row["graph_node_hints"]),
            )
        except Exception as exc:
            await self._handle_delivery_failure(row, exc)
            return
        operation_id = getattr(result, "operation_id", None)
        # 投递后状态更新 + memory.submission 事件（同一事务，fencing 写回）
        async with self._session_factory() as session:
            async with session.begin():
                delivered = await outbox_repo.mark_delivered(
                    session, row["event_id"], worker_id=self.worker_id
                )
                if not delivered:
                    return  # 失租：不覆盖新 owner 状态
                if row["turn_id"] is not None:
                    await turns_repo.update_memory_submission(
                        session,
                        row["turn_id"],
                        status="accepted",
                        operation_id=operation_id,
                    )
                    await self._turn_event_writer.append(
                        session,
                        write=TurnEventWrite(
                            turn_id=row["turn_id"],
                            event_type="memory.submission",
                            request_id=row.get("idempotency_key", ""),
                            run_id=row.get("idempotency_key", ""),
                            payload={
                                "status": "accepted",
                                "operation_id": (str(operation_id) if operation_id else None),
                            },
                        ),
                    )

    async def _deliver_source_deletion(self, row: dict[str, Any]) -> None:
        """投递 SourceDeletedEvent（§8.6 步骤 4 / 评审 C7）。

        source_ref 与 Reader 返回值严格一致（§8.6 #5）：从 Outbox message_ids
        构造 conversation:{thread_id}:message:{message_id}；一条 Outbox 可能
        携带多个 message_id（目前删除链路每个消息一条 Outbox）。
        """
        message_ids = row.get("message_ids") or []
        if not message_ids:
            await self._dead_letter(row, "SOURCE_DELETION_MISSING_MESSAGE_ID")
            return
        source_ref = f"conversation:{row['thread_id']}:message:{message_ids[0]}"
        try:
            # §8.2 / 评审 C7：source deletion 使用独立 system principal
            # （CONVERSATION_SOURCE_DELETE_SERVICE_TOKEN，memory:source_delete scope），
            # 不复用 Memory Agent token（§20.3：不得互相复用权限）。
            submit = getattr(self._source_delete_client, "submit_source_deletion", None)
            if submit is None:
                await self._dead_letter(row, "SOURCE_DELETION_CLIENT_MISSING")
                return
            await submit(
                idempotency_key=row["idempotency_key"],
                user_id=row["user_id"],
                source_ref=source_ref,
                source_version=row.get("source_checkpoint_id"),
                event_id=row["event_id"],
            )
        except Exception as exc:
            await self._handle_delivery_failure(row, exc)
            return
        async with self._session_factory() as session:
            async with session.begin():
                await outbox_repo.mark_delivered(session, row["event_id"], worker_id=self.worker_id)

    async def _handle_delivery_failure(self, row: dict[str, Any], exc: Exception) -> None:
        """错误分类（附录 A.1）：永久错误直接 dead_letter，可重试按退避。"""
        from backend.memory.client import MemoryClientError

        code = type(exc).__name__
        permanent = False
        if isinstance(exc, MemoryClientError):
            permanent = exc.http_status < 500 and exc.http_status not in (408, 409, 425, 429)
            code = exc.code
        async with self._session_factory() as session:
            async with session.begin():
                if permanent or int(row["attempt_count"]) >= self._config.max_attempts:
                    await outbox_repo.mark_dead_letter(
                        session, row["event_id"], worker_id=self.worker_id, error_code=code
                    )
                    await self._notify_turn_memory_failed(row)
                else:
                    await outbox_repo.mark_retry_wait(
                        session,
                        row["event_id"],
                        worker_id=self.worker_id,
                        error_code=code,
                        rng=self._rng,
                    )
                    await self._notify_turn_memory_retrying(row)

    async def _dead_letter(self, row: dict[str, Any], code: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await outbox_repo.mark_dead_letter(
                    session, row["event_id"], worker_id=self.worker_id, error_code=code
                )
                await self._notify_turn_memory_failed(row)

    async def _notify_turn_memory_retrying(self, row: dict[str, Any]) -> None:
        if row["turn_id"] is None:
            return
        async with self._session_factory() as session:
            async with session.begin():
                await turns_repo.update_memory_submission(
                    session, row["turn_id"], status="retrying"
                )
                await self._turn_event_writer.append(
                    session,
                    write=TurnEventWrite(
                        turn_id=row["turn_id"],
                        event_type="memory.submission",
                        request_id=row.get("idempotency_key", ""),
                        run_id=row.get("idempotency_key", ""),
                        payload={"status": "retrying", "operation_id": None},
                    ),
                )

    async def _notify_turn_memory_failed(self, row: dict[str, Any]) -> None:
        if row["turn_id"] is None:
            return
        async with self._session_factory() as session:
            async with session.begin():
                await turns_repo.update_memory_submission(session, row["turn_id"], status="failed")
                await self._turn_event_writer.append(
                    session,
                    write=TurnEventWrite(
                        turn_id=row["turn_id"],
                        event_type="memory.submission",
                        request_id=row.get("idempotency_key", ""),
                        run_id=row.get("idempotency_key", ""),
                        payload={"status": "failed", "operation_id": None},
                    ),
                )
