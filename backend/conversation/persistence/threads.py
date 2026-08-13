"""conversation_threads 仓储（方案 §7.1 / §8.6）。

- version 乐观锁：非幂等 Turn 创建锁定 Thread 行并比较 version（§1.5 R5）；
- 删除 API 事务置 deleting、递增 deletion_generation（§8.6 步骤 1）；
- delete_thread Job 是 deleting → deleted 的唯一协调器（§1.5 R3）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.conversation.contracts.domain import ThreadRow
from backend.memory.persistence.database import exec_rowcount

_INSERT_SQL = text(
    """
    INSERT INTO conversation.conversation_threads (
        thread_id, user_id, title, status, version, last_message_sequence,
        deletion_generation, created_at, updated_at
    ) VALUES (
        :thread_id, :user_id, NULL, 'active', 0, 0, 0, :now, :now
    )
    """
)


async def insert_thread(session: AsyncSession, thread_id: UUID, user_id: UUID) -> bool:
    return (
        await exec_rowcount(
            session,
            _INSERT_SQL,
            {"thread_id": thread_id, "user_id": user_id, "now": datetime.now(UTC)},
        )
    ) == 1


async def get_thread(
    session: AsyncSession, thread_id: UUID, *, for_update: bool = False
) -> dict[str, Any] | None:
    sql = "SELECT * FROM conversation.conversation_threads WHERE thread_id = :thread_id"
    if for_update:
        sql += " FOR UPDATE"
    result = await session.execute(text(sql), {"thread_id": thread_id})
    row = result.mappings().first()
    return dict(row) if row is not None else None


def thread_row_from_row(row: dict[str, Any]) -> ThreadRow:
    """数据库行 → 域对象（degraded_flags 等 JSON 化字段统一转 list）。"""
    return ThreadRow(
        thread_id=row["thread_id"],
        user_id=row["user_id"],
        title=row.get("title"),
        status=row["status"],
        version=row["version"],
        last_message_sequence=row["last_message_sequence"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row.get("deleted_at"),
        deletion_generation=row["deletion_generation"],
    )


async def bump_thread_version(session: AsyncSession, thread_id: UUID) -> int:
    """接受用户消息后 Thread version +1（§1.5 R5）。

    返回新版本；必须在 Turn 创建事务内调用。
    """
    result = await session.execute(
        text(
            "UPDATE conversation.conversation_threads "
            "SET version = version + 1, updated_at = :now "
            "WHERE thread_id = :thread_id RETURNING version"
        ),
        {"thread_id": thread_id, "now": datetime.now(UTC)},
    )
    row = result.mappings().first()
    if row is None:
        return -1
    return int(row["version"])


async def set_thread_status(
    session: AsyncSession,
    thread_id: UUID,
    status: str,
    *,
    bump_deletion_generation: bool = False,
    deleted_at: datetime | None = None,
) -> None:
    """置线程状态；删除时递增 deletion_generation（§8.6 步骤 1）。"""
    await session.execute(
        text(
            "UPDATE conversation.conversation_threads "
            "SET status = :status, updated_at = :now, "
            "deletion_generation = deletion_generation + :inc, deleted_at = :deleted_at "
            "WHERE thread_id = :thread_id"
        ),
        {
            "status": status,
            "now": datetime.now(UTC),
            "inc": 1 if bump_deletion_generation else 0,
            "deleted_at": deleted_at,
            "thread_id": thread_id,
        },
    )


async def list_threads(
    session: AsyncSession,
    user_id: UUID,
    *,
    before_cursor: tuple[datetime, UUID] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """会话列表：不透明 cursor（updated_at DESC, thread_id DESC）（§17.1）。

    删除中/已删除线程默认不进入列表；调用方负责签发 cursor。
    """
    params: dict[str, Any] = {"user_id": user_id, "limit": limit}
    sql = (
        "SELECT * FROM conversation.conversation_threads "
        "WHERE user_id = :user_id AND status NOT IN ('deleting', 'deleted')"
    )
    if before_cursor is not None:
        sql += " AND (updated_at, thread_id) < (:before_updated, :before_thread)"
        params["before_updated"] = before_cursor[0]
        params["before_thread"] = before_cursor[1]
    sql += " ORDER BY updated_at DESC, thread_id DESC LIMIT :limit"
    result = await session.execute(text(sql), params)
    return [dict(row) for row in result.mappings()]


async def count_pending_threads(session: AsyncSession) -> int:
    """运维/指标：删除中线程计数（dead letter 或等待中）。"""
    result = await session.execute(
        text("SELECT COUNT(*) FROM conversation.conversation_threads WHERE status = 'deleting'")
    )
    return int(result.scalar_one())
