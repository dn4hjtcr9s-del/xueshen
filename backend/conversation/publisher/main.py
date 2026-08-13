"""conversation-outbox-publisher 入口（附录 A.9：uv run python -m
backend.conversation.publisher.main）。

独立进程边界；只投递 Outbox，不参与 Thread 生命周期（§1.5 R3）。
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from backend.conversation.graph.state import SystemIdGenerator
from backend.conversation.persistence.database import ConversationDatabase
from backend.conversation.persistence.event_writer import TurnEventWriter
from backend.conversation.publisher.outbox_publisher import (
    ConversationOutboxPublisher,
    OutboxPublisherConfig,
)
from backend.memory.client import MemoryClient
from backend.settings import get_settings


async def _run() -> None:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("conversation.publisher")
    db = ConversationDatabase(settings)
    try:
        memory_client = MemoryClient(
            settings.memory_api_base_url,
            token=settings.memory_agent_token,
            timeout=30.0,
        )
        # §8.2 / 评审 C7：source deletion 使用独立 system principal
        source_delete_client = None
        if settings.conversation_source_delete_service_token:
            source_delete_client = MemoryClient(
                settings.memory_api_base_url,
                token=settings.conversation_source_delete_service_token,
                timeout=30.0,
            )
        publisher = ConversationOutboxPublisher(
            session_factory=db.session_factory,
            config=OutboxPublisherConfig(settings),
            memory_client=memory_client,
            source_delete_client=source_delete_client,
            turn_event_writer=TurnEventWriter(id_generator=SystemIdGenerator()),
            worker_id=f"publisher-{uuid4()}",
        )
        publisher.install_signal_handlers()
        logger.info("Outbox publisher 启动")
        await publisher.run_forever()
    finally:
        await db.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
