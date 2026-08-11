"""Break-glass 仓储 SQL 集成测试（§13.15）：真实 PostgreSQL 往返。

单元测试（tests/unit/test_break_glass.py）用内存 fake 覆盖 API 行为；
本文件验证 grant/audit 两张表的 SQL 正确性。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.persistence import break_glass as bg_repo


async def test_grant_create_get_revoke_roundtrip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    grant_id = uuid4()
    admin_id = uuid4()
    target_id = uuid4()
    expires_at = datetime.now(UTC) + timedelta(minutes=30)

    async with session_factory() as session:
        async with session.begin():
            created = await bg_repo.create_grant(
                session,
                grant_id=grant_id,
                admin_user_id=admin_id,
                target_user_id=target_id,
                reason="故障核查",
                scopes=["memory:read"],
                approved_by=None,
                expires_at=expires_at,
            )
    assert created["grant_id"] == grant_id
    assert list(created["scopes"]) == ["memory:read"]
    assert created["revoked_at"] is None

    async with session_factory() as session:
        async with session.begin():
            loaded = await bg_repo.get_grant(session, grant_id)
            assert loaded is not None
            assert loaded["admin_user_id"] == admin_id
            assert loaded["target_user_id"] == target_id

            revoked = await bg_repo.revoke_grant(
                session, grant_id=grant_id, revoked_at=datetime.now(UTC)
            )
            assert revoked is True
            # 重复撤销是 no-op
            again = await bg_repo.revoke_grant(
                session, grant_id=grant_id, revoked_at=datetime.now(UTC)
            )
            assert again is False

            final = await bg_repo.get_grant(session, grant_id)
            assert final is not None
            assert final["revoked_at"] is not None


async def test_audit_insert_and_query(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    grant_id = uuid4()
    admin_id = uuid4()
    target_id = uuid4()

    async with session_factory() as session:
        async with session.begin():
            await bg_repo.create_grant(
                session,
                grant_id=grant_id,
                admin_user_id=admin_id,
                target_user_id=target_id,
                reason="故障核查",
                scopes=["memory:read"],
                approved_by=None,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
            for action in ("request", "approve", "use", "read_body"):
                await bg_repo.insert_audit(
                    session,
                    audit_id=uuid4(),
                    grant_id=grant_id,
                    admin_user_id=admin_id,
                    target_user_id=target_id,
                    action=action,
                    resource_type="grant",
                    resource_id=str(grant_id),
                    trace_id="0" * 32,
                )

    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT action FROM memory_break_glass_audit "
                "WHERE grant_id = :grant_id ORDER BY created_at, action"
            ),
            {"grant_id": grant_id},
        )
        actions = sorted(row[0] for row in result.fetchall())
    assert actions == ["approve", "read_body", "request", "use"]
