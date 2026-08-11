"""Worker 进程（§14.1 / §11.5）。

- 单进程 asyncio 并发 4，每批领取 10，空队列轮询 1 秒。
- 领取与 Lease 设置同一事务（FOR UPDATE SKIP LOCKED，持久层保证）。
- 心跳 30 秒续约；soft 150 秒告警、hard 180 秒取消协程并停止续约。
- SIGTERM：停止领取 → 等待运行任务最多 30 秒 → 停止续约，由 Scheduler 回收。
"""

from __future__ import annotations

import asyncio
import logging
import random
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.errors import MemoryError
from backend.memory.contracts.operations import MemoryOperation, MemoryOperationResult
from backend.memory.graph.runner import MemoryGraphRunner
from backend.memory.persistence import operations as ops_repo
from backend.memory.worker.retry import (
    FailureAction,
    classify_failure,
    task_backoff_seconds,
)


@dataclass(frozen=True)
class WorkerConfig:
    concurrency: int = 4
    batch_size: int = 10
    poll_interval_seconds: float = 1.0
    lease_seconds: int = 120
    heartbeat_interval_seconds: float = 30.0
    soft_timeout_seconds: float = 150.0
    hard_timeout_seconds: float = 180.0
    shutdown_wait_seconds: float = 30.0


@dataclass
class Worker:
    session_factory: async_sessionmaker[AsyncSession]
    runner: MemoryGraphRunner
    config: WorkerConfig = field(default_factory=WorkerConfig)
    worker_id: str = field(default_factory=lambda: f"worker-{uuid4().hex[:8]}")
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("memory.worker"))

    def __post_init__(self) -> None:
        self._stopping = asyncio.Event()
        self._semaphore = asyncio.Semaphore(self.config.concurrency)
        self._tasks: set[asyncio.Task[None]] = set()
        self._rng = random.Random()

    def request_stop(self) -> None:
        self._stopping.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_stop)

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            claimed = await self._claim_batch()
            if not claimed:
                await asyncio.sleep(self.config.poll_interval_seconds)
                continue
            for row in claimed:
                task = asyncio.create_task(self._execute_guarded(row))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        # 优雅关闭：等待运行任务最多 shutdown_wait 秒（§14.1）
        if self._tasks:
            _done, pending = await asyncio.wait(
                self._tasks, timeout=self.config.shutdown_wait_seconds
            )
            for task in pending:
                task.cancel()  # 停止续约，Lease 过期后由 Scheduler 回收
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _claim_batch(self) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            async with session.begin():
                return await ops_repo.claim_operation(
                    session,
                    worker_id=self.worker_id,
                    lease_seconds=self.config.lease_seconds,
                    batch_size=self.config.batch_size,
                )

    async def _execute_guarded(self, row: dict[str, Any]) -> None:
        async with self._semaphore:
            try:
                await self._execute(row)
            except Exception:
                self.logger.exception("operation 执行出现未捕获异常: %s", row["operation_id"])

    async def _execute(self, row: dict[str, Any]) -> None:
        operation_id = row["operation_id"]
        # DB 行含状态/Lease 等额外列，只取契约字段（extra="forbid"）
        operation = MemoryOperation.model_validate(
            {k: row[k] for k in MemoryOperation.model_fields if k in row}
        )
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(operation_id))
        try:
            # cancel 在 commit 前生效（§23.2）
            async with self.session_factory() as session:
                cancelled = await ops_repo.get_cancel_requested(session, operation_id=operation_id)
            if cancelled:
                await self._complete(
                    operation_id, status="cancelled", result=None, public_error=None
                )
                return

            result = await self._run_with_timeouts(operation)
            await self._complete(
                operation_id,
                status=result.status,
                result=result.model_dump(mode="json"),
                public_error=(result.error.model_dump(mode="json") if result.error else None),
            )
        except Exception as exc:
            await self._handle_failure(row, exc)
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _run_with_timeouts(self, operation: MemoryOperation) -> MemoryOperationResult:
        soft = self.config.soft_timeout_seconds
        hard = self.config.hard_timeout_seconds

        async def _soft_watchdog() -> None:
            await asyncio.sleep(soft)
            self.logger.warning(
                "operation %s 超过 soft timeout（%.0fs），停止非关键工作",
                operation.operation_id,
                soft,
            )

        watchdog = asyncio.create_task(_soft_watchdog())
        try:
            return await asyncio.wait_for(self.runner.run(operation), timeout=hard)
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)

    async def _heartbeat_loop(self, operation_id: Any) -> None:
        while True:
            await asyncio.sleep(self.config.heartbeat_interval_seconds)
            try:
                async with self.session_factory() as session:
                    async with session.begin():
                        ok = await ops_repo.heartbeat(
                            session,
                            operation_id=operation_id,
                            worker_id=self.worker_id,
                            lease_seconds=self.config.lease_seconds,
                        )
                if not ok:
                    return  # Lease 已易主，停止续约
            except Exception:
                self.logger.exception("心跳失败: %s", operation_id)

    async def _complete(
        self,
        operation_id: Any,
        *,
        status: str,
        result: dict[str, Any] | None,
        public_error: dict[str, Any] | None,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                await ops_repo.complete_operation(
                    session,
                    operation_id=operation_id,
                    status=status,
                    result=result,
                    public_error=public_error,
                )

    async def _handle_failure(self, row: dict[str, Any], exc: BaseException) -> None:
        operation_id = row["operation_id"]
        if isinstance(exc, TimeoutError | asyncio.TimeoutError):
            # hard timeout：协程已取消、心跳已停；保持 running 等 Lease 过期回收
            self.logger.warning("operation %s hard timeout，等待 Lease 回收", operation_id)
            return
        action = classify_failure(exc)
        attempt = int(row.get("attempt_count", 1))
        max_attempts = int(row.get("max_attempts", 3))
        now = datetime.now(UTC)
        public_error = _public_error(exc)
        if action is FailureAction.NEEDS_REVIEW:
            await self._complete(
                operation_id, status="needs_review", result=None, public_error=public_error
            )
        elif action is FailureAction.DEAD_LETTER or attempt >= max_attempts:
            await self._complete(
                operation_id, status="dead_letter", result=None, public_error=public_error
            )
        else:
            backoff = task_backoff_seconds(attempt, rng=self._rng)
            async with self.session_factory() as session:
                async with session.begin():
                    await ops_repo.reschedule_operation(
                        session,
                        operation_id=operation_id,
                        next_run_at=now + timedelta(seconds=backoff),
                        status="retry_wait",
                    )


def _public_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, MemoryError):
        return {"code": exc.code, "message": str(exc)[:500]}
    return {"code": "INTERNAL_ERROR", "message": str(exc)[:500]}
