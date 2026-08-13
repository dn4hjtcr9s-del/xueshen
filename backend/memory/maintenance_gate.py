"""全局维护门禁：在恢复期间隔离所有在线流量。

门禁状态持久化在不会被覆盖恢复删除的 ``ops`` schema。每个进程以一个带引用
计数的 PostgreSQL advisory shared lock 覆盖本进程全部在途 API 请求、Worker
operation、Outbox tick 或 Scheduler tick；恢复流程持有 exclusive lock。这样既
能等待旧流量排空，也不会让每个请求长期占用一条数据库连接。状态查询或加锁
失败一律 fail-closed，避免在数据可信性未知时继续提供服务。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = logging.getLogger("memory.maintenance_gate")

TRAFFIC_LOCK_NAME = "memory-global-traffic:v1"
RESTORE_LOCK_NAME = "memory-restore:v1"

MAINTENANCE_GATE_DDL: tuple[str, ...] = (
    "CREATE SCHEMA IF NOT EXISTS ops",
    """
    CREATE TABLE IF NOT EXISTS ops.system_maintenance (
        singleton_id boolean PRIMARY KEY DEFAULT true,
        active boolean NOT NULL DEFAULT false,
        owner_token uuid,
        reason text,
        started_at timestamptz,
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT system_maintenance_singleton CHECK (singleton_id),
        CONSTRAINT system_maintenance_state_consistent CHECK (
            (active
                AND owner_token IS NOT NULL
                AND reason IS NOT NULL
                AND started_at IS NOT NULL)
            OR
            (NOT active
                AND owner_token IS NULL
                AND reason IS NULL
                AND started_at IS NULL)
        )
    )
    """,
    """
    INSERT INTO ops.system_maintenance (singleton_id, active)
    VALUES (true, false)
    ON CONFLICT (singleton_id) DO NOTHING
    """,
)


class MaintenanceGateError(RuntimeError):
    """维护门禁拒绝流量或自身不可用。"""


class MaintenanceActiveError(MaintenanceGateError):
    """系统正处于全局维护状态。"""


class MaintenanceGateUnavailableError(MaintenanceGateError):
    """门禁状态无法可靠读取或锁无法可靠获取。"""


class RestoreAlreadyRunningError(MaintenanceGateError):
    """另一个恢复流程已持有全局恢复互斥锁。"""


class RestoreAbortedError(MaintenanceGateError):
    """恢复尚未写入目标，仅因安全前置条件不满足而主动中止。"""


class MaintenanceGate:
    """基于 ops 状态行与 session advisory lock 的全局维护门禁。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        # 双 int advisory key 把锁限定到当前数据库，避免同一 PostgreSQL cluster 中
        # 多个测试库/租户数据库相互阻塞。
        self._instance_id = engine.url.database or "memory"
        self._traffic_mutex = asyncio.Lock()
        self._traffic_users = 0
        self._traffic_connection: AsyncConnection | None = None

    async def is_active(self) -> bool:
        """读取持久状态；查询异常由调用方按 fail-closed 处理。"""
        try:
            async with self._engine.connect() as connection:
                return await self._read_active(connection)
        except MaintenanceGateError:
            raise
        except Exception as exc:
            raise MaintenanceGateUnavailableError("无法读取全局维护状态") from exc

    @asynccontextmanager
    async def traffic(self) -> AsyncIterator[None]:
        """登记一项在线工作；最后一项退出前持续持有本进程共享锁。"""
        await self._enter_traffic()
        try:
            yield
        finally:
            await self._exit_traffic()

    async def _enter_traffic(self) -> None:
        async with self._traffic_mutex:
            # 每一项新工作都重新查 durable 状态；恢复先提交 active=true，故新流量
            # 会立即拒绝，而不是加入恢复正在等待的旧流量集合。
            if await self.is_active():
                raise MaintenanceActiveError("系统正在执行恢复，在线流量已隔离")
            if self._traffic_users == 0:
                connection = await self._engine.connect()
                try:
                    result = await connection.execute(
                        text(
                            "SELECT pg_try_advisory_lock_shared(hashtext(:name), "
                            "hashtext(:instance_id))"
                        ),
                        {"name": TRAFFIC_LOCK_NAME, "instance_id": self._instance_id},
                    )
                    if not bool(result.scalar_one()):
                        raise MaintenanceActiveError("系统正在切换恢复状态，在线流量已隔离")
                    # 消除第一次状态查询和共享锁获取之间的竞态。
                    if await self._read_active(connection):
                        await connection.execute(
                            text(
                                "SELECT pg_advisory_unlock_shared(hashtext(:name), "
                                "hashtext(:instance_id))"
                            ),
                            {"name": TRAFFIC_LOCK_NAME, "instance_id": self._instance_id},
                        )
                        raise MaintenanceActiveError("系统正在执行恢复，在线流量已隔离")
                except BaseException:
                    try:
                        # 释放失败时不要把仍可能持有 advisory lock 的连接放回连接池。
                        await connection.invalidate()
                    except Exception:
                        logger.error("使失败的流量门禁连接失效时出错", exc_info=True)
                    await connection.close()
                    raise
                self._traffic_connection = connection
            self._traffic_users += 1

    async def _exit_traffic(self) -> None:
        async with self._traffic_mutex:
            if self._traffic_users <= 0:
                logger.error("全局流量门禁引用计数下溢")
                return
            self._traffic_users -= 1
            if self._traffic_users != 0:
                return
            connection = self._traffic_connection
            self._traffic_connection = None
            if connection is None:
                logger.error("全局流量门禁缺少共享锁连接")
                return
            try:
                await connection.execute(
                    text(
                        "SELECT pg_advisory_unlock_shared(hashtext(:name), hashtext(:instance_id))"
                    ),
                    {"name": TRAFFIC_LOCK_NAME, "instance_id": self._instance_id},
                )
            except Exception:
                # 物理连接关闭时 PostgreSQL 仍会释放 session lock；连接失效后再归还
                # 连接池，避免一个可能仍持锁的坏连接被复用。
                logger.error("释放全局流量共享锁失败", exc_info=True)
                try:
                    await connection.invalidate()
                except Exception:
                    logger.error("使流量门禁连接失效时出错", exc_info=True)
            finally:
                await connection.close()

    @asynccontextmanager
    async def restore(self, *, force: bool, reason: str) -> AsyncIterator[UUID]:
        """激活持久门禁并独占流量锁；异常退出时故意保持 active=true。

        ``force`` 只允许接管上一次失败遗留的 active 状态；恢复互斥锁仍能阻止
        接管一个尚在运行的恢复流程。只有上下文正常结束，或调用方明确报告尚未
        修改目标的安全中止，才按 owner token 解锁。
        """
        owner_token = uuid4()
        async with self._engine.connect() as connection:
            restore_locked = False
            traffic_locked = False
            original_timeouts: tuple[str, str] | None = None
            timeouts_restored = False
            try:
                # 恢复会等待旧流量排空并执行较长时间，不能继承普通请求的超时。
                # 必须记住物理连接原始配置；AsyncEngine 会把该连接归还池中，若把 0
                # 泄漏给后续请求，会悄悄取消 API/Worker 的超时保护。
                original_timeouts = await self._read_session_timeouts(connection)
                await self._set_session_timeouts(
                    connection, statement_timeout="0", lock_timeout="0"
                )
                for statement in MAINTENANCE_GATE_DDL:
                    await connection.execute(text(statement))
                await connection.commit()

                result = await connection.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:name), hashtext(:instance_id))"),
                    {"name": RESTORE_LOCK_NAME, "instance_id": self._instance_id},
                )
                if not bool(result.scalar_one()):
                    raise RestoreAlreadyRunningError("另一个恢复流程正在运行")
                restore_locked = True

                active = await self._read_active(connection)
                if active and not force:
                    raise MaintenanceActiveError(
                        "全局维护状态已激活；确认上一次恢复失败并准备接管时请使用 --force"
                    )
                await connection.execute(
                    text(
                        """
                        UPDATE ops.system_maintenance
                        SET active = true,
                            owner_token = :owner_token,
                            reason = :reason,
                            started_at = now(),
                            updated_at = now()
                        WHERE singleton_id = true
                        """
                    ),
                    {"owner_token": owner_token, "reason": reason[:500]},
                )
                await connection.commit()

                # active 已持久化后才等待各进程的共享锁排空。
                await connection.execute(
                    text("SELECT pg_advisory_lock(hashtext(:name), hashtext(:instance_id))"),
                    {"name": TRAFFIC_LOCK_NAME, "instance_id": self._instance_id},
                )
                traffic_locked = True
                await connection.commit()

                try:
                    yield owner_token
                except RestoreAbortedError:
                    # 安全前置条件（例如未指定 --force 的非空目标）不满足时，
                    # 尚未写入恢复数据，可以安全撤销本次门禁；真正恢复失败仍保持 active。
                    # 先恢复连接超时，再开放流量，防止同一物理连接带着无限超时回到池中。
                    await self._restore_session_timeouts(connection, original_timeouts)
                    timeouts_restored = True
                    await self._deactivate(connection, owner_token)
                    raise
                except BaseException:
                    # active 状态已经提交，故意不清除；finally 只释放进程级锁。
                    raise
                else:
                    # 成功路径也要先恢复连接会话参数，再清除 durable 门禁。
                    await self._restore_session_timeouts(connection, original_timeouts)
                    timeouts_restored = True
                    await self._deactivate(connection, owner_token)
            finally:
                # 在释放 session advisory lock 前恢复 timeout；否则 timeout 恢复失败
                # 后连接关闭虽会自动解锁，但下面的显式 unlock 会在失效连接上掩盖根因。
                if original_timeouts is not None and not timeouts_restored:
                    try:
                        await self._restore_session_timeouts(connection, original_timeouts)
                    except Exception:
                        # 恢复参数失败时不能把可能已污染的连接归还池；关闭它会同时释放
                        # session advisory lock，而 durable active 状态仍保持 fail-closed。
                        logger.error("恢复连接会话超时失败", exc_info=True)
                        try:
                            await connection.invalidate()
                        except Exception:
                            logger.error("使超时恢复失败的连接失效时出错", exc_info=True)
                        # connection 已无条件释放 session lock；不要再对失效连接发 unlock。
                        traffic_locked = False
                        restore_locked = False
                if traffic_locked:
                    try:
                        await connection.execute(
                            text(
                                "SELECT pg_advisory_unlock(hashtext(:name), hashtext(:instance_id))"
                            ),
                            {"name": TRAFFIC_LOCK_NAME, "instance_id": self._instance_id},
                        )
                    except Exception:
                        logger.error("释放恢复流量排他锁失败", exc_info=True)
                        try:
                            await connection.invalidate()
                        except Exception:
                            logger.error("使恢复流量锁连接失效时出错", exc_info=True)
                if restore_locked:
                    try:
                        await connection.execute(
                            text(
                                "SELECT pg_advisory_unlock(hashtext(:name), hashtext(:instance_id))"
                            ),
                            {"name": RESTORE_LOCK_NAME, "instance_id": self._instance_id},
                        )
                    except Exception:
                        logger.error("释放恢复互斥锁失败", exc_info=True)
                        try:
                            await connection.invalidate()
                        except Exception:
                            logger.error("使恢复互斥锁连接失效时出错", exc_info=True)

    @staticmethod
    async def _read_session_timeouts(connection: AsyncConnection) -> tuple[str, str]:
        """读取并提交连接原有 timeout，供恢复结束后无损归还连接池。"""
        statement_timeout = str(
            (await connection.execute(text("SHOW statement_timeout"))).scalar_one()
        )
        lock_timeout = str((await connection.execute(text("SHOW lock_timeout"))).scalar_one())
        # SHOW 会开启隐式事务；先结束它，后续 SET 才能作为持久 session 配置保存。
        await connection.commit()
        return statement_timeout, lock_timeout

    @staticmethod
    async def _set_session_timeouts(
        connection: AsyncConnection,
        *,
        statement_timeout: str,
        lock_timeout: str,
    ) -> None:
        """用 set_config 设置并提交 session 级 timeout，避免事务结束后配置漂移。"""
        await connection.execute(
            text(
                "SELECT set_config('statement_timeout', :statement_timeout, false), "
                "set_config('lock_timeout', :lock_timeout, false)"
            ),
            {"statement_timeout": statement_timeout, "lock_timeout": lock_timeout},
        )
        await connection.commit()

    @classmethod
    async def _restore_session_timeouts(
        cls,
        connection: AsyncConnection,
        original_timeouts: tuple[str, str] | None,
    ) -> None:
        """恢复进入 restore 前的配置；缺少快照说明设置阶段尚未完成。"""
        if original_timeouts is None:
            return
        # 数据库命令若先失败，事务可能已经 aborted；rollback 后才可安全执行 set_config。
        if connection.in_transaction():
            await connection.rollback()
        await cls._set_session_timeouts(
            connection,
            statement_timeout=original_timeouts[0],
            lock_timeout=original_timeouts[1],
        )

    async def _deactivate(self, connection: AsyncConnection, owner_token: UUID) -> None:
        """仅由当前 owner 清除门禁；CAS 失败按 fail-closed 抛错。"""
        result = await connection.execute(
            text(
                """
                UPDATE ops.system_maintenance
                SET active = false,
                    owner_token = NULL,
                    reason = NULL,
                    started_at = NULL,
                    updated_at = now()
                WHERE singleton_id = true
                  AND active = true
                  AND owner_token = :owner_token
                RETURNING singleton_id
                """
            ),
            {"owner_token": owner_token},
        )
        if result.scalar_one_or_none() is None:
            raise MaintenanceGateUnavailableError("恢复完成时维护门禁 owner 已变化")
        await connection.commit()

    @staticmethod
    async def _read_active(connection: AsyncConnection) -> bool:
        result = await connection.execute(
            text("SELECT active FROM ops.system_maintenance WHERE singleton_id = true")
        )
        active: Any = result.scalar_one_or_none()
        if active is None:
            raise MaintenanceGateUnavailableError("全局维护状态行不存在")
        return bool(active)
