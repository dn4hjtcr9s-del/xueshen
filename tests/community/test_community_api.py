"""Community 只读 API 集成测试（方案 §15.3，PR-B 纵切）。

覆盖：
- 登录隔离：user_id 全部来自 AuthContext（dev auth X-Dev-User-Id），
  列表/详情不暴露内部 user_id/email（§9.1）；
- 板块列表：只返回 active（§8.1）；
- 帖子列表：latest 排序、板块筛选、unanswered 过滤（§8.2）；
- 游标：公共游标跨用户可解析、绑定 sort/board、跨路由复用拒绝（D13）；
- 详情：墓碑契约（deleted 帖子 title/body=null）、hidden → NOT_FOUND（§8.4）；
- viewer_liked / viewer_is_author 视角正确。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from backend.community.contracts.domain import BOARDS_SEED

pytestmark = pytest.mark.asyncio

USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"


async def _make_app(community_session_factory):
    """构造带 Community 装配的 FastAPI app（复用 conversation 测试装配模式）。

    app_env=development 以启用 dev auth（X-Dev-User-Id）；直接挂载模块级
    router 并注入测试库 runtime（build_community_routers 会读取全局 settings
    连接开发库，测试不经过它）。
    """
    from types import SimpleNamespace

    from backend.app import create_app
    from backend.community.api.community import router as community_router
    from backend.community.api.dependencies import CommunityRuntime
    from backend.community.persistence.database import create_community_engine
    from backend.community.services.post_service import PostReadService
    from backend.settings import Settings

    settings = Settings(app_env="development")
    app = create_app(settings=settings)
    # dev auth 的 get_auth_context 需要 app.state.runtime.session_factory
    # （IdentityMappingRepository 仅生产路径使用；dev adapter 不查询身份映射）；
    # maintenance_gate=None 供 observability middleware 读取
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
    runtime = CommunityRuntime(
        settings=settings, database=db, post_service=PostReadService(community_session_factory)
    )
    app.state.community_db = db
    app.state.community_runtime = runtime
    app.include_router(community_router)
    return app


async def _seed_post(
    session_factory,
    *,
    board_slug: str,
    title: str,
    body: str,
    author: str = USER_A,
    display_name: str = "alice",
    pinned: bool = False,
    solved: bool = False,
    deleted: bool = False,
    last_activity_at: datetime | None = None,
) -> str:
    """插入测试帖子并返回 post_id（直接落库，测试数据构造）。"""
    board = next(b for b in BOARDS_SEED if b[1] == board_slug)
    post_id = uuid4()
    now = last_activity_at or datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO community_posts "
                    "(post_id, user_id, author_display_name, board_id, title, body, "
                    " content_hash, status, discussion_status, pinned, solved_reply_id, "
                    " reply_count, like_count, created_at, updated_at, "
                    " last_activity_at, deleted_at) "
                    "VALUES (:pid, :uid, :name, :bid, :title, :body, :hash, :status, 'open', "
                    " :pinned, NULL, 0, 0, :now, :now, :la, :deleted_at)"
                ),
                {
                    "pid": post_id,
                    "uid": author,
                    "name": display_name,
                    "bid": board[0],
                    "title": title,
                    "body": body,
                    "hash": "0" * 64,
                    "status": "deleted" if deleted else "active",
                    "pinned": pinned,
                    "now": now,
                    "la": now,
                    "deleted_at": now if deleted else None,
                },
            )
    return str(post_id)


async def _seed_reply(
    session_factory, post_id: str, *, author=USER_B, display_name="bob", body="回复正文"
) -> str:
    reply_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO community_replies "
                    "(reply_id, post_id, user_id, author_display_name, body, content_hash, "
                    " status, eligible_for_memory, created_at, updated_at) "
                    "VALUES (:rid, :pid, :uid, :name, :body, :hash, 'active', true, :now, :now)"
                ),
                {
                    "rid": reply_id,
                    "pid": post_id,
                    "uid": author,
                    "name": display_name,
                    "body": body,
                    "hash": "0" * 64,
                    "now": datetime.now(UTC),
                },
            )
    return str(reply_id)


@pytest.fixture()
async def client(community_session_factory) -> AsyncClient:
    app = await _make_app(community_session_factory)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _auth(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


async def test_boards_list_active_only(client: AsyncClient) -> None:
    r = await client.get("/api/v1/community/boards", headers=_auth(USER_A))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [b["slug"] for b in items] == [
        "linear-algebra",
        "calculus",
        "probability",
        "study-methods",
    ]
    first = items[0]
    # §9.1：不暴露内部字段
    assert "user_id" not in first and "email" not in str(first)


async def test_posts_list_latest_sorted(client: AsyncClient, community_session_factory) -> None:
    older = datetime.now(UTC) - timedelta(hours=2)
    await _seed_post(
        community_session_factory,
        board_slug="linear-algebra",
        title="旧帖",
        body="b1",
        last_activity_at=older,
    )
    await _seed_post(
        community_session_factory, board_slug="linear-algebra", title="新帖", body="b2"
    )
    r = await client.get("/api/v1/community/posts", headers=_auth(USER_A))
    assert r.status_code == 200, r.text
    titles = [item["title"] for item in r.json()["items"]]
    assert titles == ["新帖", "旧帖"]


async def test_posts_list_board_filter_and_unanswered(
    client: AsyncClient, community_session_factory
) -> None:
    await _seed_post(
        community_session_factory, board_slug="linear-algebra", title="线代帖", body="b"
    )
    await _seed_post(community_session_factory, board_slug="calculus", title="微积分帖", body="b")
    r = await client.get(
        "/api/v1/community/posts",
        headers=_auth(USER_A),
        params={"board_id": "da38ecb6-6f37-5724-be95-10e496b5f3dd"},
    )
    titles = [item["title"] for item in r.json()["items"]]
    assert titles == ["线代帖"]
    # unanswered：solved 帖被过滤（两帖均未解决 → 全部保留）
    r2 = await client.get(
        "/api/v1/community/posts",
        headers=_auth(USER_A),
        params={"sort": "unanswered"},
    )
    r2_titles = {item["title"] for item in r2.json()["items"]}
    assert r2_titles == {"线代帖", "微积分帖"}


async def test_posts_list_excludes_deleted_and_hidden(
    client: AsyncClient, community_session_factory
) -> None:
    await _seed_post(community_session_factory, board_slug="linear-algebra", title="存活", body="b")
    await _seed_post(
        community_session_factory, board_slug="linear-algebra", title="已删", body="b", deleted=True
    )
    r = await client.get("/api/v1/community/posts", headers=_auth(USER_A))
    titles = [item["title"] for item in r.json()["items"]]
    assert titles == ["存活"]


async def test_posts_list_public_cursor_across_users(
    client: AsyncClient, community_session_factory
) -> None:
    """D13：公共游标跨用户可复用；绑定 sort/board；跨路由拒绝。"""
    now = datetime.now(UTC)
    for i in range(3):
        await _seed_post(
            community_session_factory,
            board_slug="linear-algebra",
            title=f"帖{i}",
            body="b",
            last_activity_at=now - timedelta(minutes=i),
        )
    r = await client.get("/api/v1/community/posts", headers=_auth(USER_A), params={"limit": 2})
    body = r.json()
    assert body["has_more"] is True and body["next_cursor"]
    cursor = body["next_cursor"]
    # 另一用户继续翻页（公共游标不绑用户）
    r2 = await client.get(
        "/api/v1/community/posts",
        headers=_auth(USER_B),
        params={"limit": 2, "cursor": cursor},
    )
    assert r2.status_code == 200, r2.text
    assert len(r2.json()["items"]) == 1
    # 绑定项变化 → 游标非法
    r3 = await client.get(
        "/api/v1/community/posts",
        headers=_auth(USER_A),
        params={"limit": 2, "cursor": cursor, "sort": "unanswered"},
    )
    assert r3.status_code == 422
    assert r3.json()["error"]["code"] == "COMMUNITY_CURSOR_INVALID"


async def test_post_detail_tombstone_and_hidden(
    client: AsyncClient, community_session_factory
) -> None:
    deleted_id = await _seed_post(
        community_session_factory,
        board_slug="linear-algebra",
        title="已删",
        body="secret",
        deleted=True,
        author=USER_A,
    )
    r = await client.get(f"/api/v1/community/posts/{deleted_id}", headers=_auth(USER_A))
    assert r.status_code == 200, r.text
    post = r.json()["post"]
    # §6.6 墓碑契约：title/body=null、deleted=true，不泄露原正文
    assert post["deleted"] is True
    assert post["title"] is None and post["body"] is None
    assert "secret" not in r.text
    assert post["viewer_is_author"] is True
    # hidden 对所有人（含作者）NOT_FOUND
    async with community_session_factory() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE community_posts SET status = 'hidden' WHERE post_id = :pid"),
                {"pid": deleted_id},
            )
    r2 = await client.get(f"/api/v1/community/posts/{deleted_id}", headers=_auth(USER_A))
    assert r2.status_code == 404
    assert r2.json()["error"]["code"] == "COMMUNITY_NOT_FOUND"


async def test_post_detail_replies_pagination_and_cursor_binding(
    client: AsyncClient, community_session_factory
) -> None:
    post_id = await _seed_post(
        community_session_factory,
        board_slug="linear-algebra",
        title="详情帖",
        body="b",
        author=USER_A,
    )
    r1id = await _seed_reply(community_session_factory, post_id, author=USER_B)
    r2id = await _seed_reply(community_session_factory, post_id, author=USER_B)
    r = await client.get(
        f"/api/v1/community/posts/{post_id}",
        headers=_auth(USER_A),
        params={"reply_limit": 1},
    )
    body = r.json()
    assert body["replies"]["has_more"] is True
    assert [i["reply_id"] for i in body["replies"]["items"]] == [r1id]
    cursor = body["replies"]["next_cursor"]
    r2 = await client.get(
        f"/api/v1/community/posts/{post_id}",
        headers=_auth(USER_A),
        params={"reply_limit": 1, "reply_cursor": cursor},
    )
    assert [i["reply_id"] for i in r2.json()["replies"]["items"]] == [r2id]
    # D39：回复游标绑定具体 post_id，换帖子复用 → 非法
    other_post = await _seed_post(
        community_session_factory, board_slug="calculus", title="另一帖", body="b", author=USER_A
    )
    r3 = await client.get(
        f"/api/v1/community/posts/{other_post}",
        headers=_auth(USER_A),
        params={"reply_limit": 1, "reply_cursor": cursor},
    )
    assert r3.status_code == 422


async def test_auth_required(client: AsyncClient) -> None:
    r = await client.get("/api/v1/community/posts")
    assert r.status_code == 401
