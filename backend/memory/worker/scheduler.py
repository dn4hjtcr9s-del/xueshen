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
import hashlib
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

from backend.memory import metrics
from backend.memory.contracts.batch import (
    BATCH_IDEMPOTENCY_KEY_TEMPLATE,
    BATCH_OPERATION_TYPE,
    BatchCursor,
)
from backend.memory.contracts.commands import MaintenanceCommand, SummarizeUserMemoryBatchCommand
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


def _batch_cursor_tuple(value: str | None) -> tuple[datetime, datetime, UUID] | None:
    """``memory_maintenance_runs.cursor`` → ``list_pending_batch_members`` 的 after 三元组。

    游标为空表示该用户本日还没有批次（从头开始扫）。损坏的游标按契约错误上抛
    （由 ``run_task`` 记日志），**不做静默降级**：静默从头扫会把已入批的证据重复入批。
    """
    if not value:
        return None
    cursor = BatchCursor.decode(value)
    return (cursor.eligible_at, cursor.created_at, cursor.operation_id)


#: 批次 operation 幂等键里的游标摘要长度（十六进制字符）。
_BATCH_CURSOR_DIGEST_CHARS = 16


def _batch_operation_key(*, run_key: str, cursor: str | None) -> str:
    """批次 operation 的幂等键：同一 (run, cursor) 恒得同一键，且长度可控。

    ``_ensure_graph_batch`` 的规则是 ``{run 幂等键}:{cursor or 'initial'}``，但证据池的
    cursor 是 :class:`BatchCursor` 的 canonical JSON（150+ 字符），拼上
    ``summarize:{user_id}:{date}`` 会超过 ``memory_operations.idempotency_key``
    （Pydantic 与 DB 列同为 200 字符）的上限。因此这里改用游标字符串的 SHA-256 前 16 位
    十六进制：仍是 cursor 的确定性函数（幂等语义不变），长度固定且加上 ``retry-``
    后缀也不会越界。
    """
    if not cursor:
        return f"{run_key}:initial"
    digest = hashlib.sha256(cursor.encode("utf-8")).hexdigest()[:_BATCH_CURSOR_DIGEST_CHARS]
    return f"{run_key}:{digest}"


@dataclass(frozen=True)
class SchedulerConfig:
    """§14.3 时间表与有界 batch 配置。"""

    timezone: str = "Asia/Shanghai"
    tick_seconds: float = 1.0
    batch_size: int = 100
    continuation_seconds: float = 30.0
    notification_retention_days: int = 90
    notification_purge_max_batches: int = 10
    #: memory-rebuild §5.6：v1→v2 文档迁移任务的门控（默认关闭）。
    #: "实现不等批准，启用必须等批准"——关闭时任务不建 run、不产生任何调度。
    schema_v2_migration_enabled: bool = False
    #: memory-rebuild §2.6：证据池 nightly 批量的门控与有界认领参数。
    #: 门控关闭时 `summarize_pending_evidence` 不建 run、不查询、无任何调度副作用。
    evidence_batch_enabled: bool = False
    #: §2.6 D4：每日批量触发时刻（config.timezone 本地时间，是配置项而非固定 0 点）。
    summary_daily_time: time = time(0, 0)
    #: §2.6 D4：单批证据上限（有界认领三件套之一）。
    batch_max_evidence: int = 50
    #: §2.6 D4：单 run 最多处理的用户数（有界认领三件套之一）。
    batch_max_users_per_run: int = 50


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
    # memory-rebuild §5.6 Phase 4：把 v1 活动文档逐个追加 v2 版本。
    # 门控 memory_schema_v2_migration_enabled（默认 false），关闭时不产生任何调度。
    ScheduledTask("migrate_markdown_schema_v2", daily_at=time(4, 15)),
    # 认证会话清理（方案 §4.4 / 附录 A.2 #8）：过期超过 30 天的 refresh family
    ScheduledTask("cleanup_expired_refresh_families", daily_at=time(4, 30)),
    ScheduledTask("check_backup_runs", daily_at=time(5, 0)),
    # memory-rebuild §2.6 Phase 6：0 点把门控到点的 pending_batch 证据按用户聚合成
    # 批量 operation。门控 memory_batch_enabled（默认 false）；这里的 daily_at 只是
    # **声明式默认值**，真实触发时刻取 config.summary_daily_time（见 _daily_at_for）。
    ScheduledTask("summarize_pending_evidence", daily_at=time(0, 0)),
)


