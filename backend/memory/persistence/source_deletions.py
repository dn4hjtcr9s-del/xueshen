"""source_deletions 事实表持久层（§13.10 / §17.3）。

第一版只记录删除事实并抑制 Reader 返回；不实现跨证据全量重算（v1.2）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.contracts.common import idempotency_payload_hash
from backend.memory.contracts.evidence import SourceDeletedEvent
from backend.memory.persistence.database import exec_rowcount


def source_deletion_idempotency_hash(user_id: UUID, event: SourceDeletedEvent) -> str:
    """规范化幂等键：user_id + source_system + source_ref + source_version + event_id（§17.3）。"""
    return idempotency_payload_hash(
        {
            "user_id": str(user_id),
            "source_system": event.source_system,
            "source_ref": event.source_ref,
            "source_version": event.source_version,
            "event_id": str(event.event_id),
        }
    )


async def record_deletion(
    session: AsyncSession, *, user_id: UUID, event: SourceDeletedEvent
) -> bool:
    """插入删除事实；同一幂等键重复投递返回 False（duplicate）。"""
    rowcount = await exec_rowcount(
        session,
        text(
            """
            INSERT INTO source_deletions (
                source_deletion_id, user_id, source_system, source_ref,
                source_version, deleted_at, idempotency_hash
            ) VALUES (
                :id, :user_id, :source_system, :source_ref,
                :source_version, :deleted_at, :idempotency_hash
            )
            ON CONFLICT (idempotency_hash) DO NOTHING
            """
        ),
        {
            "id": uuid4(),
            "user_id": user_id,
            "source_system": event.source_system,
            "source_ref": event.source_ref,
            "source_version": event.source_version,
            "deleted_at": event.deleted_at,
            "idempotency_hash": source_deletion_idempotency_hash(user_id, event),
        },
    )
    return rowcount == 1


async def list_deleted_refs(
    session: AsyncSession, *, user_id: UUID, source_system: str, source_refs: list[str]
) -> list[dict[str, Any]]:
    """查询指定引用集合中的删除事实；source_version IS NULL 表示该引用全部版本已删除。"""
    if not source_refs:
        return []
    result = await session.execute(
        text(
            """
            SELECT source_ref, source_version, deleted_at
            FROM source_deletions
            WHERE user_id = :user_id AND source_system = :source_system
              AND source_ref = ANY(:refs)
            """
        ),
        {"user_id": user_id, "source_system": source_system, "refs": source_refs},
    )
    return [dict(row) for row in result.mappings().all()]


async def delete_user_deletions(session: AsyncSession, *, user_id: UUID) -> int:
    """账号删除时物理清除删除事实（§2.3）。"""
    return await exec_rowcount(
        session,
        text("DELETE FROM source_deletions WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
