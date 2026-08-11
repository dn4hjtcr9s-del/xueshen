"""Reader 删除抑制边界（§17.1 / §17.3 / §17.4）。

Reader 查询时必须过滤 source_deletions；source_version IS NULL 表示该引用全部版本已删除。
裁决 A（2026-08-11）：部分删除时过滤并写入 deleted_refs 正常返回；
仅当全部请求引用都已删除时抛 SOURCE_DELETED。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.errors import SourceDeletedError
from backend.memory.contracts.evidence import SourceBundle
from backend.memory.persistence.source_deletions import list_deleted_refs
from backend.memory.readers.base import ActivityReader, ConversationReader


def filter_bundle_with_deletions(
    bundle: SourceBundle, deletion_rows: list[dict[str, Any]]
) -> SourceBundle:
    """按删除事实过滤 bundle（纯函数，可单测）。

    - 删除行的 source_version IS NULL 匹配该引用全部版本；
      否则与 item.metadata["source_version"] 精确匹配。
    - 全部被删除时抛 SourceDeletedError；部分删除写入 deleted_refs。
    """
    if not deletion_rows or not bundle.items:
        return bundle
    kept = []
    newly_deleted: set[str] = set()
    for item in bundle.items:
        item_version = item.metadata.get("source_version")
        suppressed = any(
            row["source_ref"] == item.source_ref
            and (row["source_version"] is None or row["source_version"] == item_version)
            for row in deletion_rows
        )
        if suppressed:
            newly_deleted.add(item.source_ref)
        else:
            kept.append(item)
    if not kept:
        raise SourceDeletedError("请求的全部源引用已被用户删除")
    deleted_refs = sorted({*bundle.deleted_refs, *newly_deleted})
    return SourceBundle.from_items(kept, deleted_refs=deleted_refs)


async def _read_and_filter(
    inner_bundle: SourceBundle,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    source_system: str,
) -> SourceBundle:
    refs = [item.source_ref for item in inner_bundle.items]
    if not refs:
        return inner_bundle
    async with session_factory() as session:
        rows = await list_deleted_refs(
            session, user_id=user_id, source_system=source_system, source_refs=refs
        )
    return filter_bundle_with_deletions(inner_bundle, rows)


class DeletionAwareConversationReader:
    """ConversationReader 包装：查询时过滤 source_deletions（§17.1）。"""

    def __init__(
        self,
        *,
        inner: ConversationReader,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._inner = inner
        self._session_factory = session_factory

    async def read(
        self,
        *,
        user_id: UUID,
        thread_id: str,
        checkpoint_id: str | None,
        message_ids: list[str],
    ) -> SourceBundle:
        bundle = await self._inner.read(
            user_id=user_id,
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            message_ids=message_ids,
        )
        return await _read_and_filter(
            bundle, self._session_factory, user_id=user_id, source_system="conversation"
        )


class DeletionAwareActivityReader:
    """ActivityReader 包装：查询时过滤 source_deletions（§17.2）。"""

    def __init__(
        self,
        *,
        inner: ActivityReader,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._inner = inner
        self._session_factory = session_factory

    async def read(
        self,
        *,
        user_id: UUID,
        activity_type: str,
        activity_ids: list[str],
        content_ref: str | None,
    ) -> SourceBundle:
        bundle = await self._inner.read(
            user_id=user_id,
            activity_type=activity_type,
            activity_ids=activity_ids,
            content_ref=content_ref,
        )
        return await _read_and_filter(
            bundle, self._session_factory, user_id=user_id, source_system="activity"
        )
