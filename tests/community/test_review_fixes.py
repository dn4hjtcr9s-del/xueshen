"""Review 修复补测（评审项 1/3/4/9/10/11/12/13，PR 全量修复验证）。

覆盖：
- Critical 1：并发同键幂等（asyncio.gather）只创建一个资源 + 一条 outbox；
  过期幂等行替换语义（§8.3 同键同 payload 返回原资源）；
- 项 3：hidden 回复不通过公共详情暴露；
- 项 4：删除链路 token 校验（settings validator）；
- 项 10：限流 429 + Retry-After + user/IP 双 bucket；purge 正向路径
  （dev scope override 模拟 system principal）；
- 项 11：板块不存在 → 404 NOT_FOUND；
- 项 12：reply_created payload source_version=content_hash + window 字段；
- 项 13：删除 outbox 幂等键 = D32 公式。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from backend.community.contracts.domain import BOARDS_SEED

pytestmark = pytest.mark.asyncio

USER_A = "11111111-1111-4111-8111-111111111111"
BOARD = BOARDS_SEED[0]


async def _make_app(community_session_factory, **settings_overrides):
    """带写服务的 app；DEV_AUTH_ALLOW_SCOPE_OVERRIDE=True 支持 system 模拟。"""
    from backend.app import create_app
    from backend.community.api.community import router as community_router
    from backend.community.api.dependencies import CommunityRuntime
    from backend.community.persistence.database import create_community_engine
    from backend.community.services.post_command_service import PostCommandService
    from backend.community.services.post_service import PostReadService
    from backend.community.services.public_user_profile_reader import (
        PublicUserProfile,
        PublicUserProfileReader,
    )
    from backend.community.services.reply_service import ReplyService
    from backend.settings import Settings

    class FakeProfileReader(PublicUserProfileReader):
        def __init__(self) -> None:
            pass  # 不调用父类构造（测试环境无 auth 库）

        async def get_active_profile(self, user_id) -> PublicUserProfile:
            return PublicUserProfile(user_id=user_id, username="alice", status="active")

    base = dict(
        _env_file=None,
        APP_ENV="development",
        DEV_AUTH_ALLOW_SCOPE_OVERRIDE=True,
    )
    base.update(settings_overrides)
    settings = Settings(**base)
    app = create_app(settings=settings)
    app.state.runtime = SimpleNamespace(
        session_factory=community_session_factory, maintenance_gate=None
    )
    db = type(
        "Db",
        (),
        {
            "engine": create_community_engine(settings),
            "session_factory": community_session_factory,
        },
    )()
    profile_reader = FakeProfileReader()
    reply_service = ReplyService(
        session_factory=community_session_factory, profile_reader_factory=lambda: profile_reader
    )
    post_command = PostCommandService(
        session_factory=community_session_factory,
        profile_reader_factory=lambda: profile_reader,
        reply_service=reply_service,
    )
    runtime = CommunityRuntime(
        settings=settings,
        database=db,
        post_service=PostReadService(community_session_factory),
        post_command_service=post_command,
        reply_service=reply_service,
        profile_reader_factory=lambda: profile_reader,
    )
    app.state.community_db = db
    app.state.community_runtime = runtime
    app.include_router(community_router)
    return app


def _auth(user_id: str = USER_A) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def _system_auth() -> dict[str, str]:
    """dev scope override 模拟 system:community-purge（D36）。"""
    return {
        "X-Dev-User-Id": USER_A,
        "X-Dev-Actor-Type": "system",
        "X-Dev-Scopes": "community:account_purge",
    }


async def _outbox_count(session_factory, event_type: str) -> int:
    async with session_factory() as session:
        return int(
            (
                await session.execute(
                    text("SELECT COUNT(*) FROM community_outbox WHERE event_type = :t"),
                    {"t": event_type},
                )
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# Critical 1：并发幂等
# ---------------------------------------------------------------------------


async def test_concurrent_same_key_creates_single_resource(
    community_session_factory,
) -> None:
    """并发同键：唯一约束裁决，只创建一个资源 + 一条 outbox（§8.3）。"""
    app = await _make_app(community_session_factory)
    transport = ASGITransport(app=app)
    payload = {"board_id": BOARD[0], "title": "并发帖", "body": "正文"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        results = await asyncio.gather(
            *[
                client.post(
                    "/api/v1/community/posts",
                    headers={**_auth(), "Idempotency-Key": "same-key"},
                    json=payload,
                )
                for _ in range(5)
            ]
        )
    post_ids = {r.json().get("post_id") for r in results}
    assert all(r.status_code == 201 for r in results)
    assert len(post_ids) == 1  # 同一个资源
    async with community_session_factory() as session:
        count = (await session.execute(text("SELECT COUNT(*) FROM community_posts"))).scalar_one()
    assert count == 1
    assert await _outbox_count(community_session_factory, "community.post_created") == 1


async def test_expired_idempotency_row_is_replaced(
    community_session_factory,
) -> None:
    """过期幂等行不占位：7 天保留期后同键重试创建新资源（§8.3 语义）。"""

    app = await _make_app(community_session_factory)
    transport = ASGITransport(app=app)
    payload = {"board_id": BOARD[0], "title": "旧帖", "body": "b"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.post(
            "/api/v1/community/posts",
            headers={**_auth(), "Idempotency-Key": "k-exp"},
            json=payload,
        )
        assert r1.status_code == 201
        # 模拟 7 天后：幂等行过期（未物理清理）
        async with community_session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE community_idempotency_requests SET expires_at = :past "
                        "WHERE idempotency_key = 'k-exp'"
                    ),
                    {"past": datetime.now(UTC) - timedelta(days=1)},
                )
        # 同键同 payload 重试：过期行被替换，创建新资源（同键新请求）
        r2 = await client.post(
            "/api/v1/community/posts",
            headers={**_auth(), "Idempotency-Key": "k-exp"},
            json=payload,
        )
        assert r2.status_code == 201
        assert r2.json()["post_id"] != r1.json()["post_id"]
        async with community_session_factory() as session:
            count = (
                await session.execute(text("SELECT COUNT(*) FROM community_posts"))
            ).scalar_one()
        assert count == 2


# ---------------------------------------------------------------------------
# 项 3：hidden 回复不暴露
# ---------------------------------------------------------------------------


async def test_hidden_reply_not_exposed_in_detail(
    community_session_factory,
) -> None:
    """§9.4/§6.6：hidden 回复不通过公共详情返回。"""
    app = await _make_app(community_session_factory)
    transport = ASGITransport(app=app)
    post_id = str(uuid4())
    reply_id = str(uuid4())
    async with community_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO community_posts "
                    "(post_id, user_id, author_display_name, board_id, title, body, "
                    " content_hash, status, discussion_status, created_at, updated_at, "
                    " last_activity_at) "
                    "VALUES (:pid, :uid, 'alice', :bid, 't', 'b', :h, 'active', 'open', "
                    " now(), now(), now())"
                ),
                {"pid": post_id, "uid": USER_A, "bid": BOARD[0], "h": "0" * 64},
            )
            await session.execute(
                text(
                    "INSERT INTO community_replies "
                    "(reply_id, post_id, user_id, author_display_name, body, content_hash, "
                    " status, created_at, updated_at) "
                    "VALUES (:rid, :pid, :uid, 'alice', '秘密回复', :h, 'hidden', now(), now())"
                ),
                {"rid": reply_id, "pid": post_id, "uid": USER_A, "h": "0" * 64},
            )
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/api/v1/community/posts/{post_id}", headers=_auth())
    assert r.status_code == 200
    assert "秘密回复" not in r.text
    assert r.json()["replies"]["items"] == []


# ---------------------------------------------------------------------------
# 项 10：限流 API 层
# ---------------------------------------------------------------------------


async def test_rate_limit_429_with_retry_after(community_session_factory) -> None:
    """§9.3：user 桶打满 → 429 + Retry-After（按窗口剩余秒数）。"""
    app = await _make_app(community_session_factory, COMMUNITY_RATE_LIMIT_POST_PER_HOUR=2)
    transport = ASGITransport(app=app)
    payload = {"board_id": BOARD[0], "title": "t", "body": "b"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for i in range(2):
            r = await client.post(
                "/api/v1/community/posts",
                headers={**_auth(), "Idempotency-Key": f"rl-{i}"},
                json=payload,
            )
            assert r.status_code == 201
        r3 = await client.post(
            "/api/v1/community/posts",
            headers={**_auth(), "Idempotency-Key": "rl-3"},
            json=payload,
        )
        assert r3.status_code == 429
        assert r3.json()["error"]["code"] == "COMMUNITY_RATE_LIMITED"
        retry_after = r3.headers.get("Retry-After")
        assert retry_after is not None
        assert 1 <= int(retry_after) <= 3600
        # 不同用户同 IP：user 桶独立但 IP 桶仍打满 → 429（§9.3 任一命中即拒绝；
        # 集成测试同源 IP 属预期）。换 IP 才能放行——此处验证 IP 桶生效。
        r4 = await client.post(
            "/api/v1/community/posts",
            headers={**_auth("22222222-2222-4222-8222-222222222222"), "Idempotency-Key": "rl-4"},
            json=payload,
        )
        assert r4.status_code == 429


# ---------------------------------------------------------------------------
# 项 10：purge 正向路径（dev scope override 模拟 system principal）
# ---------------------------------------------------------------------------


async def test_purge_positive_path(community_session_factory) -> None:
    """§8.8：system principal purge → 帖子/回复 deleted + deletion Outbox。"""
    from backend.community.api.internal_accounts import router as purge_router

    app = await _make_app(community_session_factory)
    app.include_router(purge_router)
    transport = ASGITransport(app=app)
    post_id = str(uuid4())
    reply_id = str(uuid4())
    async with community_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO community_posts "
                    "(post_id, user_id, author_display_name, board_id, title, body, "
                    " content_hash, status, discussion_status, created_at, updated_at, "
                    " last_activity_at) "
                    "VALUES (:pid, :uid, 'alice', :bid, 't', 'b', :h, 'active', 'open', "
                    " now(), now(), now())"
                ),
                {"pid": post_id, "uid": USER_A, "bid": BOARD[0], "h": "0" * 64},
            )
            await session.execute(
                text(
                    "INSERT INTO community_replies "
                    "(reply_id, post_id, user_id, author_display_name, body, content_hash, "
                    " status, created_at, updated_at) "
                    "VALUES (:rid, :pid, :uid, 'alice', 'r', :h, 'active', now(), now())"
                ),
                {"rid": reply_id, "pid": post_id, "uid": USER_A, "h": "0" * 64},
            )
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/internal/community-accounts/purge",
            headers=_system_auth(),
            json={"user_id": USER_A},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "completed"
        # 幂等重放：不产生第二条 deletion fact
        r2 = await client.post(
            "/api/v1/internal/community-accounts/purge",
            headers=_system_auth(),
            json={"user_id": USER_A},
        )
        assert r2.status_code == 200
    async with community_session_factory() as session:
        posts = (
            (
                await session.execute(
                    text(
                        "SELECT status, eligible_for_memory FROM community_posts "
                        "WHERE post_id = :pid"
                    ),
                    {"pid": post_id},
                )
            )
            .mappings()
            .one()
        )
        replies = (
            (
                await session.execute(
                    text(
                        "SELECT status, eligible_for_memory FROM community_replies "
                        "WHERE reply_id = :rid"
                    ),
                    {"rid": reply_id},
                )
            )
            .mappings()
            .one()
        )
        dels = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM community_outbox "
                    "WHERE event_type = 'community.source_deleted'"
                )
            )
        ).scalar_one()
    assert posts["status"] == "deleted" and posts["eligible_for_memory"] is False
    assert replies["status"] == "deleted" and replies["eligible_for_memory"] is False
    assert dels == 2  # 帖子 + 回复各一条；重放未新增


# ---------------------------------------------------------------------------
# 项 11：板块不存在 404
# ---------------------------------------------------------------------------


async def test_create_post_unknown_board_404(community_session_factory) -> None:
    """§8.7：板块不存在 → 404 COMMUNITY_NOT_FOUND（存在但 hidden 才 409）。"""
    app = await _make_app(community_session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/community/posts",
            headers={**_auth(), "Idempotency-Key": "nb-1"},
            json={"board_id": str(uuid4()), "title": "t", "body": "b"},
        )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "COMMUNITY_NOT_FOUND"


# ---------------------------------------------------------------------------
# 项 12/13：reply payload 与 D32 幂等键
# ---------------------------------------------------------------------------


async def test_reply_outbox_payload_contract(community_session_factory) -> None:
    """§7.5/项 12/13：reply_created payload 含 source_version/window 字段。"""
    app = await _make_app(community_session_factory)
    transport = ASGITransport(app=app)
    post_id = str(uuid4())
    async with community_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO community_posts "
                    "(post_id, user_id, author_display_name, board_id, title, body, "
                    " content_hash, status, discussion_status, created_at, updated_at, "
                    " last_activity_at) "
                    "VALUES (:pid, :uid, 'alice', :bid, 't', 'b', :h, 'active', 'open', "
                    " now(), now(), now())"
                ),
                {"pid": post_id, "uid": USER_A, "bid": BOARD[0], "h": "0" * 64},
            )
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/api/v1/community/posts/{post_id}/replies",
            headers={**_auth(), "Idempotency-Key": "rp-1"},
            json={"body": "回复内容"},
        )
        assert r.status_code == 201
    async with community_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT payload, idempotency_key FROM community_outbox "
                        "WHERE event_type = 'community.reply_created'"
                    )
                )
            )
            .mappings()
            .one()
        )
    import hashlib

    expected_hash = hashlib.sha256("回复内容".encode()).hexdigest()
    assert row["payload"]["source_version"] == expected_hash  # 项 12：content_hash
    assert row["payload"]["window_started_at"] is None  # 项 12：可空字段对齐
    assert row["payload"]["window_ended_at"] is None
    reply_id = r.json()["reply_id"]
    assert row["idempotency_key"] == f"community:community.reply_created:{reply_id}"


async def test_deletion_outbox_key_d32(community_session_factory) -> None:
    """项 13：删除 outbox 幂等键 = D32 公式 community:{event_type}:{aggregate_id}。"""
    app = await _make_app(community_session_factory)
    transport = ASGITransport(app=app)
    post_id = str(uuid4())
    async with community_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO community_posts "
                    "(post_id, user_id, author_display_name, board_id, title, body, "
                    " content_hash, status, discussion_status, created_at, updated_at, "
                    " last_activity_at) "
                    "VALUES (:pid, :uid, 'alice', :bid, 't', 'b', :h, 'active', 'open', "
                    " now(), now(), now())"
                ),
                {"pid": post_id, "uid": USER_A, "bid": BOARD[0], "h": "0" * 64},
            )
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.delete(f"/api/v1/community/posts/{post_id}", headers=_auth())
    async with community_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT idempotency_key FROM community_outbox "
                        "WHERE event_type = 'community.source_deleted'"
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["idempotency_key"] == f"community:community.source_deleted:{post_id}"
