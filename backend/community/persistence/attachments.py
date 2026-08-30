"""附件持久化（community-rebuild-plan.md §7.3/§7.12）。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from backend.community.contracts.errors import AttachmentConflictError, AttachmentForbiddenError


async def insert_attachment(
    session: AsyncSession,
    *,
    attachment_id: UUID,
    uploader_id: UUID,
    storage_key: str,
    original_filename: str,
    mime: str,
    size_bytes: int,
    width: int,
    height: int,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO community_attachments (
                attachment_id, uploader_id, storage_key, original_filename,
                mime, size_bytes, width, height, status, created_at, updated_at
            ) VALUES (
                :attachment_id, :uploader_id, :storage_key, :original_filename,
                :mime, :size_bytes, :width, :height, 'uploaded', now(), now()
            )
            """
        ),
        {
            "attachment_id": attachment_id,
            "uploader_id": uploader_id,
            "storage_key": storage_key,
            "original_filename": original_filename,
            "mime": mime,
            "size_bytes": size_bytes,
            "width": width,
            "height": height,
        },
    )


async def get_attachment_by_id(
    session: AsyncSession,
    attachment_id: UUID,
) -> dict[str, Any] | None:
    row = await session.execute(
        text("SELECT * FROM community_attachments WHERE attachment_id = :id"),
        {"id": attachment_id},
    )
    result = row.mappings().fetchone()
    return dict(result) if result is not None else None


async def get_attachment_by_storage_key(
    session: AsyncSession,
    storage_key: str,
) -> dict[str, Any] | None:
    """local-uploads 路由：按 storage_key 查附件记录（§7.12 查找规则）。"""
    row = await session.execute(
        text("SELECT * FROM community_attachments WHERE storage_key = :key"),
        {"key": storage_key},
    )
    result = row.mappings().fetchone()
    return dict(result) if result is not None else None


async def get_attachments_by_post_id(
    session: AsyncSession,
    post_id: UUID,
) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            """
            SELECT * FROM community_attachments
            WHERE post_id = :post_id AND status = 'attached'
            ORDER BY position ASC
            """
        ),
        {"post_id": post_id},
    )
    return [dict(r) for r in rows.mappings().fetchall()]


async def get_attachments_for_posts(
    session: AsyncSession,
    post_ids: list[UUID],
) -> list[dict[str, Any]]:
    if not post_ids:
        return []
    rows = await session.execute(
        text(
            """
            SELECT * FROM community_attachments
            WHERE post_id = ANY(:post_ids) AND status = 'attached'
            ORDER BY post_id, position ASC
            """
        ),
        {"post_ids": post_ids},
    )
    return [dict(r) for r in rows.mappings().fetchall()]


async def bind_attachments_to_post(
    session: AsyncSession,
    *,
    post_id: UUID,
    attachment_ids: list[UUID],
    uploader_id: UUID,
) -> None:
    """将 uploaded 状态的附件绑定到帖子；按数组顺序从 0 编号 position。"""
    for position, attachment_id in enumerate(attachment_ids):
        result = await session.execute(
            text(
                """
                UPDATE community_attachments
                SET status = 'attached', post_id = :post_id, position = :position,
                    updated_at = now()
                WHERE attachment_id = :attachment_id
                  AND uploader_id = :uploader_id
                  AND status = 'uploaded'
                """
            ),
            {
                "post_id": post_id,
                "position": position,
                "attachment_id": attachment_id,
                "uploader_id": uploader_id,
            },
        )
        if isinstance(result, CursorResult) and result.rowcount == 0:
            existing = await get_attachment_by_id(session, attachment_id)
            if existing is None:
                raise AttachmentConflictError("附件不存在")
            if existing["uploader_id"] != uploader_id:
                raise AttachmentForbiddenError("无权使用该附件")
            raise AttachmentConflictError("附件状态不可用或已被绑定")


async def mark_attachments_deleted_by_post(
    session: AsyncSession,
    post_id: UUID,
) -> None:
    await session.execute(
        text(
            """
            UPDATE community_attachments
            SET status = 'deleted', next_delete_attempt_at = now(), updated_at = now()
            WHERE post_id = :post_id AND status = 'attached'
            """
        ),
        {"post_id": post_id},
    )