@dataclass
class Scheduler:
    session_factory: async_sessionmaker[AsyncSession]
    config: SchedulerConfig = field(default_factory=SchedulerConfig)
    clock: Callable[[], datetime] = _utc_now
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("memory.scheduler"))
    maintenance_gate: MaintenanceGate | None = None
    #: auth 库会话工厂（方案 §4.4 / 附录 A.2 #8）：仅用于 refresh family 清理；
    #: 未装配时跳过该任务（auth 库不可用不阻塞 memory 调度）
    auth_session_factory: async_sessionmaker[AsyncSession] | None = None

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
                self._next_due[task.name] = self._next_daily(now, self._daily_at_for(task))
            ran.append(task.name)
        return ran

    def _ensure_initialized(self, now: datetime) -> None:
        for task in TASKS:
            if task.name in self._next_due:
                continue
            if task.interval_seconds is not None:
                self._next_due[task.name] = now  # 间隔任务启动即到期
            else:
                self._next_due[task.name] = self._next_daily(now, self._daily_at_for(task))

    def _daily_at_for(self, task: ScheduledTask) -> time:
        """日任务的真实触发时刻。

        ``TASKS`` 只是声明表：``summarize_pending_evidence`` 的触发时刻是**配置项**
        （``memory_summary_daily_time``，§2.6 D4 参数化），TASKS 里的 ``time(0, 0)``
        只是声明式默认值。因此这里按任务名收敛到 config，避免"配置改了但调度仍按
        静态时间触发"。
        """
        if task.name == "summarize_pending_evidence":
            return self.config.summary_daily_time
        assert task.daily_at is not None
        return task.daily_at

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
            # memory-rebuild §2.6「metrics 增加 pending_batch 在途 gauge」：搭在既有的
            # 5 分钟监控 tick 上刷新，**不新增 TASKS 项**（§14.3 的任务表保持不变）。
            total, due = await ops_repo.count_pending_batch_evidence(session, now=now)
        metrics.memory_pending_batch_depth.labels(state="pending").set(total)
        metrics.memory_pending_batch_depth.labels(state="due").set(due)
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

    async def _task_migrate_markdown_schema_v2(self, now: datetime) -> bool:
        """把 v1 活动文档逐个追加 v2 版本（§5.6）。

        与 verify_checksums 同构：一次 run + 全局文档 cursor 续跑，跑不完下个周期接着跑。
        **门控**：``memory_schema_v2_migration_enabled`` 关闭时直接返回，不建 run，
        因此默认部署下本任务完全不可见。
        """
        if not self.config.schema_v2_migration_enabled:
            return False
        date = self._local_date(now)
        key = f"migrate-markdown-schema-v2:{date}"
        outcome: Literal["scheduled", "waiting", "done"] = "done"
        async with self.session_factory() as session:
            async with session.begin():
                run, _created = await maintenance_repo.create_or_reuse_run(
                    session,
                    run_id=uuid4(),
                    maintenance_type="migrate_markdown_schema_v2",
                    idempotency_key=key,
                )
                if run["operation_id"] is None and run["status"] == "queued":
                    rows = await docs_repo.list_active_documents_page(
                        session, batch_size=1, cursor=run["cursor"]
                    )
                    if not rows:
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
                    operation_type="migrate_markdown_schema_v2",
                    user_id=SYSTEM_USER_ID,
                    payload_factory=lambda cursor: MaintenanceCommand(
                        kind="migrate_markdown_schema_v2",
                        cursor=cursor,
                        batch_size=self.config.batch_size,
                    ),
                )
        return outcome != "done"

    async def _task_summarize_pending_evidence(self, now: datetime) -> bool:
        """0 点把门控到点的证据按用户聚合成批量 operation（memory-rebuild §2.6 / §5.8）。

        状态机第 2 步：扫 ``status='pending_batch' AND next_run_at <= now()`` 的证据，
        按 user_id 分组，每用户生成一个 ``summarize_user_memory_batch`` 批量 operation，
        成员行写 ``batch_operation_id`` 建立归属。幂等键 ``summarize:{user_id}:{date}``
        落在 memory_maintenance_runs 上——同一用户当天只有一个 run，跑不完的批次靠
        run.cursor 续跑（``list_pending_batch_members(after=...)`` 行值比较，不用 OFFSET）。

        **门控**：``memory_batch_enabled`` 关闭时立即返回，不建 run、不查询、不产生任何
        调度副作用（与 ``_task_migrate_markdown_schema_v2`` 同构）。

        返回值沿用日任务约定：True 表示 run 还有待续批次，按 continuation 间隔继续调度。
        """
        if not self.config.evidence_batch_enabled:
            return False
        date = self._local_date(now)
        has_more = False
        async with self.session_factory() as session:
            async with session.begin():
                user_ids = await ops_repo.list_pending_batch_user_ids(
                    session, now=now, limit=self.config.batch_max_users_per_run
                )
                for user_id in user_ids:
                    user_has_more = await self._summarize_user_batch(
                        session, user_id=user_id, date=date, now=now
                    )
                    has_more = has_more or user_has_more
        return has_more

    async def _summarize_user_batch(
        self, session: AsyncSession, *, user_id: UUID, date: str, now: datetime
    ) -> bool:
        """单个用户的一轮入批；返回该用户的 run 是否还有待续批次。

        判定复用 ``_resolve_run_batch_state``（与 ``_ensure_graph_batch`` 同源）：
        run 已终结 → 无待续；在途批次 → 待续；上一批已成功 → 用 run.cursor 续排下一批。
        """
        run, _created = await maintenance_repo.create_or_reuse_run(
            session,
            run_id=uuid4(),
            maintenance_type="summarize_pending_evidence",
            idempotency_key=BATCH_IDEMPOTENCY_KEY_TEMPLATE.format(user_id=user_id, date=date),
        )
        state = await self._resolve_run_batch_state(session, run)
        if state == "done":
            return False
        if state == "waiting":
            return True
        members = await ops_repo.list_pending_batch_members(
            session,
            user_id=user_id,
            now=now,
            limit=self.config.batch_max_evidence,
            after=_batch_cursor_tuple(run["cursor"]),
        )
        if not members:
            # 并发实例可能在本事务读到 run 之后刚建批次并领走成员（advisory lock 之外的
            # 防御，§5.8"多副本下同一 evidence 只被一个批次 claim"）：重读一次 run，
            # 确认确实没有在途 operation 才把 run 收尾，避免把别人的在途批次判成"无证据"。
            refreshed = await maintenance_repo.get_run_by_key(
                session, idempotency_key=run["idempotency_key"]
            )
            if refreshed is not None and refreshed["operation_id"] is not None:
                op = await ops_repo.get_operation(session, refreshed["operation_id"])
                if op is not None and op["status"] not in TERMINAL_STATUSES:
                    return True
            await maintenance_repo.complete_run(
                session,
                run_id=run["run_id"],
                status="succeeded",
                cursor=run["cursor"],
                result={"reason": "no_pending_evidence"},
            )
            return False
        member_ids = [UUID(str(row["operation_id"])) for row in members]
        batch_operation_id = await self._create_batch_operation(
            session, run=run, user_id=user_id, member_ids=member_ids
        )
        if batch_operation_id is None:
            # 没有真正落地新批次（并发实例抢先建了同 cursor 批次，或整批成员被抢走）：
            # 不推进 cursor、不挂 run——证据归属已由对方批次负责，下一轮再续。
            return False
        last = members[-1]
        cursor = BatchCursor(
            eligible_at=last["next_run_at"],
            created_at=last["created_at"],
            operation_id=UUID(str(last["operation_id"])),
        ).encode()
        # run 的状态与游标由本任务独占维护：批量图不回写 memory_maintenance_runs
        # （成员归属与批次结果都在 operation 上），因此这里用维护任务的既有语义把 run
        # 置 running + cursor 待续；下一轮若该 operation 已成功即据此排下一批。
        await maintenance_repo.update_run_by_operation(
            session,
            operation_id=batch_operation_id,
            status="running",
            cursor=cursor,
            result={"scheduled_members": len(member_ids)},
        )
        return True

    async def _create_batch_operation(
        self,
        session: AsyncSession,
        *,
        run: dict[str, Any],
        user_id: UUID,
        member_ids: list[UUID],
    ) -> UUID | None:
        """建批次 operation 并把成员证据归属到它（§2.6 状态机第 2 步）。

        幂等键沿用 ``_ensure_graph_batch`` 的形状（``{run 幂等键}:{游标判别式}``，见
        :func:`_batch_operation_key`）：同一 run 的同一 cursor 只对应一个批次；该键上的
        上一批已终结时换 ``retry-`` 后缀，避免复用已终结的 operation。

        批次自身是 ``queued``（可被 Worker 正常认领的任务），``pending_batch`` 只是成员
        证据的沉淀态。返回 None 表示没有新批次落地：已有在途批次，或成员被并发批次整批
        抢走（此时把刚建的 operation 取消，且**不**挂到 run 上——挂上去会让
        ``_resolve_run_batch_state`` 把 run 误判为 failed，断掉该用户当天的续跑）。
        """
        base_key = _batch_operation_key(run_key=run["idempotency_key"], cursor=run["cursor"])
        existing = await ops_repo.get_by_idempotency(
            session, user_id=user_id, actor_type="system", idempotency_key=base_key
        )
        if existing is not None and existing["status"] not in TERMINAL_STATUSES:
            if run["operation_id"] is None:
                # 与 _ensure_graph_batch 一致：并发实例已建批次，这里只补挂 run 关联
                await maintenance_repo.attach_operation(
                    session, run_id=run["run_id"], operation_id=existing["operation_id"]
                )
            return None
        key = base_key if existing is None else f"{base_key}:retry-{uuid4().hex[:8]}"
        batch_operation_id = uuid4()
        payload = SummarizeUserMemoryBatchCommand(
            target_user_id=user_id,
            batch_operation_id=batch_operation_id,
            member_operation_ids=member_ids,
            max_evidence=self.config.batch_max_evidence,
        )
        input_kind, priority = OPERATION_ROUTING[BATCH_OPERATION_TYPE]
        operation = MemoryOperation(
            operation_id=batch_operation_id,
            idempotency_key=key,
            user_id=user_id,
            actor_type="system",
            input_kind=input_kind,
            operation_type=payload.kind,
            priority=priority,
            occurred_at=self.clock(),
            payload=payload,
            trace_id=new_trace_id(),
            graph_thread_id=thread_id_for_operation(batch_operation_id),
        )
        inserted = await ops_repo.insert_operation(
            session,
            operation,
            idempotency_payload_hash=idempotency_payload_hash(payload.model_dump(mode="json")),
        )
        if not inserted:
            # 并发实例抢先用同一幂等键建了批次：本实例让位，成员归属由它负责
            return None
        assigned = await ops_repo.assign_batch_members(
            session, batch_operation_id=batch_operation_id, member_operation_ids=member_ids
        )
        if assigned == 0:
            # 整批成员已被并发批次的其它幂等键领走：作废刚建的 operation
            await ops_repo.request_cancel(session, operation_id=batch_operation_id)
            return None
        await maintenance_repo.attach_operation(
            session, run_id=run["run_id"], operation_id=batch_operation_id
        )
        return batch_operation_id

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

    async def _task_cleanup_expired_refresh_families(self, now: datetime) -> bool:
        """删除过期超过 30 天的 refresh family（方案 §4.4 / 附录 A.2 #8）。

        auth 库不可用或未装配时不阻塞 memory 调度（run_task 已兜底异常日志）。
        """
        if self.auth_session_factory is None:
            return False
        from backend.auth_service.session import delete_expired_families

        async with self.auth_session_factory() as session:
            async with session.begin():
                deleted = await delete_expired_families(session)
        if deleted:
            self.logger.info("清理过期 refresh family：%d 行", deleted)
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

    async def _resolve_run_batch_state(
        self, session: AsyncSession, run: dict[str, Any]
    ) -> Literal["continue", "waiting", "done"]:
        """判断 run 当前能否再调度一个 Graph batch（§14.3）。

        ``_ensure_graph_batch`` 与证据池批量任务共用这段判定，避免两处语义漂移：

        - run 已 ``succeeded``/``failed`` → ``done``；
        - 关联 operation 在途 → ``waiting``；
        - operation 已终结但不是成功（含行丢失）→ 把 run 收尾为 ``failed`` → ``done``；
        - operation 成功但 run 仍 ``queued``（graph 未回写，异常路径）→ 同样按失败收尾；
        - 上一批成功且 cursor 待续 → ``continue``。
        """
        if run["status"] in ("succeeded", "failed"):
            return "done"
        if run["operation_id"] is None:
            return "continue"
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
        return "continue"

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
        state = await self._resolve_run_batch_state(session, run)
        if state == "done":
            return "done"
        if state == "waiting":
            return "waiting"
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


