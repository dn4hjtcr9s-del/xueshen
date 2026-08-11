"""可注入测试 Reader / Handler（§6.1 裁决 8 / §17.3）。

供 Graph 节点测试与契约测试使用；不访问任何数据库。
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from backend.memory.contracts.evidence import (
    SourceBundle,
    SourceDeletedEvent,
    SourceItem,
)
from backend.memory.persistence.source_deletions import source_deletion_idempotency_hash


class FakeConversationReader:
    """内存对话 Reader：按 thread_id 预置消息，read 时按 message_ids 过滤。"""

    def __init__(self) -> None:
        self._messages: dict[str, dict[str, SourceItem]] = {}
        self.calls: list[dict[str, object]] = []

    def add_message(self, thread_id: str, item: SourceItem) -> None:
        self._messages.setdefault(thread_id, {})[item.source_ref] = item

    async def read(
        self,
        *,
        user_id: UUID,
        thread_id: str,
        checkpoint_id: str | None,
        message_ids: list[str],
    ) -> SourceBundle:
        self.calls.append(
            {
                "user_id": user_id,
                "thread_id": thread_id,
                "checkpoint_id": checkpoint_id,
                "message_ids": list(message_ids),
            }
        )
        available = self._messages.get(thread_id, {})
        items = [available[ref] for ref in message_ids if ref in available]
        return SourceBundle.from_items(items)


class FakeActivityReader:
    """内存行为 Reader：按 activity_type + activity_id 预置内容。"""

    def __init__(self) -> None:
        self._items: dict[str, SourceItem] = {}
        self.calls: list[dict[str, object]] = []

    def add_activity(self, item: SourceItem) -> None:
        self._items[item.source_ref] = item

    async def read(
        self,
        *,
        user_id: UUID,
        activity_type: str,
        activity_ids: list[str],
        content_ref: str | None,
    ) -> SourceBundle:
        self.calls.append(
            {
                "user_id": user_id,
                "activity_type": activity_type,
                "activity_ids": list(activity_ids),
                "content_ref": content_ref,
            }
        )
        items = [self._items[ref] for ref in activity_ids if ref in self._items]
        return SourceBundle.from_items(items)


class FakeSourceDeletionHandler:
    """内存删除 Handler：记录事实 + 幂等判定，与数据库版语义一致（§17.3）。"""

    def __init__(self) -> None:
        self.events: list[SourceDeletedEvent] = []
        self._hashes: set[str] = set()

    async def handle(
        self, *, user_id: UUID, event: SourceDeletedEvent
    ) -> Literal["recorded", "duplicate", "not_found"]:
        key = source_deletion_idempotency_hash(user_id, event)
        if key in self._hashes:
            return "duplicate"
        self._hashes.add(key)
        self.events.append(event)
        return "recorded"
