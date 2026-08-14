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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.community.persistence import idempotency as idem_repo
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
    ) -> None:
        self._session_factory = session_factory
        self._interval_seconds = interval_seconds
        self._settings = settings
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
        async with self._session_factory() as session:
            async with session.begin():
                # §12.4：幂等请求保留 7 天
                await idem_repo.delete_expired(session, batch_size=batch)
                # delivered/skipped Outbox 保留 30 天
                await self._delete_outbox_before(
                    session,
                    statuses=("delivered",),
                    cutoff=now - timedelta(days=settings.community_outbox_delivered_retention_days),
                    batch=batch,
                )
                # dead_letter Outbox 保留 90 天
                await self._delete_outbox_before(
                    session,
                    statuses=("dead_letter",),
                    cutoff=now
                    - timedelta(days=settings.community_outbox_dead_letter_retention_days),
                    batch=batch,
                )
                # 通知保留 90 天（与 Memory 一致）
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