def _config_from_settings() -> SchedulerConfig:
    """从 Settings（环境变量 / .env）构造 SchedulerConfig。

    §2.6 D4 的批量参数与门控是配置项，不能写死在代码里；未在 settings 暴露的调度细项
    （tick_seconds / batch_size / continuation_seconds）沿用 dataclass 默认值。
    """
    from backend.settings import get_settings

    settings = get_settings()
    return SchedulerConfig(
        timezone=settings.memory_scheduler_timezone,
        notification_retention_days=settings.memory_notification_retention_days,
        schema_v2_migration_enabled=settings.memory_schema_v2_migration_enabled,
        # memory-rebuild §2.6 / §5.8：证据池 nightly 批量的门控与有界认领参数
        evidence_batch_enabled=settings.memory_batch_enabled,
        summary_daily_time=settings.memory_summary_daily_time,
        batch_max_evidence=settings.memory_summary_batch_max_evidence,
        batch_max_users_per_run=settings.memory_summary_max_users_per_run,
    )


async def _run() -> None:
    from backend.memory.persistence.database import Database
    from backend.settings import get_settings

    settings = get_settings()
    configure_logging(settings)
    db = Database(settings)
    maintenance_gate = MaintenanceGate(db.engine)
    # auth 库会话工厂（方案 §4.4 / 附录 A.2 #8）：refresh family 每日清理用
    from backend.auth_service.database import AuthDatabase

    auth_db = AuthDatabase(settings)
    try:
        scheduler = Scheduler(
            session_factory=db.session_factory,
            config=_config_from_settings(),
            maintenance_gate=maintenance_gate,
            auth_session_factory=auth_db.session_factory,
        )
        scheduler.install_signal_handlers()
        await scheduler.run_forever()
    finally:
        await auth_db.close()
        await db.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
