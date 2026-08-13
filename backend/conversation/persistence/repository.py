"""ConversationRepository：Graph 访问库数据的统一边界（方案 §4.1 / §10.4）。

Graph 节点不直接 import repository 模块；通过本组合体访问（runtime 注入）。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class ConversationRepository:
    """Graph 数据访问组合体（组合 root 注入 session_factory）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.session_factory = session_factory

    async def list_messages_for_manifest(
        self, session: AsyncSession, thread_id: UUID, turn_id: UUID
    ) -> list[dict[str, object]]:
        """finalize 用：该 Turn 已提交消息（user + assistant，按 sequence 排序）。

        canonical manifest 构造输入（§7.2）。
        """
        result = await session.execute(
            text(
                "SELECT message_id, role, sequence, content_hash "
                "FROM conversation.conversation_messages "
                "WHERE thread_id = :thread_id AND turn_id = :turn_id "
                "  AND status = 'completed' "
                "ORDER BY sequence"
            ),
            {"thread_id": thread_id, "turn_id": turn_id},
        )
        return [dict(r) for r in result.mappings()]
