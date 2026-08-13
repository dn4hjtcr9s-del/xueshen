"""Scheduler 进程（§14.3）。

- 独立进程；第一版单实例，仍用 PostgreSQL advisory lock 防误启动多实例。
- memory_maintenance_runs 是调度幂等、batch cursor 和维护任务总状态的唯一真相
  （先创建或复用，带幂等键）；只有需要进入 MemoryManagerGraph 的 batch 才创建
  memory_operations 并通过 operation_id 关联。
- 备份不是 Graph operation：Scheduler 只读 backup_runs，当天未成功则告警。
- verify_checksums（每天 04:00）校验活动版本 checksum 与 current/ 物化副本
  （§14.3，评审 #14 修复接入）。

启动：python -m backend.memory.worker.scheduler
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import MaintenanceCommand
from backend.memory.contracts.common import (
    OPERATION_ROUTING,
    SYSTEM_MAINTENANCE_USER_ID,
    TERMINAL_STATUSES,
    idempotency_payload_hash,
    new_trace_id,
)
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.logging_config import configure_logging
from backend.memory.maintenance_gate import MaintenanceGate, MaintenanceGateError
from backend.memory.persistence import documents as docs_repo
from backend.memory.persistence import maintenance as maintenance_repo
from backend.memory.persistence import notifications as notifications_repo
from backend.memory.persistence import operations as ops_repo
from backend.memory.persistence import outbox as outbox_repo
from backend.memory.worker.checkpoint import (
    list_expired_checkpoint_threads,
    thread_id_for_operation,
)

SYSTEM_USER_ID = UUID(SYSTEM_MAINTENANCE_USER_ID)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SchedulerConfig:
    """§14.3 时间表与有界 batch 配置。"""

    timezone: str = "Asia/Shanghai"
    tick_seconds: float = 1.0
    batch_size: int = 100
    continuation_seconds: float = 30.0
    notification_retention_days: int = 90
    notification_purge_max_batches: int = 10


@dataclass(frozen=True)
class ScheduledTask:
    """interval_seconds 与 daily_at 二选一（daily_at 为 config.timezone 本地时间）。"""

    name: str
    interval_seconds: float | None = None
    daily_at: time | None = None


#: §14.3 任务表
TASKS: tuple[ScheduledTask, ...] = (
    ScheduledTask("recover_operation_leases", interval_seconds=30),
    ScheduledTask("recover_outbox_leases", interval_seconds=30),
    ScheduledTask("schedule_index_rebuilds", interval_seconds=300),
    ScheduledTask("check_dead_letters", interval_seconds=300),
    ScheduledTask("cleanup_orphan_versions", daily_at=time(2, 30)),
    ScheduledTask("purge_tombstones", daily_at=time(3, 0)),
    ScheduledTask("cleanup_checkpoints", daily_at=time(3, 30)),
    ScheduledTask("purge_notifications", daily_at=time(3, 45)),
    ScheduledTask("verify_checksums", daily_at=time(4, 0)),
    ScheduledTask("check_backup_runs", daily_at=time(5, 0)),
)


@dataclass
class Scheduler:
    session_factory: async_sessionmaker[AsyncSession]
    config: SchedulerConfig = field(default_factory=SchedulerConfig)
    clock: Callable[[], datetime] = _utc_now
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("memory.scheduler"))
    maintenance_gate: MaintenanceGate | None = None

    def __post_init__(self) -> None:
        self._stopping = asyncio.Event()
        self._next_due: dict[str, datetime] = {}

    def request_stop(self) -> None:
        self._stopping.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_stop)

    # ------------------------------------------------------------------
    # 调度循环
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        # advisory lock 防多实例重复调度（§14.3）；lock 为 session 级，随进程退出释放
        async with self.session_factory() as lock_session:
            result = await lock_session.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:name))"),
                {"name": "memory-scheduler:v1"},
            )
            if not bool(result.scalar_one()):
                self.logger.error("已有 Scheduler 实例持有 advisory lock，本实例退出")
                return
            try:
                while not self._stopping.is_set():
                    await self.tick()
                    await asyncio.sleep(self.config.tick_seconds)
            finally:
                await lock_session.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:name))"),
                    {"name": "memory-scheduler:v1"},
                )

    async def tick(self, now: datetime | None = None) -> list[str]:
        """单次调度 tick：执行所有到期任务，返回执行的任务名。"""
        if self.maintenance_gate is not None:
            try:
                async with self.maintenance_gate.traffic():
                    return await self._tick_ungated(now)
            except MaintenanceGateError:
                self.logger.info("全局维护中，Scheduler 停止调度")
                return []
        return await self._tick_ungated(now)

    async def _tick_ungated(self, now: datetime | None = None) -> list[str]:
        """在已通过 maintenance gate 后执行到期任务。"""
        now = now or self.clock()
        self._ensure_initialized(now)
        ran: list[str] = []
        for task in TASKS:
            if now < self._next_due[task.name]:
                continue
            has_more = await self.run_task(task.name, now)
            if task.interval_seconds is not None:
                self._next_due[task.name] = now + timedelta(seconds=task.interval_seconds)
            elif has_more:
                # 日任务 run 未完成（cursor 待续）：按 continuation 间隔继续调度下一批
                self._next_due[task.name] = now + timedelta(
                    seconds=self.config.continuation_seconds
                )
            else:
                assert task.daily_at is not None
                self._next_due[task.name] = self._next_daily(now, task.daily_at)
            ran.append(task.name)
        return ran

    def _ensure_initialized(self, now: datetime) -> None:
        for task in TASKS:
            if task.name in self._next_due:
                continue
            if task.interval_seconds is not None:
                self._next_due[task.name] = now  # 间隔任务启动即到期
            else:
                assert task.daily_at is not None
                self._next_due[task.name] = self._next_daily(now, task.daily_at)

    def _next_daily(self, now: datetime, at: time) -> datetime:
        tz = ZoneInfo(self.config.timezone)
        local = now.astimezone(tz)
        candidate = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    def _local_date(self, now: datetime) -> str:
        return now.astimezone(ZoneInfo(self.config.timezone)).date().isoformat()

    async def run_task(self, name: str, now: datetime) -> bool:
        """执行单个任务；返回 True 表示有关联 run 未完成、需按 continuation 间隔续跑。"""
        handler = getattr(self, f"_task_{name}")
        try:
            return bool(await handler(now))
        except Exception:
            self.logger.exception("调度任务执行失败：%s", name)
            return False

    # ------------------------------------------------------------------
    # 间隔任务：Lease 回收 / dirty index / dead letter 指标
    # ------------------------------------------------------------------

    async def _task_recover_operation_leases(self, now: datetime) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                recovered = await ops_repo.recover_expired_leases(session)
        if recovered:
            self.logger.warning("回收过期 operation Lease：%d 个", recovered)
        return False

    async def _task_recover_outbox_leases(self, now: datetime) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                recovered = await outbox_repo.recover_expired_leases(session)
        if recovered:
            self.logger.warning("回收过期 Outbox Lease：%d 个", recovered)
        return False

    async def _task_schedule_index_rebuilds(self, now: datetime) -> bool:
        has_more = False
        async with self.session_factory() as session:
            async with session.begin():
                dirty = await docs_repo.list_dirty_indexes(
                    session, batch_size=self.config.batch_size
                )
                for row in dirty:
                    key = f"rebuild-index:{row['user_id']}:{row['index_dirty_at'].isoformat()}"
                    run, _created = await maintenance_repo.create_or_reuse_run(
                        session,
                        run_id=uuid4(),
                        maintenance_type="rebuild_index",
                        idempotency_key=key,
                    )
                    user_id = UUID(str(row["user_id"]))

                    def rebuild_payload(
                        cursor: str | None, uid: UUID = user_id
                    ) -> MaintenanceCommand:
                        return MaintenanceCommand(kind="rebuild_index", target_user_id=uid)

                    outcome = await self._ensure_graph_batch(
                        session,
                        run=run,
                        operation_type="rebuild_index",
                        user_id=user_id,
                        payload_factory=rebuild_payload,
                    )
                    has_more = has_more or outcome != "done"
        return has_more

    async def _task_check_dead_letters(self, now: datetime) -> bool:
        async with self.session_factory() as session:
            counts = await maintenance_repo.count_dead_letters(session)
        if counts["operations"] or counts["outbox"]:
            self.logger.error(
                "告警：dead letter 指标非零：operations=%d, outbox=%d",
                counts["operations"],
                counts["outbox"],
            )
        return False

    # ------------------------------------------------------------------
    # 日任务：Graph 维护 batch / 通知清理 / 备份检查
    # ------------------------------------------------------------------

    async def _task_cleanup_orphan_versions(self, now: datetime) -> bool:
        date = self._local_date(now)
        has_more = False
        async with self.session_factory() as session:
            async with session.begin():
                users = await maintenance_repo.list_document_user_ids(
                    session, batch_size=self.config.batch_size
                )
                for user_id in users:
                    run, _created = await maintenance_repo.create_or_reuse_run(
                        session,
                        run_id=uuid4(),
                        maintenance_type="cleanup_orphan_versions",
                        idempotency_key=f"cleanup-orphan-versions:{user_id}:{date}",
                    )

                    def orphan_payload(
                        cursor: str | None, uid: UUID = user_id
                    ) -> MaintenanceCommand:
                        return MaintenanceCommand(
                            kind="cleanup_orphan_versions",
                            target_user_id=uid,
                            cursor=cursor,
                            batch_size=self.config.batch_size,
                        )

                    outcome = await self._ensure_graph_batch(
                        session,
                        run=run,
                        operation_type="cleanup_orphan_versions",
                        user_id=user_id,
                        payload_factory=orphan_payload,
                    )
                    has_more = has_more or outcome != "done"
        return has_more

    async def _task_purge_tombstones(self, now: datetime) -> bool:
        date = self._local_date(now)
        key = f"purge-tombstones:{date}"
        outcome: Literal["scheduled", "waiting", "done"] = "done"
        async with self.session_factory() as session:
            async with session.begin():
                run, _created = await maintenance_repo.create_or_reuse_run(
                    session,
                    run_id=uuid4(),
                    maintenance_type="purge_tombstones",
                    idempotency_key=key,
                )
                if run["operation_id"] is None and run["status"] == "queued":
                    rows = await docs_repo.list_expired_tombstones(
                        session, now=now, batch_size=1, cursor=run["cursor"]
                    )
                    if not rows:
                        # 无工作：不创建空 operation，run 直接幂等成功
                        await maintenance_repo.complete_run(
                            session,
                            run_id=run["run_id"],
                            status="succeeded",
                            cursor=None,
                            result={"skipped": "no_expired_tombstones"},
                        )
                        return False
                outcome = await self._ensure_graph_batch(
                    session,
                    run=run,
                    operation_type="purge_tombstones",
                    user_id=SYSTEM_USER_ID,
                    payload_factory=lambda cursor: MaintenanceCommand(
                        kind="purge_tombstones", cursor=cursor, batch_size=self.config.batch_size
                    ),
                )
        return outcome != "done"

    async def _task_cleanup_checkpoints(self, now: datetime) -> bool:
        date = self._local_date(now)
        key = f"cleanup-checkpoints:{date}"
        outcome: Literal["scheduled", "waiting", "done"] = "done"
        async with self.session_factory() as session:
            async with session.begin():
                run, _created = await maintenance_repo.create_or_reuse_run(
                    session,
                    run_id=uuid4(),
                    maintenance_type="cleanup_checkpoints",
                    idempotency_key=key,
                )
                if run["operation_id"] is None and run["status"] == "queued":
                    rows = await list_expired_checkpoint_threads(
                        session, now=now, batch_size=1, cursor=run["cursor"]
                    )
                    if not rows:
                        await maintenance_repo.complete_run(
                            session,
                            run_id=run["run_id"],
                            status="succeeded",
                            cursor=None,
                            result={"skipped": "no_expired_checkpoints"},
                        )
                        return False
                outcome = await self._ensure_graph_batch(
                    session,
                    run=run,
                    operation_type="cleanup_checkpoints",
                    user_id=SYSTEM_USER_ID,
                    payload_factory=lambda cursor: MaintenanceCommand(
                        kind="cleanup_checkpoints",
                        cursor=cursor,
                        batch_size=self.config.batch_size,
                    ),
                )
        return outcome != "done"

    async def _task_verify_checksums(self, now: datetime) -> bool:
        date = self._local_date(now)
        key = f"verify-checksums:{date}"
        outcome: Literal["scheduled", "waiting", "done"] = "done"
        async with self.session_factory() as session:
            async with session.begin():
                run, _created = await maintenance_repo.create_or_reuse_run(
                    session,
                    run_id=uuid4(),
                    maintenance_type="verify_checksums",
                    idempotency_key=key,
                )
                if run["operation_id"] is None and run["status"] == "queued":
                    rows = await docs_repo.list_active_documents_page(
                        session, batch_size=1, cursor=run["cursor"]
                    )
                    if not rows:
                        # 无工作：不创建空 operation，run 直接幂等成功
                        await maintenance_repo.complete_run(
                            session,
                            run_id=run["run_id"],
                            status="succeeded",
                            cursor=None,
                            result={"skipped": "no_active_documents"},
                        )
                        return False
                outcome = await self._ensure_graph_batch(
                    session,
                    run=run,
                    operation_type="verify_checksums",
                    user_id=SYSTEM_USER_ID,
                    payload_factory=lambda cursor: MaintenanceCommand(
                        kind="verify_checksums", cursor=cursor, batch_size=self.config.batch_size
                    ),
                )
        return outcome != "done"

    async def _task_purge_notifications(self, now: datetime) -> bool:
        """清理超过 90 天的用户通知（§13.13）；不进入 Graph，run 由 Scheduler 直接收尾。"""
        date = self._local_date(now)
        key = f"purge-notifications:{date}"
        cutoff = now - timedelta(days=self.config.notification_retention_days)
        total = 0
        async with self.session_factory() as session:
            async with session.begin():
                run, _created = await maintenance_repo.create_or_reuse_run(
                    session,
                    run_id=uuid4(),
                    maintenance_type="purge_notifications",
                    idempotency_key=key,
                )
                if run["status"] in ("succeeded", "failed"):
                    return False
                for _ in range(self.config.notification_purge_max_batches):
                    deleted = await notifications_repo.purge_older_than(
                        session, cutoff=cutoff, batch_size=self.config.batch_size
                    )
                    total += deleted
                    if deleted < self.config.batch_size:
                        break
                await maintenance_repo.complete_run(
                    session,
                    run_id=run["run_id"],
                    status="succeeded",
                    cursor=None,
                    result={
                        "deleted": total,
                        "retention_days": self.config.notification_retention_days,
                    },
                )
        if total:
            self.logger.info("清理 90 天前用户通知：%d 条", total)
        return False

    async def _task_check_backup_runs(self, now: datetime) -> bool:
        """只读 backup_runs 并告警；备份执行不由 Scheduler 发起（§14.3）。"""
        tz = ZoneInfo(self.config.timezone)
        date = self._local_date(now)
        key = f"backup-check:{date}"
        day_start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.session_factory() as session:
            async with session.begin():
                run, _created = await maintenance_repo.create_or_reuse_run(
                    session,
                    run_id=uuid4(),
                    maintenance_type="backup_check",
                    idempotency_key=key,
                )
                if run["status"] in ("succeeded", "failed"):
                    return False
                ok = await maintenance_repo.has_successful_backup_since(
                    session, since=day_start.astimezone(UTC)
                )
                await maintenance_repo.complete_run(
                    session,
                    run_id=run["run_id"],
                    status="succeeded",
                    cursor=None,
                    result={"backup_succeeded_today": ok, "date": date},
                )
        if not ok:
            self.logger.error("告警：当天（%s）无成功的 backup_runs 记录", date)
        return False

    # ------------------------------------------------------------------
    # Graph batch 调度（§14.3）
    # ------------------------------------------------------------------

    async def _ensure_graph_batch(
        self,
        session: AsyncSession,
        *,
        run: dict[str, Any],
        operation_type: str,
        user_id: UUID,
        payload_factory: Callable[[str | None], MaintenanceCommand],
    ) -> Literal["scheduled", "waiting", "done"]:
        """推进 run 的一个 Graph batch；只有进入 Graph 的 batch 才创建 operation。"""
        if run["status"] in ("succeeded", "failed"):
            return "done"
        if run["operation_id"] is not None:
            op = await ops_repo.get_operation(session, run["operation_id"])
            if op is not None and op["status"] not in TERMINAL_STATUSES:
                return "waiting"
            if op is None or op["status"] != "succeeded":
                await maintenance_repo.complete_run(
                    session,
                    run_id=run["run_id"],
                    status="failed",
                    cursor=run["cursor"],
                    result={"operation_status": op["status"] if op else "missing"},
                )
                return "done"
            if run["status"] == "queued":
                # operation 成功但 graph 未回写 run（异常路径）：按失败收尾，避免静默卡住
                await maintenance_repo.complete_run(
                    session,
                    run_id=run["run_id"],
                    status="failed",
                    cursor=run["cursor"],
                    result={"reason": "operation_succeeded_without_run_update"},
                )
                return "done"
            # run['status'] == 'running'：上一批完成且 cursor 待续，落到下方调度下一批
        cursor = run["cursor"]
        payload = payload_factory(cursor)
        base_key = f"{run['idempotency_key']}:{cursor or 'initial'}"
        existing = await ops_repo.get_by_idempotency(
            session, user_id=user_id, actor_type="system", idempotency_key=base_key
        )
        if existing is not None and existing["status"] not in TERMINAL_STATUSES:
            if run["operation_id"] is None:
                await maintenance_repo.attach_operation(
                    session, run_id=run["run_id"], operation_id=existing["operation_id"]
                )
            return "waiting"
        # 同 cursor 批次重排（如上一次 busy/失败）：换 key 避免复用已终结 operation
        key = base_key if existing is None else f"{base_key}:retry-{uuid4().hex[:8]}"
        operation_id = uuid4()
        input_kind, priority = OPERATION_ROUTING[operation_type]
        operation = MemoryOperation(
            operation_id=operation_id,
            idempotency_key=key,
            user_id=user_id,
            actor_type="system",
            input_kind=input_kind,
            operation_type=operation_type,  # type: ignore[arg-type]
            priority=priority,
            occurred_at=self.clock(),
            payload=payload,
            trace_id=new_trace_id(),
            graph_thread_id=thread_id_for_operation(operation_id),
        )
        await ops_repo.insert_operation(
            session,
            operation,
            idempotency_payload_hash=idempotency_payload_hash(payload.model_dump(mode="json")),
        )
        await maintenance_repo.attach_operation(
            session, run_id=run["run_id"], operation_id=operation_id
        )
        return "scheduled"


async def _run() -> None:
    from backend.memory.persistence.database import Database
    from backend.settings import get_settings

    settings = get_settings()
    configure_logging(settings)
    db = Database(settings)
    maintenance_gate = MaintenanceGate(db.engine)
    try:
        scheduler = Scheduler(
            session_factory=db.session_factory,
            config=SchedulerConfig(
                timezone=settings.memory_scheduler_timezone,
                notification_retention_days=settings.memory_notification_retention_days,
            ),
            maintenance_gate=maintenance_gate,
        )
        scheduler.install_signal_handlers()
        await scheduler.run_forever()
    finally:
        await db.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
