"""ActivityPublisher 状态机测试（方案 §15.1，PR-D 纵切）。

覆盖：
- claim/写回 fencing（lease_owner + status='processing' 双条件）；
- evidence 删除竞态：非 active → delivered + skipped_source_deleted（§11.3）；
- 板块缺失/非 active → dead_letter + 稳定错误码（§10.2/D22），不调用 Memory；
- 重试分类（§12.2）：5xx 退避、401/403 永久失败、达到上限 dead_letter；
- feature flag 关闭：evidence 保持 pending 不消耗 attempt_count（§10.1/D7）；
- source deletion 使用 source_system=activity（§11.2）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from backend.community.contracts.domain import BOARDS_SEED
from backend.community.persistence import outbox as outbox_repo
from backend.community.services.activity_publisher import (
    ActivityPublisher,
    ActivityPublisherConfig,
)
from backend.settings import Settings

pytestmark = pytest.mark.asyncio

USER = "11111111-1111-4111-8111-111111111111"
BOARD = BOARDS_SEED[0]


class FakeMemoryClient:
    """MemoryClient 协议 fake：记录调用、按场景抛错。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    async def submit_activity_evidence(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append({"kind": "evidence", **kwargs})
        return type("R", (), {"operation_id": uuid4()})()

    async def submit_source_deletion(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append({"kind": "deletion", **kwargs})
        return {}


def _settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        APP_ENV="test",
        COMMUNITY_V2_ENABLED=True,
        COMMUNITY_PUBLISHER_ENABLED=True,
        COMMUNITY_MEMORY_SUBMIT_ENABLED=True,
        COMMUNITY_SOURCE_DELETION_ENABLED=True,
        COMMUNITY_OUTBOX_POLL_SECONDS=0.01,
        COMMUNITY_OUTBOX_LEASE_SECONDS=60,
        COMMUNITY_OUTBOX_MAX_ATTEMPTS=3,
        COMMUNITY_OUTBOX_BATCH_SIZE=50,
    )
    base.update(overrides)
    return Settings(**base)


def _publisher(session_factory, settings: Settings, client: FakeMemoryClient):
    return ActivityPublisher(
        session_factory=session_factory,
        config=ActivityPublisherConfig(settings),
        memory_client=client,
        source_delete_client=client,
        agent_token_factory=lambda subj, delegated, scopes: "fake-token",
        worker_id="test-publisher",
    )


async def _enqueue(
    session_factory,
    *,
    event_type: str,
    payload: dict,
    user_id: str = USER,
    post_id: str | None = None,
) -> str:
    event_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await outbox_repo.insert_event(
                session,
                event_id=event_id,
                event_type=event_type,
                aggregate_type="post" if "post" in event_type else "reply",
                aggregate_id=post_id or str(uuid4()),
                user_id=user_id,
                payload=payload,
                idempotency_key=f"community:{event_type}:{event_id}",
            )
    return str(event_id)


async def _outbox_state(session_factory, event_id: str) -> dict:
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    text("SELECT * FROM community_outbox WHERE event_id = :eid"), {"eid": event_id}
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _seed_post(session_factory, *, status="active", board_status="active") -> str:
    post_id = str(uuid4())
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO community_posts "
                    "(post_id, user_id, author_display_name, board_id, title, body, "
                    " content_hash, status, discussion_status, eligible_for_memory, "
                    " created_at, updated_at, last_activity_at) "
                    "VALUES (:pid, :uid, 'alice', :bid, 't', 'b', :hash, :status, 'open', true, "
                    " now(), now(), now())"
                ),
                {
                    "pid": post_id,
                    "uid": USER,
                    "bid": BOARD[0],
                    "hash": "0" * 64,
                    "status": status,
                },
            )
    if board_status != "active":
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("UPDATE community_boards SET status = :s WHERE slug = 'linear-algebra'"),
                    {"s": board_status},
                )
    return post_id


def _evidence_payload(post_id: str) -> dict:
    return {
        "source_ref": f"community:post:{post_id}",
        "source_version": "0" * 64,
        "activity_type": "forum_post",
        "activity_ids": [f"post:{post_id}"],
        "content_ref": f"community:post:{post_id}",
        "aggregated_count": 1,
        "topic_hints": ["linear-algebra"],
        "graph_node_hints": [],
    }


