"""知识总结维护 Worker（方案 §20.5、§21.5）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.services.knowledge_summary_retention import run_retention_once

logger = logging.getLogger("conversation.worker.knowledge_summary_maintenance")


class KnowledgeSummaryMaintenanceWorker:
    """每小时执行 retention；自动生成熔断由 Generation Worker 轮询检查。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Any,
        interval_seconds: int = 3600,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._interval_seconds = interval_seconds
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """请求维护循环在当前批次结束后停止。"""
        self._stop.set()

    async def run_once(self) -> dict[str, int]:
        """执行一次 retention；即使所有知识总结开关关闭也必须运行。"""
        return await run_retention_once(self._session_factory, self._settings)

    async def run_forever(self) -> None:
        """持续执行小时级维护；维护失败只告警并等待下一轮。"""
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("知识总结 retention 维护失败")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue
