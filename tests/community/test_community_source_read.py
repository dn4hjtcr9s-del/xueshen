"""CommunitySourceReadService 集成测试（方案 §10.4 / §15.2，PR-D 纵切）。

覆盖：归属校验（只返回作者自己的内容）、hidden/deleted 拒绝、SourceItem
格式（帖子"标题：..\n正文：.."、回复不拼原帖）、activity_type 前缀校验、
content_ref 校验、SourceBundle 超限拒绝。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from backend.community.contracts.domain import BOARDS_SEED
from backend.community.services.source_read_service import CommunitySourceReadService
from backend.memory.contracts.errors import (
    SourceDeletedError,
    SourceNotFoundError,
    SourceTooLargeError,
)

pytestmark = pytest.mark.asyncio

USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"
BOARD = BOARDS_SEED[0]


def _service(session_factory) -> CommunitySourceReadService:
    return CommunitySourceReadService(session_factory=session_factory)


U_A = UUID(USER_A)
U_B = UUID(USER_B)


async def _seed_post(
    session_factory, *, author=USER_A, title="标题", body="正文", status="active", eligible=True
) -> str:
    post_id = str(uuid4())
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO community_posts "
                    "(post_id, user_id, author_display_name, board_id, title, body, "
                    " content_hash, status, discussion_status, eligible_for_memory, "
                    " created_at, updated_at, last_activity_at) "
                    "VALUES (:pid, :uid, 'alice', :bid, :title, :body, :hash, :status, 'open', "
                    " :eligible, now(), now(), now())"
                ),
                {
                    "pid": post_id,
                    "uid": author,
                    "bid": BOARD[0],
                    "title": title,
                    "body": body,
                    "hash": "0" * 64,
                    "status": status,
                    "eligible": eligible,
                },
            )
    return post_id


async def _seed_reply(session_factory, post_id, *, author=USER_A, body="回复") -> str:
    reply_id = str(uuid4())
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO community_replies "
                    "(reply_id, post_id, user_id, author_display_name, body, content_hash, "
                    " status, eligible_for_memory, created_at, updated_at) "
                    "VALUES (:rid, :pid, :uid, 'alice', :body, :hash, 'active', true, now(), now())"
                ),
                {"rid": reply_id, "pid": post_id, "uid": author, "body": body, "hash": "0" * 64},
            )
    return reply_id


async def test_post_read_returns_own_content(community_session_factory) -> None:
    """§10.4：帖子 SourceItem 格式 "标题：..\n正文：.."，仅作者自己的内容。"""
    post_id = await _seed_post(community_session_factory, title="特征值", body="正文内容")
    bundle = await _service(community_session_factory).read_source_bundle(
        user_id=U_A,
        activity_type="forum_post",
        activity_ids=[f"post:{post_id}"],
        content_ref=f"community:post:{post_id}",
    )
    item = bundle.items[0]
    assert item.source_ref == f"community:post:{post_id}"
    assert item.role == "activity"
    assert item.content == "标题：特征值\n正文：正文内容"
    assert item.metadata["source_version"] == "0" * 64
    assert item.metadata["board_slug"] == "linear-algebra"
    assert item.metadata["author_user_id"] == USER_A


async def test_reply_read_does_not_include_post_content(
    community_session_factory,
) -> None:
    """§10.4：回复 SourceItem 只放回复正文，不拼入原帖正文。"""
    post_id = await _seed_post(community_session_factory, title="原帖", body="原帖秘密")
    reply_id = await _seed_reply(community_session_factory, post_id, body="我的回复")
    bundle = await _service(community_session_factory).read_source_bundle(
        user_id=U_A,
        activity_type="forum_reply",
        activity_ids=[f"reply:{reply_id}"],
        content_ref=f"community:reply:{reply_id}",
    )
    assert bundle.items[0].content == "我的回复"
    assert "原帖秘密" not in bundle.items[0].content


async def test_ownership_mismatch_rejected(community_session_factory) -> None:
    """§10.4 校验 2：他人内容（user_id 不匹配）统一 SOURCE_NOT_FOUND。"""
    post_id = await _seed_post(community_session_factory, author=USER_B)
    with pytest.raises(SourceNotFoundError):
        await _service(community_session_factory).read_source_bundle(
            user_id=U_A,
            activity_type="forum_post",
            activity_ids=[f"post:{post_id}"],
            content_ref=f"community:post:{post_id}",
        )


async def test_deleted_and_hidden_rejected(community_session_factory) -> None:
    """§10.4 校验 3/§9.4：deleted → SOURCE_DELETED；hidden/不可读 → NOT_FOUND。"""
    deleted_id = await _seed_post(community_session_factory, status="deleted")
    with pytest.raises(SourceDeletedError):
        await _service(community_session_factory).read_source_bundle(
            user_id=U_A,
            activity_type="forum_post",
            activity_ids=[f"post:{deleted_id}"],
            content_ref=f"community:post:{deleted_id}",
        )
    hidden_id = await _seed_post(community_session_factory, status="hidden")
    with pytest.raises(SourceNotFoundError):
        await _service(community_session_factory).read_source_bundle(
            user_id=U_A,
            activity_type="forum_post",
            activity_ids=[f"post:{hidden_id}"],
            content_ref=f"community:post:{hidden_id}",
        )


async def test_activity_type_prefix_mismatch(community_session_factory) -> None:
    """§10.4 校验 1：activity_type 与 ID 前缀不一致拒绝。"""
    post_id = await _seed_post(community_session_factory)
    with pytest.raises(SourceNotFoundError):
        await _service(community_session_factory).read_source_bundle(
            user_id=U_A,
            activity_type="forum_reply",
            activity_ids=[f"post:{post_id}"],
            content_ref=f"community:post:{post_id}",
        )


async def test_content_ref_mismatch(community_session_factory) -> None:
    """§10.4 校验 4：content_ref 与稳定 source_ref 不一致拒绝。"""
    post_id = await _seed_post(community_session_factory)
    with pytest.raises(SourceNotFoundError):
        await _service(community_session_factory).read_source_bundle(
            user_id=U_A,
            activity_type="forum_post",
            activity_ids=[f"post:{post_id}"],
            content_ref="community:post:other",
        )


async def test_bundle_size_limit(community_session_factory) -> None:
    """§10.4 校验 5：SourceBundle 超限拒绝（20,000 字符单 item / 80,000 bytes）。"""
    post_id = await _seed_post(community_session_factory, body="b" * 21_000)
    with pytest.raises(SourceTooLargeError):
        await _service(community_session_factory).read_source_bundle(
            user_id=U_A,
            activity_type="forum_post",
            activity_ids=[f"post:{post_id}"],
            content_ref=f"community:post:{post_id}",
        )


async def test_reader_endpoint_rejects_regular_user(
    community_session_factory,
) -> None:
    """§10.4：内部 Reader 端点拒绝非 system principal（普通用户/无 token）。"""
    from types import SimpleNamespace

    from httpx import ASGITransport, AsyncClient

    from backend.app import create_app
    from backend.community.api.internal_sources import build_reader_router
    from backend.community.services.source_read_service import CommunitySourceReadService
    from backend.settings import Settings

    app = create_app(Settings(app_env="development"))
    app.state.runtime = SimpleNamespace(
        session_factory=community_session_factory, maintenance_gate=None
    )
    reader_service = CommunitySourceReadService(session_factory=community_session_factory)
    app.include_router(build_reader_router(reader_service))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/api/v1/internal/community-sources/read",
            headers={"X-Dev-User-Id": USER_A},
            json={"user_id": USER_A, "activity_type": "forum_post", "activity_ids": ["post:x"]},
        )
        # 普通用户（dev auth）→ 403 AUTH_FORBIDDEN
        assert r.status_code == 403, r.text
        r2 = await c.post(
            "/api/v1/internal/community-sources/read",
            json={"user_id": USER_A, "activity_type": "forum_post", "activity_ids": ["post:x"]},
        )
        assert r2.status_code in (401, 403)


def test_community_scopes_in_all_but_not_agent_allowed() -> None:
    """§13.3：community scopes 加入 ALL_SCOPES，不加入 AGENT_ALLOWED_SCOPES。"""
    from backend.auth.context import (
        AGENT_ALLOWED_SCOPES,
        ALL_SCOPES,
        SCOPE_COMMUNITY_ACCOUNT_PURGE,
        SCOPE_COMMUNITY_SOURCE_READ,
    )

    assert SCOPE_COMMUNITY_SOURCE_READ in ALL_SCOPES
    assert SCOPE_COMMUNITY_ACCOUNT_PURGE in ALL_SCOPES
    assert SCOPE_COMMUNITY_SOURCE_READ not in AGENT_ALLOWED_SCOPES
    assert SCOPE_COMMUNITY_ACCOUNT_PURGE not in AGENT_ALLOWED_SCOPES
