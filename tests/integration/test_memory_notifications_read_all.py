"""Memory 域通知 read-all 集成测试（方案 community §8.6/D14）。

- POST /api/v1/memory/notifications/read-all 只更新当前认证用户的未读记录；
- 响应统一 {"unread_count": 0}（§8.6 冻结：两个域的 read-all 响应一致）；
- 重复调用幂等，返回当前计数。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from backend.memory.persistence import notifications as notifications_repo

pytestmark = pytest.mark.asyncio


async def _insert_notification(session_factory, *, user_id, unread: bool) -> None:
    """构造 memory_outbox 行 + 通知（read_at 可空；FK/唯一约束满足）。"""
    from backend.memory.persistence import outbox as outbox_repo

    outbox_id = uuid4()
    nid = uuid4()
    read_at = None if unread else datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            # operation_id 有 FK：用 NULL（outbox 允许无 operation）；
            # aggregate_id 唯一约束（uq_memory_outbox_event）→ 每条用独立值
            await outbox_repo.insert_event(
                session,
                outbox_id=outbox_id,
                operation_id=None,
                user_id=user_id,
                event_type="memory.changed",
                aggregate_type="memory",
                aggregate_id=f"a{outbox_id}",
                aggregate_version=1,
                payload={},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_user_notifications "
                    "(notification_id, user_id, event_type, title, body, "
                    " aggregate_type, aggregate_id, source_outbox_id, read_at, created_at) "
                    "VALUES (:nid, :uid, 'activity_evidence', 't', 'b', "
                    " 'activity', 'a1', :oid, :read_at, :created_at)"
                ),
                {
                    "nid": nid,
                    "uid": user_id,
                    "oid": outbox_id,
                    "read_at": read_at,
                    "created_at": datetime.now(UTC) - timedelta(minutes=5),
                },
            )


async def _make_client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_memory_read_all_only_updates_current_user(session_factory) -> None:
    """D14：read-all 只更新当前认证用户；其他用户未读不受影响。"""
    from backend.app import create_app
    from backend.settings import Settings

    user_a = uuid4()
    user_b = uuid4()
    await _insert_notification(session_factory, user_id=user_a, unread=True)
    await _insert_notification(session_factory, user_id=user_b, unread=True)

    settings = Settings(app_env="development")
    app = create_app(settings=settings)
    # 依赖注入：覆盖 runtime 使用测试库（避免连开发库）
    from types import SimpleNamespace

    app.state.runtime = SimpleNamespace(session_factory=session_factory, maintenance_gate=None)
    async with await _make_client(app) as client:
        r = await client.post(
            "/api/v1/memory/notifications/read-all",
            headers={"X-Dev-User-Id": str(user_a)},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"unread_count": 0}
        # 用户 B 的未读不受影响
        async with session_factory() as session:
            unread_b = await notifications_repo.unread_count(session, user_id=user_b)
            unread_a = await notifications_repo.unread_count(session, user_id=user_a)
        assert unread_b == 1
        assert unread_a == 0
        # 幂等：重复调用返回 200 与当前计数
        r2 = await client.post(
            "/api/v1/memory/notifications/read-all",
            headers={"X-Dev-User-Id": str(user_a)},
        )
        assert r2.json() == {"unread_count": 0}
