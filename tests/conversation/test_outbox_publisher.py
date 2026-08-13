"""conversation-outbox-publisher 集成测试（方案 §7.5 / 附录 A.1）。

覆盖：
- claim → 投递 → delivered（fencing 写回）；
- 可重试失败 → retry_wait + 退避（附录 A.1 公式）；
- 永久错误（4xx）直接 dead_letter；
- 失租 Publisher 不能覆盖新 owner 状态；
- memory.submission 事件由 Publisher 追加（§7.5 最小写范围）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo
from backend.conversation.persistence.event_writer import TurnEventWriter
from backend.conversation.publisher.outbox_publisher import (
    ConversationOutboxPublisher,
    OutboxPublisherConfig,
)
from backend.memory.client import MemoryClientError

pytestmark = pytest.mark.asyncio


class FakeMemoryClient:
    """Fake MemoryClient：可脚本化失败。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.failures: list[Exception | None] = []
        self.operation_id = uuid4()

    async def submit_conversation_evidence(self, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        return type("Op", (), {"operation_id": self.operation_id})()


async def _seed_outbox(
    session_factory: async_sessionmaker,
    *,
    event_type: str = "conversation_evidence",
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """构造 thread + turn + pending outbox。"""
    user_id = uuid4()
    thread_id = uuid4()
    turn_id = uuid4()
    event_id = uuid4()
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
                user_message_id=uuid4(),
                expected_thread_version=0,
                graph_thread_id=f"conv-turn:{turn_id}",
            )
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_outbox ("
                    "  event_id, event_type, aggregate_type, aggregate_id, aggregate_version,"
                    "  idempotency_key, user_id, thread_id, turn_id, message_ids,"
                    "  status, attempt_count, next_attempt_at, created_at"
                    ") VALUES ("
                    "  :event_id, :event_type, 'turn', :agg_id, 1,"
                    "  :idem, :user_id, :thread_id, :turn_id, ARRAY[:msg_id]::uuid[],"
                    "  'pending', 0, now(), now()"
                    ")"
                ),
                {
                    "event_id": event_id,
                    "event_type": event_type,
                    "agg_id": str(turn_id),
                    "idem": idempotency_key or f"evidence:{turn_id}",
                    "user_id": user_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "msg_id": uuid4(),
                },
            )
    return {"user_id": user_id, "thread_id": thread_id, "turn_id": turn_id, "event_id": event_id}


def _publisher(
    session_factory: async_sessionmaker,
    memory_client: FakeMemoryClient,
) -> ConversationOutboxPublisher:
    from backend.settings import Settings

    return ConversationOutboxPublisher(
        session_factory=session_factory,
        config=OutboxPublisherConfig(Settings(app_env="test")),
        memory_client=memory_client,  # type: ignore[arg-type]
        turn_event_writer=TurnEventWriter(
            id_generator=type("IG", (), {"new_uuid": staticmethod(uuid4)})()
        ),
        worker_id="pub-test",
    )


