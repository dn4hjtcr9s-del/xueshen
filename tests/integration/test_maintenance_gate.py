"""全局 maintenance gate 的真实 PostgreSQL 并发与失败语义测试。

测试使用独立临时数据库，不读取或清理共享开发库的维护状态。覆盖恢复互斥、
旧流量排空、新流量 fail-closed、失败保持锁定与安全中止自动解锁。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlparse

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic import command
from backend.memory.maintenance_gate import (
    MAINTENANCE_GATE_DDL,
    MaintenanceActiveError,
    MaintenanceGate,
    MaintenanceGateUnavailableError,
    RestoreAbortedError,
    RestoreAlreadyRunningError,
)
from backend.memory.persistence.database import create_engine
from backend.settings import Settings

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="需要 docker 创建隔离 PostgreSQL 测试数据库",
)


@pytest.fixture(scope="module")
def maintenance_database_url() -> Iterator[str]:
    """创建仅供本模块使用的数据库，避免触碰共享环境的 fail-closed 状态。"""
    settings = Settings.model_validate({"app_env": "test"})
    parsed = urlparse(settings.database_url)
    db_user = parsed.username or "memory"
    database_name = f"memory_gate_{__import__('uuid').uuid4().hex[:8]}"
    database_url = settings.database_url.rsplit("/", 1)[0] + f"/{database_name}"
    subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "createdb", "-U", db_user, database_name],
        check=True,
        capture_output=True,
    )
    try:
        config = Config("alembic.ini")
        config.attributes["database_url"] = database_url
        command.upgrade(config, "head")
        yield database_url
    finally:
        subprocess.run(
            ["docker", "compose", "exec", "-T", "postgres", "dropdb", "-U", db_user, database_name],
            check=False,
            capture_output=True,
        )


@pytest.fixture()
async def gate_engine(maintenance_database_url: str) -> AsyncIterator[AsyncEngine]:
    """每个测试前后重置隔离库 singleton，真实失败状态不会泄漏到下一用例。"""
    settings = Settings.model_validate(
        {"app_env": "test", "database_url": maintenance_database_url}
    )
    engine = create_engine(settings)
    async with engine.begin() as connection:
        for statement in MAINTENANCE_GATE_DDL:
            await connection.execute(text(statement))
        await connection.execute(
            text(
                """
                UPDATE ops.system_maintenance
                SET active = false, owner_token = NULL, reason = NULL,
                    started_at = NULL, updated_at = now()
                WHERE singleton_id = true
                """
            )
        )
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            for statement in MAINTENANCE_GATE_DDL:
                await connection.execute(text(statement))
            await connection.execute(
                text(
                    """
                    UPDATE ops.system_maintenance
                    SET active = false, owner_token = NULL, reason = NULL,
                        started_at = NULL, updated_at = now()
                    WHERE singleton_id = true
                    """
                )
            )
        await engine.dispose()


async def test_restore_success_failure_and_safe_abort(gate_engine: AsyncEngine) -> None:
    """正常完成解锁；真实失败保持 active；安全前置拒绝可撤销本次门禁。"""
    gate = MaintenanceGate(gate_engine)
    assert await gate.is_active() is False

    async with gate.restore(force=False, reason="test-success"):
        assert await gate.is_active() is True
    assert await gate.is_active() is False

    with pytest.raises(RuntimeError, match="injected restore failure"):
        async with gate.restore(force=False, reason="test-failure"):
            raise RuntimeError("injected restore failure")
    assert await gate.is_active() is True

    with pytest.raises(MaintenanceActiveError):
        async with gate.restore(force=False, reason="test-no-takeover"):
            pass

    async with gate.restore(force=True, reason="test-takeover"):
        assert await gate.is_active() is True
    assert await gate.is_active() is False

    with pytest.raises(RestoreAbortedError):
        async with gate.restore(force=False, reason="test-safe-abort"):
            raise RestoreAbortedError("target non-empty")
    assert await gate.is_active() is False


async def test_restore_waits_for_old_traffic_and_rejects_new_traffic(
    gate_engine: AsyncEngine,
) -> None:
    """active 先落库，再等待旧 shared lock 排空；之后的新工作必须立即拒绝。"""
    traffic_gate = MaintenanceGate(gate_engine)
    restore_gate = MaintenanceGate(gate_engine)
    old_traffic_entered = asyncio.Event()
    release_old_traffic = asyncio.Event()
    restore_body_entered = asyncio.Event()

    async def _old_traffic() -> None:
        async with traffic_gate.traffic():
            old_traffic_entered.set()
            await release_old_traffic.wait()

    async def _restore() -> None:
        async with restore_gate.restore(force=False, reason="test-drain"):
            restore_body_entered.set()

    traffic_task = asyncio.create_task(_old_traffic())
    await old_traffic_entered.wait()
    restore_task = asyncio.create_task(_restore())
    try:
        for _ in range(100):
            if await restore_gate.is_active():
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - 超时仅用于避免并发回归时测试永久挂起
            pytest.fail("restore 未及时激活 durable maintenance 状态")

        assert restore_body_entered.is_set() is False
        with pytest.raises(MaintenanceActiveError):
            async with MaintenanceGate(gate_engine).traffic():
                pass
    finally:
        release_old_traffic.set()
        await traffic_task
        await restore_task

    assert restore_body_entered.is_set() is True
    assert await restore_gate.is_active() is False


async def test_restore_returns_original_session_timeouts_to_connection_pool(
    maintenance_database_url: str,
) -> None:
    """恢复将无限 timeout 限定在自身连接内，归还 pool 前必须还原原值。"""
    engine = create_async_engine(
        maintenance_database_url,
        pool_size=1,
        max_overflow=0,
        connect_args={"options": "-c statement_timeout=1234 -c lock_timeout=5678"},
    )
    try:
        async with engine.begin() as connection:
            for statement in MAINTENANCE_GATE_DDL:
                await connection.execute(text(statement))
            await connection.execute(
                text(
                    """
                    UPDATE ops.system_maintenance
                    SET active = false, owner_token = NULL, reason = NULL,
                        started_at = NULL, updated_at = now()
                    WHERE singleton_id = true
                    """
                )
            )
        async with engine.connect() as connection:
            before = (
                str((await connection.execute(text("SHOW statement_timeout"))).scalar_one()),
                str((await connection.execute(text("SHOW lock_timeout"))).scalar_one()),
            )
        assert before == ("1234ms", "5678ms")

        with pytest.raises(RuntimeError, match="injected restore failure"):
            async with MaintenanceGate(engine).restore(force=False, reason="test-timeout-cleanup"):
                raise RuntimeError("injected restore failure")

        async with engine.connect() as connection:
            after = (
                str((await connection.execute(text("SHOW statement_timeout"))).scalar_one()),
                str((await connection.execute(text("SHOW lock_timeout"))).scalar_one()),
            )
        assert after == before
    finally:
        await engine.dispose()


def test_upgrade_reconciles_weak_bootstrap_gate_schema(settings: Settings) -> None:
    """0006 必须强化旧 bootstrap 表，不能因 CREATE IF NOT EXISTS 保留弱约束。"""
    parsed = urlparse(settings.database_url)
    db_user = parsed.username or "memory"
    database_name = f"memory_gate_legacy_{__import__('uuid').uuid4().hex[:8]}"
    database_url = settings.database_url.rsplit("/", 1)[0] + f"/{database_name}"
    subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "createdb", "-U", db_user, database_name],
        check=True,
        capture_output=True,
    )
    try:
        config = Config("alembic.ini")
        config.attributes["database_url"] = database_url
        command.upgrade(config, "0005_lease_fencing_generation")
        import psycopg

        sync_database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(sync_database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE ops.system_maintenance (
                        singleton_id boolean PRIMARY KEY DEFAULT true CHECK (singleton_id),
                        active boolean NOT NULL DEFAULT false,
                        owner_token uuid,
                        reason text,
                        started_at timestamptz,
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        CHECK (active OR owner_token IS NULL),
                        CHECK (active OR reason IS NULL),
                        CHECK (active OR started_at IS NULL)
                    )
                    """
                )
                # 旧 schema 接受这个不完整 active 状态；迁移后它必须仍 fail-closed，
                # 且被归一化为具备接管信息的可审计状态。
                cursor.execute(
                    "INSERT INTO ops.system_maintenance (singleton_id, active) VALUES (true, true)"
                )
            conn.commit()

        command.upgrade(config, "head")

        with psycopg.connect(sync_database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT active, owner_token::text, reason, started_at IS NOT NULL
                    FROM ops.system_maintenance
                    WHERE singleton_id = true
                    """
                )
                row = cursor.fetchone()
                assert row is not None
                active, owner_token, reason, has_started_at = row
                assert active is True
                assert owner_token == "00000000-0000-0000-0000-000000000000"
                assert reason == "legacy-incomplete-maintenance-state"
                assert has_started_at is True
                with pytest.raises(psycopg.errors.CheckViolation):
                    cursor.execute(
                        """
                        UPDATE ops.system_maintenance
                        SET owner_token = NULL
                        WHERE singleton_id = true
                        """
                    )
                conn.rollback()
                cursor.execute(
                    """
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid = 'ops.system_maintenance'::regclass
                    ORDER BY conname
                    """
                )
                constraint_names = {row[0] for row in cursor.fetchall()}
                assert "system_maintenance_singleton" in constraint_names
                assert "system_maintenance_state_consistent" in constraint_names
    finally:
        subprocess.run(
            ["docker", "compose", "exec", "-T", "postgres", "dropdb", "-U", db_user, database_name],
            check=False,
            capture_output=True,
        )


async def test_restore_mutex_and_gate_query_failure_are_fail_closed(
    gate_engine: AsyncEngine,
) -> None:
    """第二个 restore 不能接管运行中流程，状态行缺失时在线流量不得放行。"""
    first_gate = MaintenanceGate(gate_engine)
    second_gate = MaintenanceGate(gate_engine)
    restore_entered = asyncio.Event()
    release_restore = asyncio.Event()

    async def _first_restore() -> None:
        async with first_gate.restore(force=False, reason="test-first"):
            restore_entered.set()
            await release_restore.wait()

    first_task = asyncio.create_task(_first_restore())
    await restore_entered.wait()
    try:
        with pytest.raises(RestoreAlreadyRunningError):
            async with second_gate.restore(force=True, reason="test-second"):
                pass
    finally:
        release_restore.set()
        await first_task

    async with gate_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM ops.system_maintenance WHERE singleton_id = true")
        )
    with pytest.raises(MaintenanceGateUnavailableError):
        async with MaintenanceGate(gate_engine).traffic():
            pass
