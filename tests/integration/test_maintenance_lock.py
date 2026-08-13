"""维护任务 advisory lock 生命周期集成测试（生产静默失效修复）。

修复前 run_maintenance 使用 session 级 pg_try_advisory_lock，unlock 可能落在
另一条池化连接上导致锁泄漏——同类型维护任务此后永久返回 busy 且无报错。
修复后改用事务级 pg_try_advisory_xact_lock：锁随事务提交/回滚自动释放，
天然免疫连接池导致的泄漏。

本模块用直接 SQL 确定性验证互斥与释放（跨连接验证），并经 run_maintenance
全流程验证执行后锁已释放。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import MaintenanceCommand
from backend.memory.contracts.common import SYSTEM_MAINTENANCE_USER_ID
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.graph.state import MemoryRuntimeContext
from tests.integration.graph_helpers import make_operation, persist_operation

KIND = "verify_checksums"


async def _try_lock(session: AsyncSession, kind: str = KIND) -> bool:
    result = await session.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:name))"),
        {"name": f"maintenance:{kind}"},
    )
    return bool(result.scalar_one())


async def test_maintenance_lock_mutex_and_auto_release(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """同 kind 并发仅一个获得；事务提交后锁自动释放（跨连接验证）。"""
    async with session_factory() as s1:
        async with s1.begin():
            assert await _try_lock(s1) is True
            async with session_factory() as s2:
                async with s2.begin():
                    assert await _try_lock(s2) is False, "并发同 kind 必须互斥"

    # s1 事务已提交：即使 s3 从池中拿到另一条连接，锁也必须已释放
    async with session_factory() as s3:
        async with s3.begin():
            assert await _try_lock(s3) is True, "事务结束后锁必须自动释放（不得泄漏）"


async def test_maintenance_lock_released_after_rollback(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """事务回滚时锁同样释放（异常路径免疫泄漏）。"""
    async with session_factory() as s1:
        try:
            async with s1.begin():
                assert await _try_lock(s1) is True
                raise RuntimeError("模拟批次异常")
        except RuntimeError:
            pass
    async with session_factory() as s2:
        async with s2.begin():
            assert await _try_lock(s2) is True, "回滚后锁必须已释放"


async def test_maintenance_batch_releases_lock_after_run(
    session_factory: async_sessionmaker[AsyncSession],
    runtime_context: MemoryRuntimeContext,
) -> None:
    """run_maintenance 全流程执行后，同 kind 锁可再次获取（无泄漏）。"""
    operation = make_operation(
        user_id=UUID(SYSTEM_MAINTENANCE_USER_ID),
        actor_type="system",
        input_kind="maintenance",
        operation_type="verify_checksums",
        priority=0,
        payload=MaintenanceCommand(kind="verify_checksums", batch_size=100),
    )
    await persist_operation(session_factory, operation)
    runner = LocalLangGraphRunner(context=runtime_context)
    result = await runner.run(operation)
    assert result.status == "succeeded"

    # 执行完成后锁必须已释放（修复前可能因 unlock 落错连接而泄漏）
    async with session_factory() as s:
        async with s.begin():
            assert await _try_lock(s) is True


async def test_busy_mark_does_not_touch_cursor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """复审 P3：busy 分支只置 running 状态，不得回退持锁实例刚提交的新 cursor。"""
    from uuid import uuid4

    from backend.memory.persistence import maintenance as maintenance_repo

    run_id = uuid4()
    idem_key = f"lock-cursor-test-{uuid4().hex[:8]}"
    operation = make_operation(
        user_id=UUID(SYSTEM_MAINTENANCE_USER_ID),
        actor_type="system",
        input_kind="maintenance",
        operation_type="verify_checksums",
        priority=0,
        payload=MaintenanceCommand(kind="verify_checksums", batch_size=100),
    )
    await persist_operation(session_factory, operation)
    async with session_factory() as s:
        async with s.begin():
            await maintenance_repo.create_or_reuse_run(
                s,
                run_id=run_id,
                maintenance_type="verify_checksums",
                idempotency_key=idem_key,
            )
            await maintenance_repo.attach_operation(
                s, run_id=run_id, operation_id=operation.operation_id
            )
            # 模拟持锁实例先提交了推进后的 cursor
            await maintenance_repo.update_run_by_operation(
                s,
                operation_id=operation.operation_id,
                status="running",
                cursor="u1:m1",
                result={"kind": "verify_checksums", "status": "continue"},
            )
    # busy 分支
    async with session_factory() as s:
        async with s.begin():
            await maintenance_repo.mark_run_busy_by_operation(
                s,
                operation_id=operation.operation_id,
                result={"kind": "verify_checksums", "status": "busy"},
            )
    async with session_factory() as s:
        run = await maintenance_repo.get_run_by_key(s, idempotency_key=idem_key)
        assert run is not None
        assert run["cursor"] == "u1:m1", "busy 写不得回退 cursor"