async def convert_uploaded_to_orphaned(
    session: AsyncSession,
    ttl_hours: int,
    batch_size: int,
) -> int:
    """将超时 uploaded 附件转为 orphaned。"""
    result = await session.execute(
        text(
            """
            UPDATE community_attachments
            SET status = 'orphaned', next_delete_attempt_at = now(), updated_at = now()
            WHERE attachment_id IN (
                SELECT attachment_id FROM community_attachments
                WHERE status = 'uploaded'
                  AND created_at < now() - make_interval(hours => :ttl)
                ORDER BY created_at ASC
                LIMIT :batch
            )
            """
        ),
        {"ttl": ttl_hours, "batch": batch_size},
    )
    return int(result.rowcount or 0) if isinstance(result, CursorResult) else 0


async def scan_attachments_to_delete(
    session: AsyncSession,
    batch_size: int,
) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            """
            SELECT * FROM community_attachments
            WHERE status IN ('deleted', 'orphaned')
              AND next_delete_attempt_at IS NOT NULL
              AND next_delete_attempt_at <= now()
            ORDER BY next_delete_attempt_at ASC
            LIMIT :batch
            """
        ),
        {"batch": batch_size},
    )
    return [dict(r) for r in rows.mappings().fetchall()]


async def record_delete_success(
    session: AsyncSession,
    attachment_id: UUID,
) -> None:
    await session.execute(
        text(
            """
            UPDATE community_attachments
            SET storage_deleted_at = now(), next_delete_attempt_at = NULL,
                updated_at = now()
            WHERE attachment_id = :id
            """
        ),
        {"id": attachment_id},
    )


async def record_delete_failure(
    session: AsyncSession,
    attachment_id: UUID,
    error_message: str,
) -> bool:
    """按重试时序表更新：attempts+1，next_delete_attempt_at 退避；返回是否进入终态。"""
    result = await session.execute(
        text(
            """
            UPDATE community_attachments
            SET delete_attempts = delete_attempts + 1,
                last_delete_error = :error,
                next_delete_attempt_at = CASE
                    WHEN delete_attempts = 0 THEN now() + make_interval(hours => 1)
                    WHEN delete_attempts = 1 THEN now() + make_interval(hours => 4)
                    WHEN delete_attempts = 2 THEN now() + make_interval(hours => 12)
                    ELSE NULL
                END,
                updated_at = now()
            WHERE attachment_id = :id
            RETURNING delete_attempts, next_delete_attempt_at
            """
        ),
        {"id": attachment_id, "error": error_message[:500]},
    )
    row = result.mappings().fetchone()
    return row is not None and row["next_delete_attempt_at"] is None


async def delete_attachment_row(
    session: AsyncSession,
    attachment_id: UUID,
) -> None:
    """orphaned 物理删除 + 同事务删除其幂等记录。"""
    await session.execute(
        text(
            "DELETE FROM community_idempotency_requests "
            "WHERE resource_type = 'attachment' AND resource_id = :id"
        ),
        {"id": attachment_id},
    )
    await session.execute(
        text("DELETE FROM community_attachments WHERE attachment_id = :id"),
        {"id": attachment_id},
    )


async def purge_deleted_attachments(
    session: AsyncSession,
    retention_days: int,
    batch_size: int,
) -> int:
    """删除 storage_deleted_at 超过保留期的 deleted 记录及其幂等记录。"""
    rows = await session.execute(
        text(
            """
            SELECT attachment_id FROM community_attachments
            WHERE status = 'deleted'
              AND storage_deleted_at IS NOT NULL
              AND storage_deleted_at < now() - make_interval(days => :retention)
            LIMIT :batch
            """
        ),
        {"retention": retention_days, "batch": batch_size},
    )
    ids = [r["attachment_id"] for r in rows.mappings()]
    if not ids:
        return 0
    await session.execute(
        text(
            "DELETE FROM community_idempotency_requests "
            "WHERE resource_type = 'attachment' AND resource_id = ANY(:ids)"
        ),
        {"ids": ids},
    )
    result = await session.execute(
        text("DELETE FROM community_attachments WHERE attachment_id = ANY(:ids)"),
        {"ids": ids},
    )
    return int(result.rowcount or 0) if isinstance(result, CursorResult) else 0
