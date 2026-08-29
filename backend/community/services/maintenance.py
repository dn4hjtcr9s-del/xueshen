"""Community 维护清理任务（方案 §12.4，PR-D）。

lifespan 中的低频 background task（间隔 COMMUNITY_MAINTENANCE_INTERVAL_SECONDS）：
- community_idempotency_requests：保留 7 天（COMMUNITY_IDEMPOTENCY_RETENTION_DAYS）；
- delivered / skipped_source_deleted Outbox：保留 30 天；
- dead_letter Outbox：保留 90 天（删除前要求指标/告警已留存摘要）；
- Community notifications：保留 90 天（与 Memory 通知清理周期一致）。

约束（§12.4）：pending/processing/retry_wait 事件不得按年龄自动删除；
小批量（COMMUNITY_CLEANUP_BATCH_SIZE）执行，不阻塞 Publisher 主循环。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.community import metrics
from backend.community.persistence import attachments as attachments_repo
from backend.community.persistence import idempotency as idem_repo
from backend.community.storage.base import StorageBackend
from backend.settings import Settings

logger = logging.getLogger("community.maintenance")


class CommunityMaintenance:
    """低频清理任务（§12.4）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        interval_seconds: int,
        settings: Settings,
        storage: StorageBackend | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._interval_seconds = interval_seconds
        self._settings = settings
        self._storage = storage
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        logger.info("Community 维护任务启动（间隔 %ss）", self._interval_seconds)
        while not self._stop.is_set():
            try:
                await self._run_once()
            except Exception:
                logger.exception("Community 维护周期异常")
            await asyncio.sleep(self._interval_seconds)

    async def _run_once(self) -> None:
        settings = self._settings
        batch = settings.community_cleanup_batch_size
        now = datetime.now(UTC)

        # 阶段一：旧清理保持单事务
        async with self._session_factory() as session:
            async with session.begin():
                await idem_repo.delete_expired(session, batch_size=batch)
                await self._delete_outbox_before(
                    session,
                    statuses=("delivered",),
                    cutoff=now - timedelta(days=settings.community_outbox_delivered_retention_days),
                    batch=batch,
                )
                await self._delete_outbox_before(
                    session,
                    statuses=("dead_letter",),
                    cutoff=now
                    - timedelta(days=settings.community_outbox_dead_letter_retention_days),
                    batch=batch,
                )
                await session.execute(
                    text(
                        "DELETE FROM community_notifications WHERE notification_id IN ("
                        "  SELECT notification_id FROM community_notifications "
                        "  WHERE created_at < :cutoff LIMIT :batch"
                        ")"
                    ),
                    {
                        "cutoff": now
                        - timedelta(days=settings.community_notification_retention_days),
                        "batch": batch,
                    },
                )

        # 阶段二：附件清理在阶段一提交后独立执行
        if self._storage is None:
            return
        await self._run_attachment_cleanup(batch)

    async def _run_attachment_cleanup(self, batch: int) -> None:
        settings = self._settings

        # 2.1 orphan 转换
        async with self._session_factory() as session:
            async with session.begin():
                await attachments_repo.convert_uploaded_to_orphaned(
                    session,
                    ttl_hours=settings.community_orphan_ttl_hours,
                    batch_size=batch,
                )

        # 2.2 删除流水线（逐条独立事务）
        while True:
            async with self._session_factory() as session:
                rows = await attachments_repo.scan_attachments_to_delete(session, batch_size=batch)
            if not rows:
                break
            for row in rows:
                await self._process_one_attachment_delete(row)

        # 2.3 物理删除 deleted 记录
        async with self._session_factory() as session:
            async with session.begin():
                await attachments_repo.purge_deleted_attachments(
                    session,
                    retention_days=settings.community_attachment_deleted_retention_days,
                    batch_size=batch,
                )

    async def _process_one_attachment_delete(self, row: dict[str, Any]) -> None:
        attachment_id = row["attachment_id"]
        storage_key = row["storage_key"]
        status = row["status"]

        if self._storage is None:
            return

        # 事务外调用存储删除
        result = await self._storage.delete(storage_key)

        async with self._session_factory() as session:
            async with session.begin():
                fresh = await attachments_repo.get_attachment_by_id(session, attachment_id)
                if fresh is None:
                    return
                if not result.success:
                    exhausted = await attachments_repo.record_delete_failure(
                        session, attachment_id, result.error_message or "unknown"
                    )
                    metrics.community_attachment_delete_failures_total.inc()
                    if exhausted:
                        metrics.community_attachment_delete_exhausted_total.inc()
                    return
                if status == "orphaned":
                    await attachments_repo.delete_attachment_row(session, attachment_id)
                else:
                    await attachments_repo.record_delete_success(session, attachment_id)

    async def _delete_outbox_before(
        self,
        session: AsyncSession,
        *,
        statuses: tuple[str, ...],
        cutoff: datetime,
        batch: int,
    ) -> None:
        """按状态 + 年龄分批删除 Outbox（pending/processing/retry_wait 不受影响）。"""
        await session.execute(
            text(
                "DELETE FROM community_outbox WHERE event_id IN ("
                "  SELECT event_id FROM community_outbox "
                "  WHERE status = ANY(:statuses) AND created_at < :cutoff "
                "  LIMIT :batch"
                ")"
            ),
            {"statuses": list(statuses), "cutoff": cutoff, "batch": batch},
        )
