"""记录型 SourceDeletionHandler（§17.3）。

第一版只记录删除事实、阻止 Reader 再次返回该引用；
不假装已经重新计算受影响总结或图谱状态；"not_found" 保留给 v1.2 正式适配器。
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.evidence import SourceDeletedEvent
from backend.memory.persistence.source_deletions import record_deletion


class RecordingSourceDeletionHandler:
    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle(
        self, *, user_id: UUID, event: SourceDeletedEvent
    ) -> Literal["recorded", "duplicate", "not_found"]:
        async with self._session_factory() as session:
            async with session.begin():
                inserted = await record_deletion(session, user_id=user_id, event=event)
        return "recorded" if inserted else "duplicate"
