"""conversation-worker 主进程（方案 §5.4 / 附录 A.9）。

职责：
1. 轮询并 claim accepted Turn（或回收过期 running/cancelling lease），
   执行/恢复 ConversationGraph（LangGraph + PostgreSQL checkpointer）；
2. 消费可靠 conversation_jobs（generate_title / summarize_thread / delete_thread）；
3. 内置 maintenance loop：retention sweep（§1.5 R1，30 天 Turn Event /
   终态 Checkpoint 清理）。

启动：uv run python -m backend.conversation.worker.main
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.persistence import turns as turns_repo
from backend.settings import Settings

logger = logging.getLogger("conversation.worker")


class GraphWorkerConfig:
    """Worker 配置（§5.4 / 附录 A.2）。"""

    def __init__(self, settings: Settings) -> None:
        self.poll_seconds = settings.conversation_worker_poll_seconds
        self.lease_seconds = settings.conversation_turn_lease_seconds
        self.max_attempts = settings.conversation_turn_max_attempts
        self.job_max_attempts = settings.conversation_job_max_attempts
        self.retention_sweep_interval_hours = settings.conversation_retention_sweep_interval_hours
        self.sse_event_retention_days = settings.conversation_sse_event_retention_days
        self.checkpoint_retention_days = settings.conversation_checkpoint_retention_days


class ConversationGraphWorker:
    """Turn poller + Graph 执行器（§5.4）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: GraphWorkerConfig,
        graph_runner: Any,
        graph_thread_id_for_turn: Callable[[UUID], str],
        worker_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._graph_runner = graph_runner
        self._graph_thread_id_for_turn = graph_thread_id_for_turn
        self.worker_id = worker_id
        self._stop = asyncio.Event()
        self._last_sweep_at: datetime | None = None
        # C4（评审）：本 worker 当前持有的 Turn lease（claim 时登记，终态释放）
        self._owned_turns: dict[UUID, int] = {}  # turn_id -> lease_generation

    def install_signal_handlers(self) -> None:
        import signal

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass

    async def run_forever(self) -> None:
        logger.info("Conversation worker 启动: %s", self.worker_id)
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        try:
            while not self._stop.is_set():
                try:
                    await self._poll_once()
                except Exception:
                    logger.exception("worker 轮询周期异常")
                await asyncio.sleep(self._config.poll_seconds)
        finally:
            self._stop.set()
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _heartbeat_loop(self) -> None:
        """C4（评审）：定期续租本 worker 持有的 Turn lease（默认 60s 租期，
        每 1/3 租期续一次），防止运行中的 Turn 被其他 Worker 误回收。"""
        interval = max(self._config.lease_seconds / 3.0, 1.0)
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            for turn_id in list(self._owned_turns.keys()):
                try:
                    ok = await turns_repo.renew_lease_via_factory(
                        self._session_factory,
                        turn_id=turn_id,
                        worker_id=self.worker_id,
                        lease_seconds=self._config.lease_seconds,
                    )
                    if not ok:
                        # 租约已丢失（被回收或已终态）：停止续租
                        self._owned_turns.pop(turn_id, None)
                except Exception:
                    logger.warning("续租失败: turn=%s", turn_id)

    async def _poll_once(self) -> None:
        """单个轮询周期：claim 一个 Turn 并执行（先单并发，简单可靠）。"""
        turn = await self._claim_next_turn()
        if turn is None:
            return
        # C4（评审）：登记 lease 供心跳续租
        self._owned_turns[turn["turn_id"]] = int(turn["lease_generation"])
        # §17.4.1：turn.started 事件（评审 C2：流式起点可达前端）
        try:
            await self._write_started_event(turn)
        except Exception:
            logger.warning("turn.started 事件写入失败: turn_id=%s", turn["turn_id"])
        try:
            await self._graph_runner.execute_turn(turn, worker_id=self.worker_id)
        except Exception:
            logger.exception("Turn 执行失败: turn_id=%s", turn["turn_id"])
            await self._mark_failed_if_attempts_exhausted(turn)
        finally:
            self._owned_turns.pop(turn["turn_id"], None)

    async def _write_started_event(self, turn: dict[str, Any]) -> None:
        """写 turn.started（status=running）。"""
        from backend.conversation.contracts.events import TurnEventWrite
        from backend.conversation.graph.state import SystemIdGenerator
        from backend.conversation.persistence.event_writer import TurnEventWriter

        writer = TurnEventWriter(id_generator=SystemIdGenerator())
        async with self._session_factory() as session:
            async with session.begin():
                await writer.append(
                    session,
                    write=TurnEventWrite(
                        turn_id=turn["turn_id"],
                        event_type="turn.started",
                        request_id=str(turn.get("request_id") or ""),
                        run_id=str(turn.get("run_id") or ""),
                        payload={"status": "running"},
                    ),
                )

    async def _claim_next_turn(self) -> dict[str, Any] | None:
        """claim 最早可执行的 Turn（accepted 到点，或过期 lease 回收）。"""
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    text(
                        """
                        SELECT * FROM conversation.conversation_turns
                        WHERE (
                            status = 'accepted' AND next_attempt_at <= :now
                        ) OR (
                            status IN ('running', 'cancelling')
                            AND lease_expires_at IS NOT NULL
                            AND lease_expires_at < :now
                        )
                        ORDER BY next_attempt_at
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """
                    ),
                    {"now": datetime.now(UTC)},
                )
                row_mapping = result.mappings().first()
                if row_mapping is None:
                    return None
                row = dict(row_mapping)
                if row["status"] == "cancelling":
                    # 回收者只完成取消清理并写终态（R2）
                    await turns_repo.write_terminal_cancelled(session, row["turn_id"])
                    return None
                generation = int(row["lease_generation"]) + 1
                new_attempt = int(row["attempt_count"]) + 1
                # 附录 A.2（第三轮评审 P2）：回收后按 attempt 退避（5s→10s，cap 60s）
                next_attempt_at = _turn_reclaim_backoff(new_attempt, datetime.now(UTC))
                await session.execute(
                    text(
                        "UPDATE conversation.conversation_turns "
                        "SET status = 'running', lease_owner = :owner, "
                        "    lease_generation = :generation, lease_expires_at = :expires, "
                        "    attempt_count = attempt_count + 1, next_attempt_at = :next, "
                        "    updated_at = :now "
                        "WHERE turn_id = :turn_id AND lease_generation = :current"
                    ),
                    {
                        "owner": self.worker_id,
                        "generation": generation,
                        "expires": datetime.now(UTC)
                        + timedelta_seconds(self._config.lease_seconds),
                        "next": next_attempt_at,
                        "turn_id": row["turn_id"],
                        "current": int(row["lease_generation"]),
                        "now": datetime.now(UTC),
                    },
                )
                row["status"] = "running"
                row["lease_owner"] = self.worker_id
                row["lease_generation"] = generation
                row["attempt_count"] = new_attempt
                row["next_attempt_at"] = next_attempt_at
                return row

    async def _mark_failed_if_attempts_exhausted(self, turn: dict[str, Any]) -> None:
        """attempt 超限 → failed（§5.4：CONVERSATION_TURN_MAX_ATTEMPTS=3）。

        修复（评审 P1-7）：写终态携带 fencing（仅当前 lease owner 可写），
        并追加 turn.failed 事件（§17.4.1：前端拿到失败原因）。
        """
        from backend.conversation.contracts.events import TurnEventWrite
        from backend.conversation.graph.state import SystemIdGenerator
        from backend.conversation.persistence.event_writer import TurnEventWriter

        if int(turn["attempt_count"]) < self._config.max_attempts:
            return
        writer = TurnEventWriter(id_generator=SystemIdGenerator())
        async with self._session_factory() as session:
            async with session.begin():
                # fencing：仅当前 worker 持有的 lease 可写 failed 终态
                from backend.memory.persistence.database import exec_rowcount

                updated = await exec_rowcount(
                    session,
                    text(
                        "UPDATE conversation.conversation_turns "
                        "SET status = 'failed', updated_at = :now "
                        "WHERE turn_id = :turn_id AND status = 'running' "
                        "  AND lease_owner = :owner"
                    ),
                    {
                        "turn_id": turn["turn_id"],
                        "now": datetime.now(UTC),
                        "owner": self.worker_id,
                    },
                )
                if updated == 1:
                    await writer.append(
                        session,
                        write=TurnEventWrite(
                            turn_id=turn["turn_id"],
                            event_type="turn.failed",
                            request_id=str(turn.get("request_id") or ""),
                            run_id=str(turn.get("run_id") or ""),
                            payload={
                                "error": {
                                    "code": "TURN_ATTEMPT_EXHAUSTED",
                                    "message": "回答尝试次数已达上限",
                                    "retryable": False,
                                    "trace_id": str(turn.get("request_id") or ""),
                                }
                            },
                        ),
                    )

    async def run_maintenance_loop(self) -> None:
        """R1 retention sweep：多副本只允许一个实例执行（数据库 advisory lock）。"""
        while not self._stop.is_set():
            now = datetime.now(UTC)
            if (
                self._last_sweep_at is None
                or (now - self._last_sweep_at).total_seconds()
                >= self._config.retention_sweep_interval_hours * 3600
            ):
                await self._sweep_retention()
                self._last_sweep_at = now
            await asyncio.sleep(60)

    async def _sweep_retention(self) -> None:
        """R1：清理超过 30 天的 Turn Event 与终态 Checkpoint（运行中不清理）。

        修复（评审 P1-6）：advisory lock 必须在**同一连接/同一事务**内获取与释放
        （pg_advisory_lock 是 session 级，跨连接获取后随连接关闭自动释放，
        多副本互斥名存实亡）。因此整个 sweep 在单个 session 内完成。
        """
        # 单连接内完成 获取锁 → 清理 → 释放锁（评审 P1-6）
        async with self._session_factory() as session:
            async with session.begin():
                acquired = await session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": 0x434F4E56535553},  # "CONVSS" 固定 namespace
                )
                if not acquired.scalar_one():
                    logger.info("retention sweep 已被其他副本持有，跳过")
                    return
                cutoff = datetime.now(UTC) - timedelta_days(self._config.sse_event_retention_days)
                # 只清理所属 Turn 已终态的事件（R1）
                await session.execute(
                    text(
                        """
                        DELETE FROM conversation.conversation_turn_events e
                        USING conversation.conversation_turns t
                        WHERE e.turn_id = t.turn_id
                          AND e.occurred_at < :cutoff
                          AND t.status IN
                              ('completed', 'failed', 'cancelled', 'deleted')
                        """
                    ),
                    {"cutoff": cutoff},
                )
                # 终态 Checkpoint 清理（R1 / 评审 P1-6：运行中 checkpoint 不清理；
                # checkpoint 表在 conversation_checkpoints schema，这里按 Turn
                # 终态时间兜底清理 graph_thread_id 前缀关联——checkpoint 行删除
                # 由 LangGraph saver 接口处理，此处只清理 DB 中已终态 Turn 的
                # 过期 checkpoint 元数据）
                await session.execute(
                    text(
                        """
                        DELETE FROM conversation_checkpoints.checkpoints c
                        USING conversation.conversation_turns t
                        WHERE c.thread_id = ('conv-turn:' || t.turn_id::text)
                          AND t.updated_at < :checkpoint_cutoff
                          AND t.status IN
                              ('completed', 'failed', 'cancelled', 'deleted')
                        """
                    ),
                    {
                        "checkpoint_cutoff": datetime.now(UTC)
                        - timedelta_days(self._config.checkpoint_retention_days)
                    },
                )
                logger.info("retention sweep 完成（events + checkpoints）")


def timedelta_seconds(seconds: int) -> Any:
    from datetime import timedelta

    return timedelta(seconds=seconds)


def timedelta_days(days: int) -> Any:
    from datetime import timedelta

    return timedelta(days=days)


def _turn_reclaim_backoff(attempt: int, now: datetime) -> datetime:
    """附录 A.2（第三轮评审 P2）：Turn claim 回收退避，cap 60s + 0~20% jitter。

    与 turns._reclaim_backoff 同公式，但带 jitter（随机源由 worker 持有）；
    graph_worker 的 claim 路径独立实现，故此处复刻公式。
    """
    import random as _random
    from datetime import timedelta as _td

    base = min(5 * (2 ** (attempt - 1)), 60)
    return now + _td(seconds=base * (1 + _random.random() * 0.2))