async def test_deliver_evidence_success(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """§7.5：投递成功 → delivered + memory_submission=accepted + 事件。"""
    seed = await _seed_outbox(conversation_session_factory)
    client = FakeMemoryClient()
    publisher = _publisher(conversation_session_factory, client)
    await publisher._poll_once()

    async with conversation_session_factory() as session:
        from backend.conversation.persistence import outbox as outbox_repo

        row = await outbox_repo.get_outbox(session, seed["event_id"])
        turn = await turns_repo.get_turn(session, seed["turn_id"])
        events = await turns_repo.list_events(session, seed["turn_id"])
    assert row["status"] == "delivered"
    assert turn["memory_submission_status"] == "accepted"
    assert any(e["event_type"] == "memory.submission" for e in events)
    assert len(client.calls) == 1
    assert client.calls[0]["idempotency_key"].startswith("evidence:")


async def test_retryable_failure_backoff(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """附录 A.1：可重试失败 → retry_wait + 退避（5s 基数）。"""
    seed = await _seed_outbox(conversation_session_factory)
    client = FakeMemoryClient()
    client.failures.append(MemoryClientError("OPENAI_TIMEOUT", "超时", http_status=503))
    publisher = _publisher(conversation_session_factory, client)
    await publisher._poll_once()

    async with conversation_session_factory() as session:
        from backend.conversation.persistence import outbox as outbox_repo

        row = await outbox_repo.get_outbox(session, seed["event_id"])
    assert row["status"] == "retry_wait"
    assert row["last_error_code"] == "OPENAI_TIMEOUT"
    assert row["next_attempt_at"] is not None


async def test_permanent_failure_dead_letter(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """附录 A.1：永久错误（4xx 契约/权限）直接 dead_letter 不排队。"""
    seed = await _seed_outbox(conversation_session_factory)
    client = FakeMemoryClient()
    client.failures.append(MemoryClientError("INVALID_PAYLOAD", "非法请求", http_status=422))
    publisher = _publisher(conversation_session_factory, client)
    await publisher._poll_once()

    async with conversation_session_factory() as session:
        from backend.conversation.persistence import outbox as outbox_repo

        row = await outbox_repo.get_outbox(session, seed["event_id"])
    assert row["status"] == "dead_letter"
    assert row["last_error_code"] == "INVALID_PAYLOAD"


async def test_fenced_publisher_cannot_overwrite(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """§7.5：失租 Publisher 不能覆盖新 owner 状态（fencing）。"""
    seed = await _seed_outbox(conversation_session_factory)
    # 模拟新 owner 已 claim 并投递完成
    async with conversation_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE conversation.conversation_outbox "
                    "SET status = 'delivered', lease_owner = 'new-owner', "
                    "    lease_generation = 5, delivered_at = now() "
                    "WHERE event_id = :event_id"
                ),
                {"event_id": seed["event_id"]},
            )
    client = FakeMemoryClient()
    publisher = _publisher(conversation_session_factory, client)
    await publisher._poll_once()

    async with conversation_session_factory() as session:
        from backend.conversation.persistence import outbox as outbox_repo

        row = await outbox_repo.get_outbox(session, seed["event_id"])
    assert row["status"] == "delivered"  # 未被覆盖
    assert row["lease_owner"] == "new-owner"


async def test_reclaim_stale_processing_outbox(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """评审 C5：崩溃遗留 processing（lease 过期）可被回收投递。"""
    seed = await _seed_outbox(conversation_session_factory)
    async with conversation_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE conversation.conversation_outbox "
                    "SET status = 'processing', lease_owner = 'dead-worker', "
                    "    lease_generation = 2, "
                    "    lease_expires_at = now() - interval '10 seconds' "
                    "WHERE event_id = :event_id"
                ),
                {"event_id": seed["event_id"]},
            )
    client = FakeMemoryClient()
    publisher = _publisher(conversation_session_factory, client)
    await publisher._poll_once()
    async with conversation_session_factory() as session:
        from backend.conversation.persistence import outbox as outbox_repo

        row = await outbox_repo.get_outbox(session, seed["event_id"])
    assert row["status"] == "delivered"
    assert len(client.calls) == 1


async def test_lease_fencing_prevents_stale_delivery_override(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """评审 C4：失租 Publisher 不能覆盖新 owner 的 delivered 状态。"""
    seed = await _seed_outbox(conversation_session_factory)
    # 模拟 claim 后（processing, owner=pub-test），随后新 owner 已投递完成
    async with conversation_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE conversation.conversation_outbox "
                    "SET status = 'processing', lease_owner = 'pub-test', "
                    "    lease_generation = 3, lease_expires_at = now() + interval '60 seconds' "
                    "WHERE event_id = :event_id"
                ),
                {"event_id": seed["event_id"]},
            )
    # 新 owner 抢先投递完成
    async with conversation_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE conversation.conversation_outbox "
                    "SET status = 'delivered', lease_owner = 'pub-new', "
                    "    lease_generation = 4, delivered_at = now() "
                    "WHERE event_id = :event_id"
                ),
                {"event_id": seed["event_id"]},
            )
    client = FakeMemoryClient()
    publisher = _publisher(conversation_session_factory, client)
    await publisher._poll_once()
    async with conversation_session_factory() as session:
        from backend.conversation.persistence import outbox as outbox_repo

        row = await outbox_repo.get_outbox(session, seed["event_id"])
    assert row["status"] == "delivered"
    assert row["lease_owner"] == "pub-new"  # 未被旧 owner 覆盖
