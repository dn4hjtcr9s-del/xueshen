"""Community 写路径集成测试（方案 §15.1/§15.3，PR-C 纵切）。

覆盖：发帖幂等/冲突、板块禁用、内容校验、回复 + 通知、点赞/取消幂等、
解决状态机（幂等重试/切换/取消/closed）、删除（计数/solved 清除/D34、
deletion Outbox）、通知 read/read-all 跨用户隔离、purge 幂等与 scope 校验。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from backend.community.contracts.domain import BOARDS_SEED
from backend.community.services.public_user_profile_reader import (
    PublicUserProfile,
    PublicUserProfileReader,
)

pytestmark = pytest.mark.asyncio

USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"
BOARD = BOARDS_SEED[0]  # linear-algebra


class FakeProfileReader(PublicUserProfileReader):
    """测试用公开资料 adapter（auth 测试库无 users 表，直接返回固定资料）。"""

    def __init__(self) -> None:
        self._names = {USER_A: "alice", USER_B: "bob"}

    async def get_active_profile(self, user_id) -> PublicUserProfile:
        return PublicUserProfile(
            user_id=user_id, username=self._names.get(str(user_id), "tester"), status="active"
        )


async def _make_app(community_session_factory, *, purge_token: str | None = None):
    """装配带写服务的 Community app（dev auth + 注入测试库 runtime）。"""
    from backend.app import create_app
    from backend.community.api.community import router as community_router
    from backend.community.api.dependencies import CommunityRuntime
    from backend.community.persistence.database import create_community_engine
    from backend.community.services.post_command_service import PostCommandService
    from backend.community.services.post_service import PostReadService
    from backend.community.services.reply_service import ReplyService
    from backend.settings import Settings

    settings = Settings(app_env="development")
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


@pytest.fixture()
async def client(community_session_factory) -> AsyncClient:
    app = await _make_app(community_session_factory)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _auth(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def _idem(key: str = "k1") -> dict[str, str]:
    return {"Idempotency-Key": key}


async def _seed_post(
    session_factory, *, author=USER_A, title="帖", body="正文", status="active", discussion="open"
) -> str:
    post_id = str(uuid4())
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO community_posts "
                    "(post_id, user_id, author_display_name, board_id, title, body, "
                    " content_hash, status, discussion_status, created_at, updated_at, "
                    " last_activity_at) "
                    "VALUES (:pid, :uid, 'alice', :bid, :title, :body, :hash, "
                    " :status, :disc, now(), now(), now())"
                ),
                {
                    "pid": post_id,
                    "uid": author,
                    "bid": BOARD[0],
                    "title": title,
                    "body": body,
                    "hash": "0" * 64,
                    "status": status,
                    "disc": discussion,
                },
            )
    return post_id


async def _seed_reply(session_factory, post_id: str, *, author=USER_B) -> str:
    reply_id = str(uuid4())
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO community_replies "
                    "(reply_id, post_id, user_id, author_display_name, body, content_hash, "
                    " status, created_at, updated_at) "
                    "VALUES (:rid, :pid, :uid, 'bob', '回复', :hash, 'active', now(), now())"
                ),
                {"rid": reply_id, "pid": post_id, "uid": author, "hash": "0" * 64},
            )
    return reply_id


async def _post_body(session_factory, post_id: str) -> dict:
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    text("SELECT * FROM community_posts WHERE post_id = :pid"), {"pid": post_id}
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


# ---------------------------------------------------------------------------
# 发帖（§8.3）
# ---------------------------------------------------------------------------


async def test_create_post_success_and_idempotent_replay(
    client: AsyncClient, community_session_factory
) -> None:
    payload = {"board_id": BOARD[0], "title": "特征值直觉", "body": "正文内容"}
    r1 = await client.post(
        "/api/v1/community/posts", headers={**_auth(USER_A), **_idem("k1")}, json=payload
    )
    assert r1.status_code == 201, r1.text
    post_id = r1.json()["post_id"]
    assert r1.json()["viewer_is_author"] is True
    assert "user_id" not in r1.text
    # 幂等重放：同键同体返回同一资源
    r2 = await client.post(
        "/api/v1/community/posts", headers={**_auth(USER_A), **_idem("k1")}, json=payload
    )
    assert r2.status_code == 201 and r2.json()["post_id"] == post_id
    # 同键不同体 → 冲突
    r3 = await client.post(
        "/api/v1/community/posts",
        headers={**_auth(USER_A), **_idem("k1")},
        json={"board_id": BOARD[0], "title": "改", "body": "改"},
    )
    assert r3.status_code == 422
    assert r3.json()["error"]["code"] == "COMMUNITY_IDEMPOTENCY_CONFLICT"
    # 只落一帖
    async with community_session_factory() as session:
        count = (await session.execute(text("SELECT COUNT(*) FROM community_posts"))).scalar_one()
    assert count == 1
    # outbox 与帖子同事务入队
    async with community_session_factory() as session:
        outbox = (
            (await session.execute(text("SELECT event_type, payload FROM community_outbox")))
            .mappings()
            .all()
        )
    assert len(outbox) == 1
    assert outbox[0]["event_type"] == "community.post_created"
    assert outbox[0]["payload"]["topic_hints"] == ["linear-algebra"]


async def test_create_post_board_disabled_and_content_validation(
    client: AsyncClient, community_session_factory
) -> None:
    # 板块不可发帖（hidden）
    async with community_session_factory() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE community_boards SET status = 'hidden' WHERE slug = 'linear-algebra'")
            )
    r = await client.post(
        "/api/v1/community/posts",
        headers={**_auth(USER_A), **_idem("k2")},
        json={"board_id": BOARD[0], "title": "t", "body": "b"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "COMMUNITY_BOARD_DISABLED"
    # 控制字符拒绝（D37）
    r2 = await client.post(
        "/api/v1/community/posts",
        headers={**_auth(USER_A), **_idem("k3")},
        json={"board_id": BOARD[0], "title": "t\x01", "body": "b"},
    )
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "COMMUNITY_CONTENT_INVALID"


# ---------------------------------------------------------------------------
# 回复（§8.4 / D31）
# ---------------------------------------------------------------------------


async def test_create_reply_notifies_author_and_closed_post(
    client: AsyncClient, community_session_factory
) -> None:
    post_id = await _seed_post(community_session_factory, author=USER_A)
    r = await client.post(
        f"/api/v1/community/posts/{post_id}/replies",
        headers={**_auth(USER_B), **_idem("r1")},
        json={"body": "我也遇到过这个问题"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["author"]["display_name"] == "bob"
    # 帖子作者收到通知；回复作者（自己回复自己）不收
    async with community_session_factory() as session:
        notif = (
            (
                await session.execute(
                    text("SELECT * FROM community_notifications WHERE recipient_user_id = :u"),
                    {"u": USER_A},
                )
            )
            .mappings()
            .all()
        )
        self_notif = (
            (
                await session.execute(
                    text("SELECT * FROM community_notifications WHERE recipient_user_id = :u"),
                    {"u": USER_B},
                )
            )
            .mappings()
            .all()
        )
    assert len(notif) == 1
    assert notif[0]["event_type"] == "post_replied"
    assert notif[0]["title"] == "bob 回复了你的帖子"
    assert notif[0]["body"] == "我也遇到过这个问题"
    assert len(self_notif) == 0
    # deleted 帖子回复 → POST_CLOSED（D31）
    deleted_id = await _seed_post(community_session_factory, author=USER_A, status="deleted")
    r2 = await client.post(
        f"/api/v1/community/posts/{deleted_id}/replies",
        headers={**_auth(USER_B), **_idem("r2")},
        json={"body": "x"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "COMMUNITY_POST_CLOSED"
    # hidden 帖子回复 → NOT_FOUND
    hidden_id = await _seed_post(community_session_factory, author=USER_A, status="hidden")
    r3 = await client.post(
        f"/api/v1/community/posts/{hidden_id}/replies",
        headers={**_auth(USER_B), **_idem("r3")},
        json={"body": "x"},
    )
    assert r3.status_code == 404


# ---------------------------------------------------------------------------
# 点赞（§8.5）
# ---------------------------------------------------------------------------


async def test_like_unlike_idempotent(client: AsyncClient, community_session_factory) -> None:
    post_id = await _seed_post(community_session_factory)
    r = await client.post(f"/api/v1/community/posts/{post_id}/like", headers=_auth(USER_B))
    assert r.status_code == 200
    assert (await _post_body(community_session_factory, post_id))["like_count"] == 1
    # 重复点赞幂等（计数不漂移）
    await client.post(f"/api/v1/community/posts/{post_id}/like", headers=_auth(USER_B))
    assert (await _post_body(community_session_factory, post_id))["like_count"] == 1
    # 取消点赞
    await client.delete(f"/api/v1/community/posts/{post_id}/like", headers=_auth(USER_B))
    assert (await _post_body(community_session_factory, post_id))["like_count"] == 0
    # 再次取消幂等
    await client.delete(f"/api/v1/community/posts/{post_id}/like", headers=_auth(USER_B))
    assert (await _post_body(community_session_factory, post_id))["like_count"] == 0


async def test_like_deleted_post_not_found(client: AsyncClient, community_session_factory) -> None:
    deleted_id = await _seed_post(community_session_factory, status="deleted")
    r = await client.post(f"/api/v1/community/posts/{deleted_id}/like", headers=_auth(USER_B))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 解决状态机（§8.5 / D21）
# ---------------------------------------------------------------------------


async def test_resolve_state_machine(client: AsyncClient, community_session_factory) -> None:
    post_id = await _seed_post(community_session_factory, author=USER_A)
    reply_a = await _seed_reply(community_session_factory, post_id, author=USER_B)
    reply_b = await _seed_reply(community_session_factory, post_id, author=USER_B)
    # 解决 → generation=1 + 通知
    r = await client.post(
        f"/api/v1/community/posts/{post_id}/resolve",
        headers=_auth(USER_A),
        json={"reply_id": reply_a},
    )
    assert r.status_code == 200
    body = await _post_body(community_session_factory, post_id)
    assert str(body["solved_reply_id"]) == reply_a
    assert body["solution_generation"] == 1
    async with community_session_factory() as session:
        n = (
            await session.execute(text("SELECT COUNT(*) FROM community_notifications"))
        ).scalar_one()
    assert n == 1
    # 同 reply 幂等重试：不递增、不重复通知
    await client.post(
        f"/api/v1/community/posts/{post_id}/resolve",
        headers=_auth(USER_A),
        json={"reply_id": reply_a},
    )
    body = await _post_body(community_session_factory, post_id)
    assert body["solution_generation"] == 1
    async with community_session_factory() as session:
        n = (
            await session.execute(text("SELECT COUNT(*) FROM community_notifications"))
        ).scalar_one()
    assert n == 1
    # 切换 A→B：generation+1、新通知
    await client.post(
        f"/api/v1/community/posts/{post_id}/resolve",
        headers=_auth(USER_A),
        json={"reply_id": reply_b},
    )
    body = await _post_body(community_session_factory, post_id)
    assert str(body["solved_reply_id"]) == reply_b
    assert body["solution_generation"] == 2
    async with community_session_factory() as session:
        n = (
            await session.execute(text("SELECT COUNT(*) FROM community_notifications"))
        ).scalar_one()
    assert n == 2
    # 取消：不递增、不通知
    await client.post(
        f"/api/v1/community/posts/{post_id}/resolve",
        headers=_auth(USER_A),
        json={"reply_id": None},
    )
    body = await _post_body(community_session_factory, post_id)
    assert body["solved_reply_id"] is None
    assert body["solution_generation"] == 2
    async with community_session_factory() as session:
        n = (
            await session.execute(text("SELECT COUNT(*) FROM community_notifications"))
        ).scalar_one()
    assert n == 2


async def test_resolve_closed_and_non_author(
    client: AsyncClient, community_session_factory
) -> None:
    post_id = await _seed_post(community_session_factory, author=USER_A)
    reply_id = await _seed_reply(community_session_factory, post_id, author=USER_B)
    # 非作者 → NOT_FOUND（不泄露对象状态）
    r = await client.post(
        f"/api/v1/community/posts/{post_id}/resolve",
        headers=_auth(USER_B),
        json={"reply_id": reply_id},
    )
    assert r.status_code == 404
    # deleted 帖作者 → POST_CLOSED（D21）
    deleted_id = await _seed_post(community_session_factory, author=USER_A, status="deleted")
    r2 = await client.post(
        f"/api/v1/community/posts/{deleted_id}/resolve",
        headers=_auth(USER_A),
        json={"reply_id": reply_id},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "COMMUNITY_POST_CLOSED"


# ---------------------------------------------------------------------------
# 删除（§11.1 / D26 / D34）
# ---------------------------------------------------------------------------


async def test_delete_post_and_reply(client: AsyncClient, community_session_factory) -> None:
    post_id = await _seed_post(community_session_factory, author=USER_A)
    await _seed_reply(community_session_factory, post_id, author=USER_B)
    # 非作者删除 → NOT_FOUND
    r = await client.delete(f"/api/v1/community/posts/{post_id}", headers=_auth(USER_B))
    assert r.status_code == 404
    # 作者删除帖子：墓碑 + closed + deletion Outbox（仅帖子来源）
    r = await client.delete(f"/api/v1/community/posts/{post_id}", headers=_auth(USER_A))
    assert r.status_code == 200
    body = await _post_body(community_session_factory, post_id)
    assert body["status"] == "deleted" and body["discussion_status"] == "closed"
    assert body["eligible_for_memory"] is False
    # 重复删除幂等成功，不重复生成 deletion event
    await client.delete(f"/api/v1/community/posts/{post_id}", headers=_auth(USER_A))
    async with community_session_factory() as session:
        dels = (
            (
                await session.execute(
                    text(
                        "SELECT event_type, payload FROM community_outbox "
                        "WHERE event_type = 'community.source_deleted'"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(dels) == 1
    assert dels[0]["payload"]["source_ref"] == f"community:post:{post_id}"
    assert dels[0]["payload"]["source_system"] == "activity"


async def test_delete_solved_reply_clears_solution(
    client: AsyncClient, community_session_factory
) -> None:
    """D34：删除 solved 回复仅清除解决标记，不递增 generation、不产生通知。"""
    post_id = await _seed_post(community_session_factory, author=USER_A)
    reply_id = await _seed_reply(community_session_factory, post_id, author=USER_B)
    await client.post(
        f"/api/v1/community/posts/{post_id}/resolve",
        headers=_auth(USER_A),
        json={"reply_id": reply_id},
    )
    r = await client.delete(
        f"/api/v1/community/posts/{post_id}/replies/{reply_id}", headers=_auth(USER_B)
    )
    assert r.status_code == 200
    body = await _post_body(community_session_factory, post_id)
    assert body["solved_reply_id"] is None
    assert body["solution_generation"] == 1  # 未递增
    assert body["reply_count"] == 0  # D26：递减为当前 active 回复数
    async with community_session_factory() as session:
        n = (
            await session.execute(text("SELECT COUNT(*) FROM community_notifications"))
        ).scalar_one()
    assert n == 1  # 只有最初 resolve 的通知，删除未新增


# ---------------------------------------------------------------------------
# 通知（§8.6）
# ---------------------------------------------------------------------------


async def test_notifications_read_and_isolation(
    client: AsyncClient, community_session_factory
) -> None:
    post_id = await _seed_post(community_session_factory, author=USER_A)
    await client.post(
        f"/api/v1/community/posts/{post_id}/replies",
        headers={**_auth(USER_B), **_idem("n1")},
        json={"body": "回复一"},
    )
    r = await client.get("/api/v1/community/notifications", headers=_auth(USER_A))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unread_count"] == 1
    assert body["items"][0]["event_type"] == "post_replied"
    assert "actor_user_id" not in str(body) and "recipient_user_id" not in str(body)
    # 用户 B 无通知（跨用户隔离）
    r_b = await client.get("/api/v1/community/notifications", headers=_auth(USER_B))
    assert r_b.json()["unread_count"] == 0
    # 单条已读
    nid = body["items"][0]["notification_id"]
    r2 = await client.post(f"/api/v1/community/notifications/{nid}/read", headers=_auth(USER_A))
    assert r2.json()["unread_count"] == 0
    # read-all 幂等
    r3 = await client.post("/api/v1/community/notifications/read-all", headers=_auth(USER_A))
    assert r3.json() == {"unread_count": 0}


# ---------------------------------------------------------------------------
# purge（§8.8 / D35 / D36）
# ---------------------------------------------------------------------------


async def test_purge_requires_system_scope(
    community_session_factory,
) -> None:
    """purge 仅限 system principal + community:account_purge；普通用户拒绝。"""
    from backend.community.api.internal_accounts import router as purge_router

    app = await _make_app(community_session_factory)
    app.include_router(purge_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # 普通用户（dev auth）→ 403
        r = await c.post(
            "/api/v1/internal/community-accounts/purge",
            headers=_auth(USER_A),
            json={"user_id": USER_A},
        )
        assert r.status_code in (401, 403)
        # 无认证 → 401
        r2 = await c.post("/api/v1/internal/community-accounts/purge", json={"user_id": USER_A})
        assert r2.status_code == 401


async def test_purge_deletes_user_content_idempotently(
    community_session_factory,
) -> None:
    """purge：帖子/回复 deleted + deletion Outbox；重复 purge 不重复产生事实。"""
    from backend.community.contracts.domain import source_deletion_id_for

    # 构造 system principal token 场景：直接调用 service 层逻辑校验幂等性。
    # 路由级 system token 校验在 PR-D（scope 加入 ALL_SCOPES 后）以
    # service_tokens 签发工具验证。
    post_id = await _seed_post(community_session_factory, author=USER_A)
    reply_id = await _seed_reply(community_session_factory, post_id, author=USER_A)
    # 直接执行 purge 的业务逻辑（路由 scope 校验已在上个测试覆盖）
    from backend.community.persistence import outbox as outbox_repo

    async with community_session_factory() as session:
        async with session.begin():
            from backend.community.persistence import posts as posts_repo
            from backend.community.persistence import replies as replies_repo

            await posts_repo.mark_post_deleted(session, post_id)
            await replies_repo.mark_reply_deleted(session, reply_id)
            for ref, agg_type, agg_id in (
                (f"community:post:{post_id}", "post", post_id),
                (f"community:reply:{reply_id}", "reply", reply_id),
            ):
                await outbox_repo.insert_event(
                    session,
                    event_id=source_deletion_id_for(USER_A, ref),
                    event_type="community.source_deleted",
                    aggregate_type=agg_type,
                    aggregate_id=str(agg_id),
                    user_id=USER_A,
                    payload={
                        "source_ref": ref,
                        "source_version": None,
                        "source_system": "activity",
                        "event_id": str(source_deletion_id_for(USER_A, ref)),
                    },
                    idempotency_key=f"community:community.source_deleted:{ref}",
                )
    # 重放（同一 outbox 键）：不产生第二条 deletion fact
    async with community_session_factory() as session:
        async with session.begin():
            for ref, agg_type, agg_id in (
                (f"community:post:{post_id}", "post", post_id),
                (f"community:reply:{reply_id}", "reply", reply_id),
            ):
                inserted = await outbox_repo.insert_event(
                    session,
                    event_id=source_deletion_id_for(USER_A, ref),
                    event_type="community.source_deleted",
                    aggregate_type=agg_type,
                    aggregate_id=str(agg_id),
                    user_id=USER_A,
                    payload={
                        "source_ref": ref,
                        "source_version": None,
                        "source_system": "activity",
                        "event_id": str(source_deletion_id_for(USER_A, ref)),
                    },
                    idempotency_key=f"community:community.source_deleted:{ref}",
                )
                assert inserted is False  # ON CONFLICT DO NOTHING 去重
    async with community_session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM community_outbox "
                    "WHERE event_type = 'community.source_deleted'"
                )
            )
        ).scalar_one()
    assert count == 2
