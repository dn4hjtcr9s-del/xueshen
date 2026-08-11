"""memory_documents 仓储（规格 §13.3 / §8.6 / §8.7）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount


async def get_document(
    session: AsyncSession, *, user_id: UUID, memory_id: str
) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM memory_documents WHERE user_id = :user_id AND memory_id = :memory_id"),
        {"user_id": user_id, "memory_id": memory_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def lock_documents(
    session: AsyncSession, *, user_id: UUID, memory_ids: list[str]
) -> list[dict[str, Any]]:
    """按 memory_id 字典序 SELECT ... FOR UPDATE（§13.18 锁顺序）。"""
    ordered = sorted(memory_ids)
    result = await session.execute(
        text(
            "SELECT * FROM memory_documents "
            "WHERE user_id = :user_id AND memory_id = ANY(:memory_ids) "
            "ORDER BY memory_id ASC FOR UPDATE"
        ),
        {"user_id": user_id, "memory_ids": ordered},
    )
    return [dict(r) for r in result.mappings().all()]


async def upsert_document(
    session: AsyncSession,
    *,
    user_id: UUID,
    memory_id: str,
    memory_type: str,
    topic_key: str | None,
    topic_title: str | None,
    logical_path: str,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO memory_documents (
                user_id, memory_id, memory_type, topic_key, topic_title, logical_path
            ) VALUES (
                :user_id, :memory_id, :memory_type, :topic_key, :topic_title, :logical_path
            )
            ON CONFLICT (user_id, memory_id) DO UPDATE
            SET topic_title = COALESCE(EXCLUDED.topic_title, memory_documents.topic_title),
                updated_at = now()
            """
        ),
        {
            "user_id": user_id,
            "memory_id": memory_id,
            "memory_type": memory_type,
            "topic_key": topic_key,
            "topic_title": topic_title,
            "logical_path": logical_path,
        },
    )


async def set_active_version(
    session: AsyncSession,
    *,
    user_id: UUID,
    memory_id: str,
    active_version: int,
    active_storage_key: str,
    active_checksum: str,
) -> None:
    await session.execute(
        text(
            """
            UPDATE memory_documents
            SET active_version = :active_version,
                active_storage_key = :active_storage_key,
                active_checksum = :active_checksum,
                updated_at = now()
            WHERE user_id = :user_id AND memory_id = :memory_id
            """
        ),
        {
            "user_id": user_id,
            "memory_id": memory_id,
            "active_version": active_version,
            "active_storage_key": active_storage_key,
            "active_checksum": active_checksum,
        },
    )


async def tombstone_document(
    session: AsyncSession,
    *,
    user_id: UUID,
    memory_id: str,
    deleted_version: int,
    deleted_at: datetime,
    tombstone_until: datetime,
) -> None:
    """删除：活动指针全部置 NULL，仅保留版本指针与审计元数据（§8.7）。"""
    await session.execute(
        text(
            """
            UPDATE memory_documents
            SET active_version = NULL, active_storage_key = NULL, active_checksum = NULL,
                deleted_version = :deleted_version, deleted_at = :deleted_at,
                tombstone_until = :tombstone_until, updated_at = now()
            WHERE user_id = :user_id AND memory_id = :memory_id
            """
        ),
        {
            "user_id": user_id,
            "memory_id": memory_id,
            "deleted_version": deleted_version,
            "deleted_at": deleted_at,
            "tombstone_until": tombstone_until,
        },
    )


async def restore_document(
    session: AsyncSession,
    *,
    user_id: UUID,
    memory_id: str,
    active_version: int,
    active_storage_key: str,
    active_checksum: str,
) -> None:
    """恢复：清除 tombstone，指向新的不可变版本（§8.7）。"""
    await session.execute(
        text(
            """
            UPDATE memory_documents
            SET active_version = :active_version,
                active_storage_key = :active_storage_key,
                active_checksum = :active_checksum,
                deleted_at = NULL, tombstone_until = NULL, deleted_version = NULL,
                updated_at = now()
            WHERE user_id = :user_id AND memory_id = :memory_id
            """
        ),
        {
            "user_id": user_id,
            "memory_id": memory_id,
            "active_version": active_version,
            "active_storage_key": active_storage_key,
            "active_checksum": active_checksum,
        },
    )


