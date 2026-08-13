"""conversation_messages 仓储（方案 §7.3）。

- 用户消息创建后即为 completed；助手回答只有完整生成并通过验证才 completed；
- 取消/失败的部分回答保存为 cancelled/failed，默认 eligible_for_context=false、
  eligible_for_memory=false；
- 已完成消息内容不可原地修改。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_INSERT_SQL = text(
    """
    INSERT INTO conversation.conversation_messages (
        message_id, thread_id, turn_id, user_id, sequence, role, content,
        status, content_hash, eligible_for_context, eligible_for_memory,
        occurred_at, completed_at
    ) VALUES (
        :message_id, :thread_id, :turn_id, :user_id, :sequence, :role, :content,
        :status, :content_hash, :eligible_for_context, :eligible_for_memory,
        :occurred_at, :completed_at
    )
    """
)


async def insert_message(
    session: AsyncSession,
    *,
    message_id: UUID,
    thread_id: UUID,
    turn_id: UUID,
    user_id: UUID,
    sequence: int,
    role: str,
    content: str,
    content_hash: str,
    status: str = "completed",
    eligible_for_context: bool = True,
    eligible_for_memory: bool = True,
    occurred_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> None:
    now = occurred_at or datetime.now(UTC)
    await session.execute(
        _INSERT_SQL,
        {
            "message_id": message_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "user_id": user_id,
            "sequence": sequence,
            "role": role,
            "content": content,
            "status": status,
            "content_hash": content_hash,
            "eligible_for_context": eligible_for_context,
            "eligible_for_memory": eligible_for_memory,
            "occurred_at": now,
            "completed_at": completed_at if status == "completed" else None,
        },
    )


async def get_message(
    session: AsyncSession, message_id: UUID, *, include_deleted: bool = False
) -> dict[str, Any] | None:
    sql = "SELECT * FROM conversation.conversation_messages WHERE message_id = :message_id"
    if not include_deleted:
        sql += " AND status != 'deleted'"
    result = await session.execute(text(sql), {"message_id": message_id})
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def get_messages_by_ids(
    session: AsyncSession, message_ids: list[UUID], *, include_deleted: bool = False
) -> list[dict[str, Any]]:
    """按稳定 message_ids 精确读取（Reader §8.3 #3：不能扩展为整个线程）。"""
    sql = "SELECT * FROM conversation.conversation_messages WHERE message_id = ANY(:message_ids)"
    if not include_deleted:
        sql += " AND status != 'deleted'"
    result = await session.execute(text(sql), {"message_ids": list(message_ids)})
    return [dict(row) for row in result.mappings()]


async def list_messages(
    session: AsyncSession,
    thread_id: UUID,
    *,
    before_sequence: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    """消息分页：before_sequence 向历史方向，DB 倒序取页后由调用方正序返回（§17.1）。"""
    params: dict[str, Any] = {"thread_id": thread_id, "limit": limit}
    sql = (
        "SELECT * FROM conversation.conversation_messages "
        "WHERE thread_id = :thread_id AND status != 'deleted'"
    )
    if before_sequence is not None:
        sql += " AND sequence < :before_sequence"
        params["before_sequence"] = before_sequence
    sql += " ORDER BY sequence DESC LIMIT :limit"
    result = await session.execute(text(sql), params)
    return [dict(row) for row in result.mappings()]


async def mark_message_deleted(session: AsyncSession, message_id: UUID) -> None:
    """删除线程时幂等置 deleted（本地清理，§8.6 步骤 3）。"""
    await session.execute(
        text(
            "UPDATE conversation.conversation_messages "
            "SET status = 'deleted', deleted_at = :now "
            "WHERE message_id = :message_id"
        ),
        {"message_id": message_id, "now": datetime.now(UTC)},
    )


async def mark_messages_deleted_for_thread(session: AsyncSession, thread_id: UUID) -> None:
    """delete_thread 本地清理：线程全部消息置 deleted（§8.6 步骤 3）。"""
    await session.execute(
        text(
            "UPDATE conversation.conversation_messages "
            "SET status = 'deleted', deleted_at = :now "
            "WHERE thread_id = :thread_id AND status != 'deleted'"
        ),
        {"thread_id": thread_id, "now": datetime.now(UTC)},
    )


async def increment_thread_sequence(session: AsyncSession, thread_id: UUID, *, by: int = 1) -> int:
    """线程 last_message_sequence 原子 +N；返回新值（消息序列分配，§7.1）。"""
    result = await session.execute(
        text(
            "UPDATE conversation.conversation_threads "
            "SET last_message_sequence = last_message_sequence + :by, updated_at = :now "
            "WHERE thread_id = :thread_id RETURNING last_message_sequence"
        ),
        {"thread_id": thread_id, "by": by, "now": datetime.now(UTC)},
    )
    row = result.mappings().first()
    if row is None:
        return -1
    return int(row["last_message_sequence"])
