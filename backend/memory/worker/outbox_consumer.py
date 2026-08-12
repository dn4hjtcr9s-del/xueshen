"""Outbox Consumer 进程（§14.4 / §13.12）。

- 轮询 1 秒、每批 100 条、Lease 60 秒、最多 10 次重试、指数退避上限 30 分钟。
- 以 (event_type, target) 路由；目标侧唯一幂等键保证至少一次投递下不重复产生业务效果。
- 单 target 失败只把该 delivery 置 retry_wait/dead_letter，不影响其他 target；
  所有启用 target 成功后主行才 published；任一 target dead_letter 则主行
  dead_letter 并输出告警日志。
- summary_projection 幂等键：summary-projection:{memory_id}:{source_version}:{node_id}。

启动：python -m backend.memory.worker.outbox_consumer
"""

from __future__ import annotations

import asyncio
import logging
import random
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import ProjectSummaryToGraphCommand
from backend.memory.contracts.common import (
    OPERATION_ROUTING,
    idempotency_payload_hash,
    new_trace_id,
)
from backend.memory.contracts.errors import InvalidPayloadError
from backend.memory.contracts.evidence import GraphProjectionEvidence
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.logging_config import configure_logging
from backend.memory.persistence import notifications as notifications_repo
from backend.memory.persistence import operations as ops_repo
from backend.memory.persistence import outbox as outbox_repo
from backend.memory.worker.checkpoint import thread_id_for_operation
from backend.memory.worker.retry import outbox_backoff_seconds

_PROJECTION_EVENTS = {"memory.changed", "memory.deleted", "memory.restored"}