async def list_active_documents(session: AsyncSession, *, user_id: UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT * FROM memory_documents "
            "WHERE user_id = :user_id AND deleted_at IS NULL AND active_version IS NOT NULL "
            "ORDER BY memory_id ASC"
        ),
        {"user_id": user_id},
    )
    return [dict(r) for r in result.mappings().all()]


async def list_deleted_in_window(
    session: AsyncSession, *, user_id: UUID, now: datetime
) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT * FROM memory_documents "
            "WHERE user_id = :user_id AND deleted_at IS NOT NULL "
            "AND tombstone_until > :now ORDER BY deleted_at DESC"
        ),
        {"user_id": user_id, "now": now},
    )
    return [dict(r) for r in result.mappings().all()]


async def list_expired_tombstones(
    session: AsyncSession, *, now: datetime, batch_size: int, cursor: str | None
) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT * FROM memory_documents "
            "WHERE deleted_at IS NOT NULL AND tombstone_until <= :now "
            "AND (:cursor IS NULL OR (user_id::text || ':' || memory_id) > :cursor) "
            "ORDER BY user_id, memory_id LIMIT :batch_size"
        ),
        {"now": now, "batch_size": batch_size, "cursor": cursor},
    )
    return [dict(r) for r in result.mappings().all()]


async def mark_index_dirty(session: AsyncSession, *, user_id: UUID, dirty_at: datetime) -> None:
    """commit 同事务把 index 文档标记为 dirty（§8.6）：保留最早待重建时间。"""
    await session.execute(
        text(
            """
            INSERT INTO memory_documents (
                user_id, memory_id, memory_type, logical_path, index_dirty_at
            ) VALUES (
                :user_id, 'index', 'index', 'index.md', :dirty_at
            )
            ON CONFLICT (user_id, memory_id) DO UPDATE
            SET index_dirty_at = LEAST(
                memory_documents.index_dirty_at, EXCLUDED.index_dirty_at
            ),
            updated_at = now()
            """
        ),
        {"user_id": user_id, "dirty_at": dirty_at},
    )


async def clear_index_dirty(
    session: AsyncSession, *, user_id: UUID, expected_dirty_at: datetime
) -> bool:
    """重建成功后仅在没有更新 commit 发生的前提下清除 dirty（§8.6）。"""
    rowcount = await exec_rowcount(
        session,
        text(
            """
            UPDATE memory_documents
            SET index_dirty_at = NULL, updated_at = now()
            WHERE user_id = :user_id AND memory_type = 'index'
              AND index_dirty_at = :expected_dirty_at
            """
        ),
        {"user_id": user_id, "expected_dirty_at": expected_dirty_at},
    )
    return rowcount == 1


async def list_dirty_indexes(session: AsyncSession, *, batch_size: int) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT * FROM memory_documents "
            "WHERE memory_type = 'index' AND index_dirty_at IS NOT NULL "
            "ORDER BY index_dirty_at ASC LIMIT :batch_size"
        ),
        {"batch_size": batch_size},
    )
    return [dict(r) for r in result.mappings().all()]


async def get_max_version(session: AsyncSession, *, user_id: UUID, memory_id: str) -> int:
    """文档历史最大版本（恢复时新版本号 = 最大版本 + 1，§8.7）。"""
    result = await session.execute(
        text(
            "SELECT COALESCE(MAX(GREATEST(before_version, after_version)), 0) "
            "FROM memory_commits WHERE user_id = :user_id AND memory_id = :memory_id"
        ),
        {"user_id": user_id, "memory_id": memory_id},
    )
    return int(result.scalar() or 0)
