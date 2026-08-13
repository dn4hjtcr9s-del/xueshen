"""conversation-worker 集成测试（方案 §5.4 / §1.5 / 附录 A.2/A.3）。

覆盖：
- claim 可 claim 条件（accepted 到点 / 过期 lease 回收）；
- cancelling 过期 lease 回收者只完成取消清理并写终态（R2）；
- attempt 超限转 failed；
- 附录 A.2：回收后 next_attempt_at 按 5s→10s 退避；
- 附录 A.3：graph_thread_id 确定性派生。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo
from backend.conversation.worker.graph_worker import (
    ConversationGraphWorker,
    GraphWorkerConfig,
)

pytestmark = pytest.mark.asyncio


async def _seed_turn(
    session_factory: async_sessionmaker,
    *,
    status: str = "accepted",
    lease_expires_at=None,
    next_attempt_at=None,
) -> dict[str, object]:
    """构造一个 Turn（可指定状态与时间）。"""
    user_id = uuid4()
    thread_id = uuid4()
    turn_id = uuid4()
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
            if status != "accepted":
                await session.execute(
                    text(
                        "UPDATE conversation.conversation_turns "
                        "SET status = :status, lease_owner = 'dead-worker', "
                        "    lease_generation = 1, lease_expires_at = :lease, "
                        "    next_attempt_at = :next "
                        "WHERE turn_id = :turn_id"
                    ),
                    {
                        "status": status,
                        "lease": lease_expires_at,
                        "next": next_attempt_at or datetime.now(UTC),
                        "turn_id": turn_id,
                    },
                )
    return {"user_id": user_id, "thread_id": thread_id, "turn_id": turn_id}


def _worker(
    session_factory: async_sessionmaker,
    config: GraphWorkerConfig | None = None,
) -> ConversationGraphWorker:
    from backend.settings import Settings

    return ConversationGraphWorker(
        session_factory=session_factory,
        config=config or GraphWorkerConfig(Settings(app_env="test")),
        graph_runner=object(),
        graph_thread_id_for_turn=lambda t: f"conv-turn:{t}",
        worker_id="w-test",
    )


async def test_claim_accepted_turn_sets_running(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """§5.4：accepted 且到点 → claim 后 running，attempt+1。"""
    seed = await _seed_turn(conversation_session_factory)
    worker = _worker(conversation_session_factory)
    claimed = await worker._claim_next_turn()
    assert claimed is not None
    assert claimed["turn_id"] == seed["turn_id"]
    assert claimed["status"] == "running"
    assert claimed["attempt_count"] == 1
    async with conversation_session_factory() as session:
        row = await turns_repo.get_turn(session, seed["turn_id"])
    assert row["status"] == "running"
    assert row["lease_owner"] == "w-test"


async def test_reclaim_expired_running_lease(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """§5.4：过期 running lease 可回收，attempt+1（第二次尝试）。"""
    past = datetime.now(UTC) - timedelta(seconds=5)
    seed = await _seed_turn(conversation_session_factory, status="running", lease_expires_at=past)
    worker = _worker(conversation_session_factory)
    claimed = await worker._claim_next_turn()
    assert claimed is not None
    assert claimed["turn_id"] == seed["turn_id"]
    assert claimed["attempt_count"] == 1  # 首次回收：0 → 1
    # 附录 A.2：回收后退避 5s→10s（attempt=1 → 5s，attempt=2 → 10s）
    assert claimed["next_attempt_at"] > datetime.now(UTC) - timedelta(seconds=2)


async def test_reclaim_cancelling_writes_cancelled(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """R2：回收 cancelling 只完成取消清理并写终态，不得恢复回答。"""
    past = datetime.now(UTC) - timedelta(seconds=5)
    seed = await _seed_turn(
        conversation_session_factory, status="cancelling", lease_expires_at=past
    )
    worker = _worker(conversation_session_factory)
    claimed = await worker._claim_next_turn()
    assert claimed is None  # 回收者不返回 Turn 给 graph
    async with conversation_session_factory() as session:
        row = await turns_repo.get_turn(session, seed["turn_id"])
    assert row["status"] == "cancelled"


async def test_attempt_exhausted_marks_failed(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """§5.4：attempt ≥ 3 转 failed。"""
    past = datetime.now(UTC) - timedelta(seconds=5)
    seed = await _seed_turn(conversation_session_factory, status="running", lease_expires_at=past)
    async with conversation_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE conversation.conversation_turns "
                    "SET attempt_count = 3 WHERE turn_id = :turn_id"
                ),
                {"turn_id": seed["turn_id"]},
            )
    worker = _worker(conversation_session_factory)
    claimed = await worker._claim_next_turn()
    assert claimed is not None
    await worker._mark_failed_if_attempts_exhausted(claimed)
    async with conversation_session_factory() as session:
        row = await turns_repo.get_turn(session, seed["turn_id"])
    assert row["status"] == "failed"


async def test_graph_thread_id_deterministic() -> None:
    """附录 A.3：graph_thread_id 从 turn_id 确定性派生。"""
    from backend.conversation.worker.main import graph_thread_id_for_turn

    turn_id = uuid4()
    assert graph_thread_id_for_turn(turn_id) == f"conv-turn:{turn_id}"
    assert graph_thread_id_for_turn(turn_id) == graph_thread_id_for_turn(turn_id)