class _DeliveryFencedError(Exception):
    """delivery 写回被 fencing 拒绝（Lease 已易主）；用于回滚同事务的投递副作用。"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class OutboxConsumerConfig:
    """§14.4 第一版默认配置。"""

    poll_interval_seconds: float = 1.0
    batch_size: int = 100
    lease_seconds: int = 60
    max_attempts: int = 10


async def load_projection_link(
    session: AsyncSession, *, user_id: UUID, memory_id: str, node_id: str, source_version: int
) -> dict[str, Any] | None:
    """apply_active_version 的映射信息：commit 同事务写入的活动 link（§13.8.1）。"""
    result = await session.execute(
        text(
            """
            SELECT mapping_method, mapping_confidence FROM memory_graph_links
            WHERE user_id = :user_id AND memory_id = :memory_id AND node_id = :node_id
              AND memory_version = :source_version AND active = true
            """
        ),
        {
            "user_id": user_id,
            "memory_id": memory_id,
            "node_id": node_id,
            "source_version": source_version,
        },
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def load_commit_evidence(
    session: AsyncSession, *, user_id: UUID, memory_id: str, source_version: int
) -> list[GraphProjectionEvidence]:
    """apply_active_version 的证据：该版本 commit 的 evidence_refs（§10.6）。"""
    result = await session.execute(
        text(
            """
            SELECT evidence_refs, created_at FROM memory_commits
            WHERE user_id = :user_id AND memory_id = :memory_id
              AND after_version = :source_version
            ORDER BY created_at DESC LIMIT 1
            """
        ),
        {"user_id": user_id, "memory_id": memory_id, "source_version": source_version},
    )
    row = result.mappings().first()
    if row is None:
        return []
    return [
        GraphProjectionEvidence(
            evidence_ref=str(ref),
            direction="learning",
            strength=0.6,
            occurred_at=row["created_at"],
        )
        for ref in (row["evidence_refs"] or [])
    ]


def projection_routing(event_type: str, payload: dict[str, Any]) -> tuple[str, int] | None:
    """§14.4 路由规则：事件 → (projection_action, aggregate_version)。

    memory.changed/memory.restored → apply_active_version（after_version）；
    memory.deleted → recompute_without_deleted_version（deleted_version，
    绝不把删除版本当活动版本）。非 projection 事件返回 None。
    """
    if event_type in ("memory.changed", "memory.restored"):
        return "apply_active_version", int(payload["after_version"])
    if event_type == "memory.deleted":
        return "recompute_without_deleted_version", int(payload["deleted_version"])
    return None


def projection_idempotency_key(memory_id: str, source_version: int, node_id: str) -> str:
    """§14.4：summary-projection:{memory_id}:{source_version}:{node_id}。"""
    return f"summary-projection:{memory_id}:{source_version}:{node_id}"


def notification_text(event_type: str, payload: dict[str, Any]) -> tuple[str, str]:
    """用户通知文案（§13.13）；第一版为确定性模板。"""
    if event_type == "memory.deleted":
        return (
            "记忆已删除",
            f"记忆 {payload.get('memory_id', '')[:100]} 已删除，"
            f"{str(payload.get('restore_until', ''))[:32]} 前可恢复。",
        )
    if event_type == "memory.restored":
        return ("记忆已恢复", f"记忆 {payload.get('memory_id', '')[:100]} 已恢复。")
    if event_type == "review_candidate.created":
        return ("有新的记忆候选", "有一条新的记忆候选等待审核。")
    if event_type == "review_candidate.resolved":
        return ("记忆候选已处理", f"候选审核结果：{payload.get('decision', '')[:50]}。")
    if event_type == "graph_state.changed":
        return ("知识图谱状态已更新", f"节点 {payload.get('node_id', '')[:50]} 的掌握状态已更新。")
    if event_type == "graph_state.explanation_available":
        return ("知识图谱状态说明", str(payload.get("summary", ""))[:500])
    return ("记忆通知", event_type[:100])


@dataclass
class OutboxConsumer:
    session_factory: async_sessionmaker[AsyncSession]
    config: OutboxConsumerConfig = field(default_factory=OutboxConsumerConfig)
    consumer_id: str = field(default_factory=lambda: f"outbox-consumer-{uuid4().hex[:8]}")
    clock: Callable[[], datetime] = _utc_now
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("memory.outbox_consumer")
    )

    def __post_init__(self) -> None:
        self._stopping = asyncio.Event()
        self._rng = random.Random()

    def request_stop(self) -> None:
        self._stopping.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_stop)

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            await self.tick()
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def tick(self) -> int:
        """单次轮询 tick：领取一批并逐条处理，返回领取条数。

        评审 #8：串行处理期间用批量心跳维持整批 Lease；所有写回按
        owner/generation fencing CAS，Lease 易主后立即停止本行处理。
        """
        async with self.session_factory() as session:
            async with session.begin():
                rows = await outbox_repo.claim_batch(
                    session,
                    worker_id=self.consumer_id,
                    lease_seconds=self.config.lease_seconds,
                    batch_size=self.config.batch_size,
                )
        if not rows:
            return 0
        heartbeat_task = asyncio.create_task(
            self._batch_heartbeat_loop([row["outbox_id"] for row in rows])
        )
        try:
            for row in rows:
                if self._stopping.is_set():
                    break
                await self._process_guarded(row)
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        return len(rows)

    async def _batch_heartbeat_loop(self, outbox_ids: list[UUID]) -> None:
        interval = max(self.config.lease_seconds / 3, 1.0)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.session_factory() as session:
                    async with session.begin():
                        renewed = await outbox_repo.heartbeat_batch(
                            session,
                            worker_id=self.consumer_id,
                            outbox_ids=outbox_ids,
                            lease_seconds=self.config.lease_seconds,
                        )
                if renewed < len(outbox_ids):
                    self.logger.info(
                        "批量心跳：%s/%s 行仍持有 Lease（其余已易主或终结）",
                        renewed,
                        len(outbox_ids),
                    )
            except Exception:
                self.logger.exception("outbox 批量心跳失败")

    async def _process_guarded(self, row: dict[str, Any]) -> None:
        try:
            await self._process(row)
        except Exception:
            self.logger.exception("outbox 处理出现未捕获异常: %s", row["outbox_id"])
            backoff = outbox_backoff_seconds(int(row.get("attempt_count", 1)), rng=self._rng)
            async with self.session_factory() as session:
                async with session.begin():
                    await outbox_repo.reschedule_outbox(
                        session,
                        outbox_id=row["outbox_id"],
                        next_run_at=self.clock() + timedelta(seconds=backoff),
                        expected_worker=self.consumer_id,
                        expected_generation=int(row.get("lease_generation") or 0),
                    )

    async def _process(self, row: dict[str, Any]) -> None:
        outbox_id = row["outbox_id"]
        generation = int(row.get("lease_generation") or 0)
        status: str | None = None
        async with self.session_factory() as session:
            deliveries = await outbox_repo.list_deliveries(session, outbox_id=outbox_id)
        for delivery in deliveries:
            if delivery["status"] == "succeeded":
                continue
            if not await self._deliver(row, delivery, generation=generation):
                self.logger.info("outbox %s Lease 已易主，停止本行处理", outbox_id)
                return
        async with self.session_factory() as session:
            async with session.begin():
                # 全部成功 → published；任一 dead_letter → dead_letter（§13.12）。
                # finalize 同时释放 Lease 并写入退避时间，之后不得再做持锁写回
                backoff = outbox_backoff_seconds(int(row.get("attempt_count", 1)), rng=self._rng)
                finalized = await outbox_repo.finalize_outbox(
                    session,
                    outbox_id=outbox_id,
                    expected_worker=self.consumer_id,
                    expected_generation=generation,
                    retry_next_run_at=self.clock() + timedelta(seconds=backoff),
                )
                if not finalized:
                    self.logger.info(
                        "outbox %s finalize 被 fencing 拒绝（Lease 已易主）", outbox_id
                    )
                    return
                status = await outbox_repo.get_status(session, outbox_id=outbox_id)
        if status == "dead_letter":
            self.logger.error(
                "告警：Outbox %s（%s/%s）存在 dead_letter target，主行已置 dead_letter",
                outbox_id,
                row["event_type"],
                row["aggregate_id"],
            )

    async def _deliver(
        self, row: dict[str, Any], delivery: dict[str, Any], *, generation: int
    ) -> bool:
        """投递单个 target；返回 False 表示 fencing 拒绝（Lease 已易主）。"""
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    await self._dispatch(session, row, delivery)
                    written = await outbox_repo.mark_delivery(
                        session,
                        delivery_id=delivery["delivery_id"],
                        status="succeeded",
                        expected_worker=self.consumer_id,
                        expected_generation=generation,
                    )
                    if not written:
                        # 投递副作用与标记在同一事务：fencing 失败整体回滚，
                        # 不产生未记账的投递
                        raise _DeliveryFencedError()
        except _DeliveryFencedError:
            return False
        except Exception as exc:
            attempt = int(delivery["attempt_count"]) + 1
            max_attempts = int(row.get("max_attempts") or self.config.max_attempts)
            status = "dead_letter" if attempt >= max_attempts else "retry_wait"
            self.logger.warning(
                "delivery %s（%s → %s）失败，置 %s：%s",
                delivery["delivery_id"],
                row["event_type"],
                delivery["target"],
                status,
                exc,
            )
            async with self.session_factory() as session:
                async with session.begin():
                    return await outbox_repo.mark_delivery(
                        session,
                        delivery_id=delivery["delivery_id"],
                        status=status,
                        last_error={"code": type(exc).__name__, "message": str(exc)[:500]},
                        expected_worker=self.consumer_id,
                        expected_generation=generation,
                    )
        return True

    async def _dispatch(
        self, session: AsyncSession, row: dict[str, Any], delivery: dict[str, Any]
    ) -> None:
        target = delivery["target"]
        if target == "summary_projection":
            await self._deliver_summary_projection(session, row)
        elif target == "user_notification":
            title, body = notification_text(row["event_type"], row["payload"])
            await notifications_repo.insert_notification(
                session,
                user_id=row["user_id"],
                event_type=row["event_type"],
                title=title,
                body=body,
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                source_outbox_id=row["outbox_id"],
            )
        elif target == "internal_event_log":
            await outbox_repo.insert_internal_event_log(
                session,
                event_log_id=uuid4(),
                outbox_id=row["outbox_id"],
                event_type=row["event_type"],
                idempotency_key=delivery["idempotency_key"],
                user_id=row["user_id"],
                payload=row["payload"],
            )
        else:
            raise InvalidPayloadError(f"未知 Outbox target: {target}")

    async def _deliver_summary_projection(self, session: AsyncSession, row: dict[str, Any]) -> None:
        event_type = row["event_type"]
        payload = row["payload"]
        routing = projection_routing(event_type, payload)
        if routing is None:
            # learner 事件通常没有图谱候选（§14.4）：幂等成功，不强制关联
            return
        action, source_version = routing
        memory_id = str(payload["memory_id"])
        candidates = list(payload.get("graph_projection_candidates") or [])
        if not candidates:
            # 空候选：直接幂等成功，不创建空 operation（§14.4）
            return
        for node_id in candidates:
            command = await self._build_projection_command(
                session,
                row,
                event_type=event_type,
                action=action,
                memory_id=memory_id,
                source_version=source_version,
                node_id=str(node_id),
            )
            if command is None:
                continue
            await self._insert_projection_operation(session, row=row, command=command)

    async def _build_projection_command(
        self,
        session: AsyncSession,
        row: dict[str, Any],
        *,
        event_type: str,
        action: str,
        memory_id: str,
        source_version: int,
        node_id: str,
    ) -> ProjectSummaryToGraphCommand | None:
        if action == "recompute_without_deleted_version":
            return ProjectSummaryToGraphCommand(
                trigger_event_type=event_type,  # type: ignore[arg-type]
                projection_action=action,  # type: ignore[arg-type]
                source_memory_id=memory_id,
                source_version=source_version,
                node_id=node_id,
            )
        link = await load_projection_link(
            session,
            user_id=row["user_id"],
            memory_id=memory_id,
            node_id=node_id,
            source_version=source_version,
        )
        if link is None:
            # 无可靠节点映射：projection 必然 no_change（§10.6），跳过不建 operation
            self.logger.warning(
                "无可靠节点映射，跳过 projection：%s v%s → %s", memory_id, source_version, node_id
            )
            return None
        evidence = await load_commit_evidence(
            session, user_id=row["user_id"], memory_id=memory_id, source_version=source_version
        )
        if not evidence:
            # 无有效证据：projection 必然 no_change（§10.6），跳过不建 operation
            self.logger.warning(
                "无 commit 证据，跳过 projection：%s v%s → %s", memory_id, source_version, node_id
            )
            return None
        return ProjectSummaryToGraphCommand(
            trigger_event_type=event_type,  # type: ignore[arg-type]
            projection_action=action,  # type: ignore[arg-type]
            source_memory_id=memory_id,
            source_version=source_version,
            node_id=node_id,
            mapping_method=link["mapping_method"],
            mapping_confidence=float(link["mapping_confidence"]),
            evidence=evidence,
        )

    async def _insert_projection_operation(
        self, session: AsyncSession, *, row: dict[str, Any], command: ProjectSummaryToGraphCommand
    ) -> None:
        operation_id = uuid4()
        input_kind, priority = OPERATION_ROUTING["project_summary_to_graph"]
        operation = MemoryOperation(
            operation_id=operation_id,
            idempotency_key=projection_idempotency_key(
                command.source_memory_id, command.source_version, command.node_id
            ),
            user_id=row["user_id"],
            actor_type="summary_projection",
            input_kind=input_kind,
            operation_type="project_summary_to_graph",
            priority=priority,
            occurred_at=self.clock(),
            payload=command,
            trace_id=new_trace_id(),
            graph_thread_id=thread_id_for_operation(operation_id),
        )
        created = await ops_repo.insert_operation(
            session,
            operation,
            idempotency_payload_hash=idempotency_payload_hash(command.model_dump(mode="json")),
        )
        if not created:
            # 至少一次投递下的重复：operation 已存在，幂等成功
            self.logger.debug("projection operation 已存在：%s", operation.idempotency_key)


def _config_from_settings() -> OutboxConsumerConfig:
    from backend.settings import get_settings

    settings = get_settings()
    return OutboxConsumerConfig(
        poll_interval_seconds=settings.memory_outbox_poll_seconds,
        batch_size=settings.memory_outbox_batch_size,
        lease_seconds=settings.memory_outbox_lease_seconds,
        max_attempts=settings.memory_outbox_max_attempts,
    )


async def _run() -> None:
    from backend.memory.persistence.database import Database
    from backend.settings import get_settings

    settings = get_settings()
    configure_logging(settings)
    db = Database(settings)
    try:
        consumer = OutboxConsumer(
            session_factory=db.session_factory, config=_config_from_settings()
        )
        consumer.install_signal_handlers()
        await consumer.run_forever()
    finally:
        await db.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