async def test_evidence_published_with_delegated_token(
    community_session_factory,
) -> None:
    """§12.1/§10.3：evidence 成功投递 → delivered + published。"""
    post_id = await _seed_post(community_session_factory)
    event_id = await _enqueue(
        community_session_factory,
        event_type="community.post_created",
        payload=_evidence_payload(post_id),
        post_id=post_id,
    )
    client = FakeMemoryClient()
    publisher = _publisher(community_session_factory, _settings(), client)
    await publisher._poll_once()
    state = await _outbox_state(community_session_factory, event_id)
    assert state["status"] == "delivered"
    assert state["delivery_result"] == "published"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["kind"] == "evidence"
    assert call["activity_type"] == "forum_post"
    assert call["idempotency_key"] == f"community-activity:forum_post:post:{post_id}:v1"


async def test_evidence_skipped_when_source_deleted(
    community_session_factory,
) -> None:
    """§11.3 删除竞态：来源已删除 → delivered + skipped_source_deleted，不调 Memory。"""
    post_id = await _seed_post(community_session_factory, status="deleted")
    event_id = await _enqueue(
        community_session_factory,
        event_type="community.post_created",
        payload=_evidence_payload(post_id),
        post_id=post_id,
    )
    client = FakeMemoryClient()
    publisher = _publisher(community_session_factory, _settings(), client)
    await publisher._poll_once()
    state = await _outbox_state(community_session_factory, event_id)
    assert state["status"] == "delivered"
    assert state["delivery_result"] == "skipped_source_deleted"
    assert client.calls == []


async def test_board_missing_goes_dead_letter(
    community_session_factory,
) -> None:
    """§10.2/D22：板块缺失 → dead_letter + 稳定错误码，不调用 Memory。

    真实库中 posts.board_id 有 FK 约束，board 缺失在数据完整性上不可能
    （§10.2：该场景即"Community 数据完整性异常"），故以 mock 方式验证
    Publisher 的 dead-letter 分支。
    """
    from unittest.mock import AsyncMock
    from unittest.mock import patch as mock_patch

    post_id = await _seed_post(community_session_factory)
    event_id = await _enqueue(
        community_session_factory,
        event_type="community.post_created",
        payload=_evidence_payload(post_id),
        post_id=post_id,
    )
    client = FakeMemoryClient()
    publisher = _publisher(community_session_factory, _settings(), client)
    with mock_patch.object(publisher, "_read_board", new=AsyncMock(return_value=None)):
        await publisher._poll_once()
    state = await _outbox_state(community_session_factory, event_id)
    assert state["status"] == "dead_letter"
    assert state["last_error_code"] == "community_board_missing"
    assert client.calls == []


async def test_board_inactive_goes_dead_letter(
    community_session_factory,
) -> None:
    """§10.2/D22：板块 hidden → dead_letter + community_board_inactive。"""
    post_id = await _seed_post(community_session_factory, board_status="hidden")
    event_id = await _enqueue(
        community_session_factory,
        event_type="community.post_created",
        payload=_evidence_payload(post_id),
        post_id=post_id,
    )
    client = FakeMemoryClient()
    publisher = _publisher(community_session_factory, _settings(), client)
    await publisher._poll_once()
    state = await _outbox_state(community_session_factory, event_id)
    assert state["status"] == "dead_letter"
    assert state["last_error_code"] == "community_board_inactive"
    assert client.calls == []


async def test_retry_classification_and_dead_letter(
    community_session_factory,
) -> None:
    """§12.2：401 永久失败 → dead_letter；5xx 指数退避；达上限 dead_letter。"""
    from backend.memory.client import MemoryClientError

    # 永久失败（401）
    post_id = await _seed_post(community_session_factory)
    event_id = await _enqueue(
        community_session_factory,
        event_type="community.post_created",
        payload=_evidence_payload(post_id),
        post_id=post_id,
    )
    client = FakeMemoryClient()
    client.error = MemoryClientError("AUTH_REQUIRED", "401", http_status=401)
    publisher = _publisher(community_session_factory, _settings(), client)
    await publisher._poll_once()
    state = await _outbox_state(community_session_factory, event_id)
    assert state["status"] == "dead_letter"
    assert state["last_error_code"] == "AUTH_REQUIRED"

    # 5xx 可重试 → retry_wait + attempt_count +1
    post_id2 = await _seed_post(community_session_factory)
    event_id2 = await _enqueue(
        community_session_factory,
        event_type="community.post_created",
        payload=_evidence_payload(post_id2),
        post_id=post_id2,
    )
    client2 = FakeMemoryClient()
    client2.error = MemoryClientError("X", "503", http_status=503)
    publisher2 = _publisher(community_session_factory, _settings(), client2)
    await publisher2._poll_once()
    state2 = await _outbox_state(community_session_factory, event_id2)
    assert state2["status"] == "retry_wait"
    assert state2["attempt_count"] == 1
    assert state2["next_attempt_at"] > datetime.now(UTC)

    # 达到最大尝试次数 → dead_letter（同时重置可 claim 时间）
    async with community_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE community_outbox SET attempt_count = 3, "
                    "status = 'pending', next_attempt_at = now() WHERE event_id = :eid"
                ),
                {"eid": event_id2},
            )
    await publisher2._poll_once()
    state2b = await _outbox_state(community_session_factory, event_id2)
    assert state2b["status"] == "dead_letter"


async def test_flag_off_keeps_pending(community_session_factory) -> None:
    """§10.1/D7：COMMUNITY_MEMORY_SUBMIT_ENABLED=false → evidence 保持 pending。"""
    post_id = await _seed_post(community_session_factory)
    event_id = await _enqueue(
        community_session_factory,
        event_type="community.post_created",
        payload=_evidence_payload(post_id),
        post_id=post_id,
    )
    client = FakeMemoryClient()
    settings = _settings(
        COMMUNITY_MEMORY_SUBMIT_ENABLED=False, COMMUNITY_SOURCE_DELETION_ENABLED=False
    )
    publisher = _publisher(community_session_factory, settings, client)
    await publisher._poll_once()
    state = await _outbox_state(community_session_factory, event_id)
    assert state["status"] == "pending"
    assert state["attempt_count"] == 0
    assert client.calls == []


async def test_deletion_uses_activity_source_system(
    community_session_factory,
) -> None:
    """§11.2：source deletion 投递 source_system=activity、source_version=null。"""
    post_id = await _seed_post(community_session_factory)
    event_id = await _enqueue(
        community_session_factory,
        event_type="community.source_deleted",
        payload={
            "source_ref": f"community:post:{post_id}",
            "source_version": None,
            "source_system": "activity",
            "event_id": str(uuid4()),
        },
        post_id=post_id,
    )
    client = FakeMemoryClient()
    publisher = _publisher(community_session_factory, _settings(), client)
    await publisher._poll_once()
    state = await _outbox_state(community_session_factory, event_id)
    assert state["status"] == "delivered"
    call = client.calls[0]
    assert call["kind"] == "deletion"
    assert call["source_system"] == "activity"
    assert call["source_version"] is None


async def test_needs_review_is_business_success(community_session_factory) -> None:
    """§12.2：Memory 返回 needs_review → 业务成功，不重试（delivered）。"""
    post_id = await _seed_post(community_session_factory)
    event_id = await _enqueue(
        community_session_factory,
        event_type="community.post_created",
        payload=_evidence_payload(post_id),
        post_id=post_id,
    )

    class NeedsReviewClient(FakeMemoryClient):
        async def submit_activity_evidence(self, **kwargs):
            self.calls.append({"kind": "evidence", **kwargs})
            return type("R", (), {"operation_id": uuid4(), "status": "needs_review"})()

    client = NeedsReviewClient()
    publisher = _publisher(community_session_factory, _settings(), client)
    await publisher._poll_once()
    state = await _outbox_state(community_session_factory, event_id)
    assert state["status"] == "delivered"
    assert state["delivery_result"] == "published"
    assert state["attempt_count"] == 0


async def test_fencing_writeback_requires_owner(community_session_factory) -> None:
    """§7.5：写回 fencing——非 owner 写回不生效（claim CAS + 双条件）。"""
    post_id = await _seed_post(community_session_factory)
    event_id = await _enqueue(
        community_session_factory,
        event_type="community.post_created",
        payload=_evidence_payload(post_id),
        post_id=post_id,
    )
    client = FakeMemoryClient()
    publisher = _publisher(community_session_factory, _settings(), client)
    # 另一 owner 抢占 claim
    async with community_session_factory() as session:
        async with session.begin():
            rows = await outbox_repo.claim_events(
                session,
                worker_id="other-worker",
                lease_seconds=60,
                batch_size=10,
                allowed_event_types=("community.post_created",),
            )
            assert len(rows) == 1
    # 原 publisher 尝试写回（owner 不匹配）→ 不生效
    await publisher._poll_once()
    state = await _outbox_state(community_session_factory, event_id)
    assert state["status"] == "processing"
    assert state["lease_owner"] == "other-worker"
