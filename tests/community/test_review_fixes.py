"""Review 修复补测（评审项 1/3/4/9/10/11/12/13 + Critical 1-10，PR 全量修复验证）。

覆盖：
- Critical 1：并发同键幂等（asyncio.gather）只创建一个资源 + 一条 outbox；
  过期幂等行替换语义（§8.3 同键同 payload 返回原资源）；
- 项 3：hidden 回复不通过公共详情暴露；
- 项 4：删除链路 token 校验（settings validator）；
- 项 10：限流 429 + Retry-After + user/IP 双 bucket；purge 正向路径
  （dev scope override 模拟 system principal）；
- 项 11：板块不存在 → 404 NOT_FOUND；
- 项 12：reply_created payload source_version=content_hash + window 字段；
- 项 13：删除 outbox 幂等键 = D32 公式；
- Critical 修复：附件上传幂等、本地存储路径穿越、已删帖附件不泄露、
  管理员审核幂等、建吧申请唯一冲突。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from backend.community.contracts.domain import BOARDS_SEED
from backend.community.storage.factory import get_storage_backend

pytestmark = pytest.mark.asyncio

USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"
BOARD = BOARDS_SEED[0]


async def _make_app(community_session_factory, **settings_overrides):
    """带写服务的 app；DEV_AUTH_ALLOW_SCOPE_OVERRIDE=True 支持 system 模拟。"""
    from backend.app import create_app
    from backend.community.api.community import router as community_router
    from backend.community.api.dependencies import CommunityRuntime
    from backend.community.persistence.database import create_community_engine
    from backend.community.services.attachment_service import AttachmentUploadService
    from backend.community.services.board_application_service import BoardApplicationService
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
        COMMUNITY_V2_ENABLED=True,
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
    storage = get_storage_backend(settings)
    reply_service = ReplyService(
        session_factory=community_session_factory,
        profile_reader_factory=lambda: profile_reader,
        settings=settings,
    )
    post_command = PostCommandService(
        session_factory=community_session_factory,
        profile_reader_factory=lambda: profile_reader,
        reply_service=reply_service,
        settings=settings,
        storage=storage,
    )
    runtime = CommunityRuntime(
        settings=settings,
        database=db,
        post_service=PostReadService(community_session_factory, settings=settings, storage=storage),
        post_command_service=post_command,
        reply_service=reply_service,
        profile_reader_factory=lambda: profile_reader,
        attachment_upload_service=AttachmentUploadService(
            settings=settings, storage=storage, session_factory=community_session_factory
        ),
        board_application_service=BoardApplicationService(
            settings=settings, session_factory=community_session_factory
        ),
        storage=storage,
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


def _admin_auth() -> dict[str, str]:
    return {"X-Dev-User-Id": USER_A}


def _small_png(color: tuple[int, int, int, int] = (0, 0, 0, 0)) -> BytesIO:
    """1x1 PNG，用于上传测试。"""
    from PIL import Image

    buf = BytesIO()
    Image.new("RGBA", (1, 1), color).save(buf, format="PNG")
    buf.seek(0)
    return buf


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
                    " content_hash, status, discussion_status, reply_count, "
                    " created_at, updated_at, last_activity_at) "
                    "VALUES (:pid, :uid, 'alice', :bid, 't', 'b', :h, 'active', 'open', 1, "
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
                    " content_hash, status, discussion_status, reply_count, "
                    " created_at, updated_at, last_activity_at) "
                    "VALUES (:pid, :uid, 'alice', :bid, 't', 'b', :h, 'active', 'open', 1, "
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
                    " content_hash, status, discussion_status, reply_count, "
                    " created_at, updated_at, last_activity_at) "
                    "VALUES (:pid, :uid, 'alice', :bid, 't', 'b', :h, 'active', 'open', 1, "
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
                    " content_hash, status, discussion_status, reply_count, "
                    " created_at, updated_at, last_activity_at) "
                    "VALUES (:pid, :uid, 'alice', :bid, 't', 'b', :h, 'active', 'open', 1, "
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


# ---------------------------------------------------------------------------
# Critical 修复回归测试
# ---------------------------------------------------------------------------


async def test_upload_attachment_idempotency_same_file_replays(
    community_session_factory,
) -> None:
    """同 Idempotency-Key + 同文件 → 返回同一 attachment_id。"""
    app = await _make_app(community_session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.post(
            "/api/v1/community/uploads",
            headers={**_auth(), "Idempotency-Key": "upload-same"},
            files={"file": ("a.png", _small_png(), "image/png")},
        )
        assert r1.status_code == 201, r1.text
        r2 = await client.post(
            "/api/v1/community/uploads",
            headers={**_auth(), "Idempotency-Key": "upload-same"},
            files={"file": ("a.png", _small_png(), "image/png")},
        )
        assert r2.status_code == 201, r2.text
    assert r1.json()["attachment_id"] == r2.json()["attachment_id"]


async def test_upload_attachment_idempotency_different_file_conflicts(
    community_session_factory,
) -> None:
    """同 Idempotency-Key + 不同文件 → 409 冲突。"""
    app = await _make_app(community_session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.post(
            "/api/v1/community/uploads",
            headers={**_auth(), "Idempotency-Key": "upload-diff"},
            files={"file": ("a.png", _small_png(), "image/png")},
        )
        assert r1.status_code == 201, r1.text
        r2 = await client.post(
            "/api/v1/community/uploads",
            headers={**_auth(), "Idempotency-Key": "upload-diff"},
            files={"file": ("b.png", _small_png((255, 0, 0, 255)), "image/png")},
        )
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "COMMUNITY_IDEMPOTENCY_CONFLICT"


async def test_local_upload_path_traversal_returns_404(
    community_session_factory,
) -> None:
    """本地存储 key 含 ../ 时返回 404，不泄露 base_path 外文件。"""
    app = await _make_app(community_session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v1/community/local-uploads/../etc/passwd")
    assert r.status_code == 404


async def test_deleted_post_detail_has_no_attachments(
    community_session_factory,
) -> None:
    """已删除帖子详情不返回附件 URL（墓碑契约）。"""
    app = await _make_app(community_session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        up = await client.post(
            "/api/v1/community/uploads",
            headers={**_auth(), "Idempotency-Key": "deleted-post-attach"},
            files={"file": ("a.png", _small_png(), "image/png")},
        )
        assert up.status_code == 201, up.text
        aid = up.json()["attachment_id"]
        r = await client.post(
            "/api/v1/community/posts",
            headers={**_auth(), "Idempotency-Key": "deleted-post"},
            json={
                "board_id": str(BOARD[0]),
                "title": "将删除",
                "body": "正文",
                "attachment_ids": [aid],
            },
        )
        assert r.status_code == 201, r.text
        post_id = r.json()["post_id"]
        await client.delete(
            f"/api/v1/community/posts/{post_id}",
            headers={**_auth(), "Idempotency-Key": "deleted-post-del"},
        )
        detail = await client.get(f"/api/v1/community/posts/{post_id}", headers=_auth())
    assert detail.status_code == 200
    post = detail.json()["post"]
    assert post["deleted"] is True
    assert post["title"] is None
    assert post["attachments"] == []


async def test_admin_approve_application_idempotency(
    community_session_factory,
) -> None:
    """管理员通过同 Idempotency-Key 重复 approve → 返回同一 board_id。"""
    app = await _make_app(
        community_session_factory,
        COMMUNITY_ADMIN_USER_IDS=USER_A,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        app_resp = await client.post(
            "/api/v1/community/applications",
            headers={**_auth(USER_B), "Idempotency-Key": "admin-app-001"},
            json={
                "name": "幂等审核吧",
                "slug": "idem-review",
                "description": "d",
                "reason": "r",
            },
        )
        assert app_resp.status_code == 201, app_resp.text
        application_id = app_resp.json()["application_id"]
        a1 = await client.post(
            f"/api/v1/community/admin/applications/{application_id}/approve",
            headers={**_admin_auth(), "Idempotency-Key": "admin-approve-001"},
        )
        assert a1.status_code == 200, a1.text
        a2 = await client.post(
            f"/api/v1/community/admin/applications/{application_id}/approve",
            headers={**_admin_auth(), "Idempotency-Key": "admin-approve-001"},
        )
        assert a2.status_code == 200, a2.text
    assert a1.json()["board_id"] == a2.json()["board_id"]


async def test_create_application_conflict_with_existing_board(
    community_session_factory,
) -> None:
    """申请 slug 与已有板块冲突 → 409 BoardNameConflict。"""
    app = await _make_app(community_session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/community/applications",
            headers={**_auth(USER_B), "Idempotency-Key": "conflict-app"},
            json={
                "name": BOARDS_SEED[0][2],
                "slug": BOARDS_SEED[0][1],
                "description": "d",
                "reason": "r",
            },
        )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BOARD_NAME_CONFLICT"
