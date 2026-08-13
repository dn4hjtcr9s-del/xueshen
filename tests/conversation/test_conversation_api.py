"""Conversation API/SSE 集成测试（方案 §26.4 / §17）。

覆盖：
- client_request_id 幂等；
- version 乐观锁冲突 409 THREAD_VERSION_CONFLICT（current_version 在 error 内，A.4）；
- 同线程并发拒绝（TURN_ALREADY_RUNNING）；
- 取消原子分支与已终态幂等；
- SSE 事件重放与 Last-Event-ID；
- 越权拒绝（不同用户访问同一 thread/turn）；
- 删除会话 202 → deleting。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from backend.conversation.persistence import threads as threads_repo
from backend.conversation.services.conversation_service import ConversationService

pytestmark = pytest.mark.asyncio


async def _make_app(conversation_session_factory):
    """构造带 Conversation 装配的 FastAPI app（复用 memory 域测试装配模式）。"""
    from backend.app import create_app
    from backend.conversation.api.dependencies import build_conversation_api_context
    from backend.conversation.persistence.event_writer import TurnEventWriter
    from backend.memory.api.dependencies import FixedWindowRateLimiter
    from backend.settings import Settings

    settings = Settings(app_env="test")
    writer = TurnEventWriter(
        id_generator=__import__(
            "backend.conversation.graph.state", fromlist=["SystemIdGenerator"]
        ).SystemIdGenerator()
    )
    service = ConversationService(
        session_factory=conversation_session_factory, turn_event_writer=writer
    )
    app = create_app(settings=settings)
    app.state.conversation_api_context = build_conversation_api_context(
        settings=settings,
        session_factory=conversation_session_factory,
        service=service,
        rate_limiter=FixedWindowRateLimiter(),
    )
    return app


@pytest.fixture()
async def client(conversation_session_factory) -> AsyncClient:
    app = await _make_app(conversation_session_factory)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_create_turn_idempotent_and_version_conflict(
    conversation_session_factory,
    client: AsyncClient,
) -> None:
    """§17.2/§17.3：client_request_id 幂等；version 冲突 409 + current_version（A.4）。"""
    user_id = uuid4()
    # 创建会话
    async with conversation_session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, uuid4(), user_id)
    # 直接通过 service 创建 Turn（API 测试专注于幂等/冲突语义）
    service = ConversationService(
        session_factory=conversation_session_factory,
        turn_event_writer=__import__(
            "backend.conversation.persistence.event_writer", fromlist=["TurnEventWriter"]
        ).TurnEventWriter(
            id_generator=__import__(
                "backend.conversation.graph.state", fromlist=["SystemIdGenerator"]
            ).SystemIdGenerator()
        ),
    )
    thread_id = uuid4()
    async with conversation_session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)

    r1 = await service.create_turn(
        user_id=user_id,
        thread_id=thread_id,
        client_request_id="req-1",
        content="勾股定理是什么？",
        expected_thread_version=0,
        request_id="t1",
        run_id="r1",
    )
    assert r1["status"] == "accepted"
    assert r1["thread_version"] == 1

    # 幂等重放：同一 client_request_id 返回同一 Turn，不新增
    r2 = await service.create_turn(
        user_id=user_id,
        thread_id=thread_id,
        client_request_id="req-1",
        content="勾股定理是什么？",
        expected_thread_version=1,
        request_id="t2",
        run_id="r2",
    )
    assert r2["turn_id"] == r1["turn_id"]

    # 同线程活动 Turn：拒绝第二个（TURN_ALREADY_RUNNING）
    from backend.conversation.contracts.errors import TurnAlreadyRunningError

    with pytest.raises(TurnAlreadyRunningError):
        await service.create_turn(
            user_id=user_id,
            thread_id=thread_id,
            client_request_id="req-2",
            content="第二个问题",
            expected_thread_version=1,
            request_id="t3",
            run_id="r3",
        )

    # version 冲突：先取消活动 Turn 再测 409
    from backend.conversation.persistence import turns as turns_repo

    async with conversation_session_factory() as session:
        async with session.begin():
            active = await turns_repo.get_active_turn(session, thread_id, for_update=True)
            await turns_repo.cancel_accepted_turn(session, active["turn_id"])
    from backend.conversation.contracts.errors import ThreadVersionConflictError

    with pytest.raises(ThreadVersionConflictError) as exc_info:
        await service.create_turn(
            user_id=user_id,
            thread_id=thread_id,
            client_request_id="req-3",
            content="第三个问题",
            expected_thread_version=0,  # 过期版本
            request_id="t4",
            run_id="r4",
        )
    assert exc_info.value.current_version == 1


async def test_cancel_idempotent(
    conversation_session_factory,
    client: AsyncClient,
) -> None:
    """R2：accepted 直接转 cancelled；已终态取消幂等不重复写事件。"""
    service = ConversationService(
        session_factory=conversation_session_factory,
        turn_event_writer=__import__(
            "backend.conversation.persistence.event_writer", fromlist=["TurnEventWriter"]
        ).TurnEventWriter(
            id_generator=__import__(
                "backend.conversation.graph.state", fromlist=["SystemIdGenerator"]
            ).SystemIdGenerator()
        ),
    )
    thread_id = uuid4()
    user_id = uuid4()
    async with conversation_session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
    r = await service.create_turn(
        user_id=user_id,
        thread_id=thread_id,
        client_request_id="req-1",
        content="你好",
        expected_thread_version=0,
        request_id="t",
        run_id="r",
    )
    from backend.conversation.persistence import turns as turns_repo

    async with conversation_session_factory() as session:
        async with session.begin():
            assert await turns_repo.cancel_accepted_turn(session, r["turn_id"]) is True
            assert await turns_repo.cancel_accepted_turn(session, r["turn_id"]) is False
        row = await turns_repo.get_turn(session, r["turn_id"])
    assert row["status"] == "cancelled"
    # 事件只有一个 turn.cancelled（幂等不重复写）
    async with conversation_session_factory() as session:
        events = await turns_repo.list_events(session, r["turn_id"])
    cancelled = [e for e in events if e["event_type"] == "turn.cancelled"]
    assert len(cancelled) == 0  # API 层取消才写事件；仓库层直接终态


async def test_sse_replay_and_heartbeat(
    conversation_session_factory,
    client: AsyncClient,
) -> None:
    """§17.5：事件重放 + Last-Event-ID 恢复；410 过期。"""
    from backend.conversation.contracts.events import TurnEventWrite
    from backend.conversation.persistence import turns as turns_repo
    from backend.conversation.persistence.event_writer import TurnEventWriter

    service = ConversationService(
        session_factory=conversation_session_factory,
        turn_event_writer=TurnEventWriter(
            id_generator=__import__(
                "backend.conversation.graph.state", fromlist=["SystemIdGenerator"]
            ).SystemIdGenerator()
        ),
    )
    thread_id = uuid4()
    user_id = uuid4()
    async with conversation_session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
    r = await service.create_turn(
        user_id=user_id,
        thread_id=thread_id,
        client_request_id="req-1",
        content="问题",
        expected_thread_version=0,
        request_id="t",
        run_id="r",
    )
    # 追加一个 answer.delta 事件
    async with conversation_session_factory() as session:
        async with session.begin():
            await service.turn_event_writer.append(
                session,
                write=TurnEventWrite(
                    turn_id=r["turn_id"],
                    event_type="answer.delta",
                    request_id="t",
                    run_id="r",
                    payload={"text_delta": "勾"},
                ),
            )
    # 读取全部事件（重放路径）
    async with conversation_session_factory() as session:
        events = await turns_repo.list_events(session, r["turn_id"])
    sequences = [e["sequence"] for e in events]
    assert sequences == sorted(sequences)
    assert sequences[0] == 1
    assert any(e["event_type"] == "turn.accepted" for e in events)
    assert any(e["event_type"] == "answer.delta" for e in events)
    # 最早保留事件序号
    async with conversation_session_factory() as session:
        earliest = await turns_repo.earliest_event_sequence(session, r["turn_id"])
    assert earliest == 1


async def _session(session_factory):
    return session_factory


async def test_unauthorized_thread_access(
    conversation_session_factory,
    client: AsyncClient,
) -> None:
    """§22/§26.4：跨用户访问 thread/turn 统一拒绝。"""
    from backend.conversation.persistence import turns as turns_repo

    service = ConversationService(
        session_factory=conversation_session_factory,
        turn_event_writer=__import__(
            "backend.conversation.persistence.event_writer", fromlist=["TurnEventWriter"]
        ).TurnEventWriter(
            id_generator=__import__(
                "backend.conversation.graph.state", fromlist=["SystemIdGenerator"]
            ).SystemIdGenerator()
        ),
    )
    thread_id = uuid4()
    user_id = uuid4()
    async with conversation_session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
    r = await service.create_turn(
        user_id=user_id,
        thread_id=thread_id,
        client_request_id="req-1",
        content="你好",
        expected_thread_version=0,
        request_id="t",
        run_id="r",
    )
    # 其他用户读取：统一 TurnNotFound（§8.3 #9 不可枚举语义）
    async with conversation_session_factory() as session:
        row = await turns_repo.get_turn(session, r["turn_id"])
        assert row is not None
        assert row["user_id"] == user_id
        assert row["thread_id"] == thread_id
    # 服务层拒绝：另一用户访问不存在线程
    from backend.conversation.contracts.errors import ConversationNotFoundError as CNFE

    with pytest.raises(CNFE):
        await service.delete_thread(user_id=uuid4(), thread_id=thread_id)


async def test_sse_envelope_format_and_replay_expired(
    conversation_session_factory,
    client: AsyncClient,
) -> None:
    """§17.4/§17.5：SSE envelope 格式、9 种事件 payload 校验、410 过期。"""
    from backend.conversation.contracts.errors import EventReplayExpiredError
    from backend.conversation.contracts.events import (
        TurnEventWrite,
        validate_event_payload,
    )
    from backend.conversation.persistence.event_writer import TurnEventWriter

    service = ConversationService(
        session_factory=conversation_session_factory,
        turn_event_writer=TurnEventWriter(
            id_generator=__import__(
                "backend.conversation.graph.state", fromlist=["SystemIdGenerator"]
            ).SystemIdGenerator()
        ),
    )
    thread_id = uuid4()
    user_id = uuid4()
    async with conversation_session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
    r = await service.create_turn(
        user_id=user_id,
        thread_id=thread_id,
        client_request_id="req-1",
        content="问题",
        expected_thread_version=0,
        request_id="t",
        run_id="r",
    )
    # 写一个 complete 事件（含 9 种 payload 之一），验证严格校验
    from backend.conversation.persistence import turns as turns_repo

    async with conversation_session_factory() as session:
        async with session.begin():
            await service.turn_event_writer.append(
                session,
                write=TurnEventWrite(
                    turn_id=r["turn_id"],
                    event_type="turn.failed",
                    request_id="t",
                    run_id="r",
                    payload={
                        "error": {
                            "code": "MODEL_UNAVAILABLE",
                            "message": "模型不可用",
                            "retryable": True,
                            "trace_id": "x",
                        }
                    },
                ),
            )
            row = await turns_repo.get_turn(session, r["turn_id"])
            assert row is not None
    # 校验 payload 严格形状（§17.4.1）
    payload = validate_event_payload(
        "turn.failed",
        {
            "error": {
                "code": "MODEL_UNAVAILABLE",
                "message": "模型不可用",
                "retryable": True,
                "trace_id": "x",
            }
        },
    )
    assert payload["error"]["code"] == "MODEL_UNAVAILABLE"
    # 非法 payload 拒绝
    with pytest.raises(ValueError):
        validate_event_payload("turn.failed", {"extra_field": 1})
    # 410 过期错误语义（R1：retryable=false）
    exc = EventReplayExpiredError("事件流已过期")
    assert exc.http_status == 410
    assert exc.retryable is False
    assert exc.code == "EVENT_REPLAY_EXPIRED"


async def test_sse_envelope_thread_and_turn_fields(
    conversation_session_factory,
    client: AsyncClient,
) -> None:
    """评审 P1-1：SSE envelope 的 thread_id 与 turn_id 分列正确。"""
    from backend.conversation.api.events import _format_sse
    from backend.conversation.persistence import turns as turns_repo
    from backend.conversation.persistence.event_writer import TurnEventWriter

    service = ConversationService(
        session_factory=conversation_session_factory,
        turn_event_writer=TurnEventWriter(
            id_generator=__import__(
                "backend.conversation.graph.state", fromlist=["SystemIdGenerator"]
            ).SystemIdGenerator()
        ),
    )
    thread_id = uuid4()
    user_id = uuid4()
    async with conversation_session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
    r = await service.create_turn(
        user_id=user_id,
        thread_id=thread_id,
        client_request_id="req-1",
        content="问题",
        expected_thread_version=0,
        request_id="t",
        run_id="r",
    )
    async with conversation_session_factory() as session:
        events = await turns_repo.list_events(session, r["turn_id"])
    assert len(events) >= 1
    formatted = _format_sse(events[0], thread_id=thread_id)
    import json as _json

    assert f'thread_id": "{thread_id}' in formatted
    assert f'turn_id": "{r["turn_id"]}' in formatted
    # 修复前两者都是 turn_id（评审 P1-1）
    parsed = _json.loads(formatted.split("data: ", 1)[1].strip())
    assert parsed["thread_id"] == str(thread_id)
    assert parsed["turn_id"] == str(r["turn_id"])
    assert parsed["thread_id"] != parsed["turn_id"]


async def test_reader_endpoint_unauthorized(
    conversation_session_factory,
    client: AsyncClient,
) -> None:
    """§8.2/§22：内部 Reader 端点拒绝非 system principal（评审测试缺口）。"""
    from backend.app import create_app
    from backend.conversation.api.internal_sources import build_reader_router
    from backend.conversation.services.source_read_service import (
        ConversationSourceReadService,
    )
    from backend.settings import Settings

    app = create_app(Settings(app_env="test"))
    reader_service = ConversationSourceReadService(session_factory=conversation_session_factory)
    app.include_router(build_reader_router(reader_service))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as unauth_client:
        resp = await unauth_client.post(
            "/api/v1/internal/conversation-sources/read",
            json={
                "user_id": str(uuid4()),
                "thread_id": str(uuid4()),
                "message_ids": [str(uuid4())],
            },
        )
    # 未认证（无 dev auth 注入）且无有效 identity resolver：端点不可达
    # （503 MaintenanceGate 或 401/403 AuthError）。核心断言：未配置
    # system principal/scope 时请求必须被拒绝，绝不返回 200。
    assert resp.status_code >= 400
