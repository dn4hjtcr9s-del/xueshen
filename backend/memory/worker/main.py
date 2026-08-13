"""Worker 进程入口（§14.6：uv run python -m backend.memory.worker.main）。

组装 settings、session factory、LocalLangGraphRunner（真实 graph + PostgreSQL
checkpointer），install_signal_handlers + run_forever。执行语义见 worker.py
（§14.1 / §11.5）。
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from backend.memory.contracts.errors import SourceNotFoundError
from backend.memory.contracts.evidence import SourceBundle
from backend.memory.graph.openai_client import RealMemoryLLMClient
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.graph.state import (
    MemoryRuntimeContext,
    SystemClock,
    SystemIdGenerator,
    default_registry_factory,
)
from backend.memory.logging_config import configure_logging
from backend.memory.maintenance_gate import MaintenanceGate
from backend.memory.persistence.database import Database
from backend.memory.services.graph_state_service import KnowledgeGraphStateService
from backend.memory.services.memory_service import MemoryService
from backend.memory.storage.local_markdown import LocalMarkdownStore
from backend.memory.worker.checkpoint import CheckpointCleanupAdapter
from backend.memory.worker.worker import Worker, WorkerConfig
from backend.settings import Settings, get_settings


class _UnavailableConversationReader:
    """第一版无上游对话系统适配器（§25：本期只定义 Reader 接口）。"""

    async def read(
        self,
        *,
        user_id: UUID,
        thread_id: str,
        checkpoint_id: str | None,
        message_ids: list[str],
    ) -> SourceBundle:
        raise SourceNotFoundError("对话 Reader 正式适配器本期未接入（§17.1）")


class _UnavailableActivityReader:
    """第一版无上游行为系统适配器（§25：本期只定义 Reader 接口）。"""

    async def read(
        self,
        *,
        user_id: UUID,
        activity_type: str,
        activity_ids: list[str],
        content_ref: str | None,
    ) -> SourceBundle:
        raise SourceNotFoundError("行为 Reader 正式适配器本期未接入（§17.2）")


def _worker_config(settings: Settings) -> WorkerConfig:
    return WorkerConfig(
        concurrency=settings.memory_worker_concurrency,
        batch_size=settings.memory_worker_batch_size,
        poll_interval_seconds=settings.memory_worker_poll_seconds,
        lease_seconds=settings.memory_operation_lease_seconds,
        heartbeat_interval_seconds=float(settings.memory_heartbeat_interval_seconds),
        soft_timeout_seconds=float(settings.memory_operation_soft_timeout_seconds),
        hard_timeout_seconds=float(settings.memory_operation_hard_timeout_seconds),
        shutdown_wait_seconds=float(settings.memory_worker_graceful_shutdown_seconds),
    )


def _psycopg_conninfo(settings: Settings) -> str:
    """SQLAlchemy URL → psycopg conninfo（checkpointer 不走 SQLAlchemy）。"""
    return settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


async def _run() -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    settings = get_settings()
    configure_logging(settings)
    logger = logging.getLogger("memory.worker")
    db = Database(settings)
    maintenance_gate = MaintenanceGate(db.engine)
    try:
        store = LocalMarkdownStore(settings.memory_storage_root)
        memory_service = MemoryService(
            settings=settings, session_factory=db.session_factory, store=store
        )
        async with AsyncPostgresSaver.from_conn_string(_psycopg_conninfo(settings)) as saver:
            async with maintenance_gate.traffic():
                await saver.setup()
            context = MemoryRuntimeContext(
                settings=settings,
                memory_service=memory_service,
                graph_state_service=KnowledgeGraphStateService(
                    settings=settings, session_factory=db.session_factory
                ),
                conversation_reader=_UnavailableConversationReader(),
                activity_reader=_UnavailableActivityReader(),
                graph_registry_factory=default_registry_factory,
                openai_client=RealMemoryLLMClient(settings=settings),
                session_factory=db.session_factory,
                clock=SystemClock(),
                id_generator=SystemIdGenerator(),
                logger=logger,
                checkpoint_cleanup=CheckpointCleanupAdapter(saver=saver),
            )
            runner = LocalLangGraphRunner(context=context, checkpointer=saver)
            worker = Worker(
                session_factory=db.session_factory,
                runner=runner,
                config=_worker_config(settings),
                logger=logger,
                maintenance_gate=maintenance_gate,
            )
            worker.install_signal_handlers()
            logger.info("Worker 启动：concurrency=%d", worker.config.concurrency)
            await worker.run_forever()
    finally:
        await db.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
