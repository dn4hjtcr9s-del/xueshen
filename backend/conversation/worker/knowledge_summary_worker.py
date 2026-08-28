"""知识总结 Generation Worker（知识总结方案 §13、§14）。

它使用独立的 generation_jobs 队列，不混入标题、会话摘要和删除任务。模型调用、
租约和内容提交由 KnowledgeSummaryGenerationService 完成；本模块只负责轮询、并发
上限和生命周期信号。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation import metrics
from backend.conversation.gateways.knowledge_summary_openai import KnowledgeSummaryGateway
from backend.conversation.persistence import knowledge_summaries as summaries_repo
from backend.conversation.persistence import knowledge_summary_generations as generations_repo
from backend.conversation.persistence import knowledge_summary_retention as retention_repo
from backend.conversation.services.knowledge_summary_enqueue import (
    KnowledgeSummaryEnqueueRepairService,
)
from backend.conversation.services.knowledge_summary_generation import (
    KnowledgeSummaryGenerationService,
)
from backend.conversation.services.token_counter import TokenCounter

logger = logging.getLogger("conversation.worker.knowledge_summary")


class KnowledgeSummaryWorker:
    """独立知识总结 Job 消费器。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: Any,
        gateway: KnowledgeSummaryGateway,
        token_counter: TokenCounter,
        worker_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._gateway = gateway
        self._token_counter = token_counter
        self.worker_id = worker_id or f"knowledge-summary-worker-{uuid4()}"
        self._stop = asyncio.Event()
        self._semaphore = asyncio.Semaphore(
            int(getattr(config, "conversation_knowledge_summary_worker_concurrency", 4))
        )

    def install_signal_handlers(self) -> None:
        """安装 SIGINT/SIGTERM，保持与现有 Conversation Worker 一致。"""
        import signal

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass

    async def run_forever(self) -> None:
        """持续轮询 Generation Job；关闭 generation 时不创建新任务。"""
        logger.info("KnowledgeSummaryWorker 启动: %s", self.worker_id)
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except Exception:
                logger.exception("知识总结 Worker 轮询异常")
            await asyncio.sleep(
                float(getattr(self._config, "conversation_knowledge_summary_poll_seconds", 1.0))
            )

    async def _poll_once(self) -> None:
        if not getattr(self._config, "conversation_knowledge_summary_generation_enabled", False):
            return
        # 1. 修复 finalize 中入队失败的 Turn（§14.1）：在 claim Job 之前处理。
        await self._repair_enqueue_failed_turns()
        async with self._session_factory() as session:
            async with session.begin():
                queue_depths = await retention_repo.get_queue_depths(session)
                for status in ("pending", "retry_wait"):
                    for trigger in (
                        "auto",
                        "manual",
                        "manual_retry",
                        "manual_refresh",
                        "ops_retry",
                    ):
                        metrics.knowledge_summary_queue_depth.labels(
                            status=status, trigger=trigger
                        ).set(queue_depths.get((status, trigger), 0))
                suspension = await retention_repo.evaluate_auto_suspension(
                    session,
                    queue_limit=int(
                        getattr(
                            self._config,
                            "conversation_knowledge_summary_auto_queue_depth_limit",
                            5000,
                        )
                    ),
                    oldest_limit_seconds=int(
                        getattr(
                            self._config,
                            "conversation_knowledge_summary_auto_oldest_job_seconds",
                            600,
                        )
                    ),
                    failure_rate=float(
                        getattr(
                            self._config,
                            "conversation_knowledge_summary_auto_failure_rate",
                            0.50,
                        )
                    ),
                    minimum_calls=int(
                        getattr(
                            self._config,
                            "conversation_knowledge_summary_auto_failure_min_calls",
                            20,
                        )
                    ),
                    daily_token_budget=getattr(
                        self._config, "conversation_knowledge_summary_daily_token_budget", None
                    ),
                )
                if suspension is not None:
                    for reason in suspension["reasons"]:
                        metrics.knowledge_summary_auto_suspensions_total.labels(reason=reason).inc()
                    logger.error(
                        "知识总结自动生成已熔断: reasons=%s queue_depth=%s",
                        suspension["reasons"],
                        suspension["queue_depth"],
                    )
                runtime_control = await summaries_repo.get_runtime_control(session)
                rows = await generations_repo.claim_generation_jobs(
                    session,
                    worker_id=self.worker_id,
                    lease_seconds=int(
                        getattr(self._config, "conversation_knowledge_summary_lease_seconds", 60)
                    ),
                    max_concurrency=int(
                        getattr(
                            self._config, "conversation_knowledge_summary_worker_concurrency", 4
                        )
                    ),
                    manual_reserved_slots=int(
                        getattr(
                            self._config,
                            "conversation_knowledge_summary_manual_reserved_slots",
                            1,
                        )
                    ),
                    auto_generation_suspended=bool(
                        runtime_control and runtime_control["auto_generation_suspended"]
                    ),
                )
        if rows:
            await asyncio.gather(*(self._run_row(row) for row in rows))

    async def _repair_enqueue_failed_turns(self) -> None:
        """每轮扫描并修复已到时间的 enqueue_failed Turn（§14.1）。"""
        if hasattr(self._config, "knowledge_summary_flags"):
            flags = self._config.knowledge_summary_flags
        else:
            flags = {
                "enabled": getattr(self._config, "conversation_knowledge_summary_enabled", False),
                "generation": getattr(
                    self._config,
                    "conversation_knowledge_summary_generation_enabled",
                    False,
                ),
                "auto_generate": getattr(
                    self._config,
                    "conversation_knowledge_summary_auto_generate_enabled",
                    False,
                ),
            }
        if not (flags["enabled"] and flags["generation"] and flags["auto_generate"]):
            return
        repair_service = KnowledgeSummaryEnqueueRepairService(settings=self._config)
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                turns = await summaries_repo.claim_enqueue_failed_turns(
                    session, now=now, batch_size=50
                )
        for turn_row in turns:
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        # 重新读取并锁定该 Turn，避免与其他 Worker 并发。
                        current = await session.execute(
                            text(
                                "SELECT * FROM conversation.conversation_turns "
                                "WHERE turn_id = :turn_id "
                                "  AND knowledge_summary_enqueue_status = 'enqueue_failed' "
                                "  AND knowledge_summary_enqueue_next_attempt_at <= :now "
                                "FOR UPDATE"
                            ),
                            {"turn_id": turn_row["turn_id"], "now": now},
                        )
                        current_row = current.mappings().first()
                        if current_row is None:
                            continue
                        await repair_service.repair_turn(session, turn_row=dict(current_row))
            except Exception:
                logger.exception("修复知识总结 enqueue 失败: turn=%s", turn_row.get("turn_id"))

    async def _run_row(self, row: dict[str, Any]) -> None:
        async with self._semaphore:
            service = KnowledgeSummaryGenerationService(
                session_factory=self._session_factory,
                config=self._config,
                gateway=self._gateway,
                token_counter=self._token_counter,
                worker_id=self.worker_id,
            )
            await service.execute(row)
