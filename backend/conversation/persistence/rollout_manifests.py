"""conversation_rollout_segments 仓储（memory-rebuild §5.4 Phase 2）。

段状态机（Phase 0 决策的三段式）：

- ``open``：本地热段，尚未上传；``object_key``/``object_etag``/``sha256``/``byte_size``/
  ``ordinal_end`` 均可空。**懒创建**——首次真正写入（文件物化）时才建行，因此空 turn
  不会在 manifest 留下痕迹。
- ``sealed``：对象已上传且 manifest 已登记，上述字段全部必填且此后不可变。
- ``deleted``：逻辑删除 tombstone；跨节点恢复时旧 ``open`` 段也走这条路（保留审计）。

**封存顺序铁律**（§5.4）：先上传对象成功，再在同一 PG 事务内写 manifest 与 message
pointer，事务成功后才把段标为 sealed。反向顺序会产生"manifest 指向不存在对象"的
悬空引用；崩溃在两步之间只会留下孤儿对象，由 reconcile 处理。

**fencing**：所有状态更新都带 ``(lease_owner, lease_generation)`` 校验——失租的 worker
不得再改 manifest（与 finalize 的 fencing 同源）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_COLUMNS = (
    "segment_id, thread_id, turn_id, ordinal_start, ordinal_end, object_key, object_etag, "
    "sha256, byte_size, status, created_at, sealed_at, deleted_at"
)


async def insert_open(
    session: AsyncSession,
    *,
    segment_id: UUID,
    thread_id: UUID,
    turn_id: UUID,
    ordinal_start: int,
    created_at: datetime | None = None,
) -> None:
    """登记一个 ``open`` 段（首次写入时调用；重复调用幂等）。"""
    await session.execute(
        text(
            """
            INSERT INTO conversation.conversation_rollout_segments (
                segment_id, thread_id, turn_id, ordinal_start, status, created_at
            ) VALUES (
                :segment_id, :thread_id, :turn_id, :ordinal_start, 'open', :created_at
            )
            ON CONFLICT (segment_id) DO NOTHING
            """
        ),
        {
            "segment_id": segment_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "ordinal_start": ordinal_start,
            "created_at": created_at or datetime.now(UTC),
        },
    )


async def get_by_id(session: AsyncSession, segment_id: UUID) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM conversation.conversation_rollout_segments "
                    "WHERE segment_id = :segment_id"
                ),
                {"segment_id": segment_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


async def get_open_by_turn(session: AsyncSession, turn_id: UUID) -> dict[str, Any] | None:
    """按 turn 找未封存段——崩溃重跑时决定"续写还是重建"（§1.7 步骤 2）。"""
    row = (
        (
            await session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM conversation.conversation_rollout_segments "
                    "WHERE turn_id = :turn_id AND status = 'open' "
                    "ORDER BY ordinal_start DESC LIMIT 1"
                ),
                {"turn_id": turn_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


async def list_by_thread(
    session: AsyncSession, thread_id: UUID, *, include_deleted: bool = False
) -> list[dict[str, Any]]:
    """按 ordinal 升序列出该 thread 的段（重放与删除用）。"""
    clause = "" if include_deleted else " AND status <> 'deleted'"
    rows = (
        (
            await session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM conversation.conversation_rollout_segments "
                    f"WHERE thread_id = :thread_id{clause} ORDER BY ordinal_start"
                ),
                {"thread_id": thread_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


async def seal(
    session: AsyncSession,
    *,
    segment_id: UUID,
    object_key: str,
    object_etag: str,
    sha256: str,
    byte_size: int,
    ordinal_end: int,
    fence: tuple[str, int] | None = None,
    sealed_at: datetime | None = None,
) -> bool:
    """把 ``open`` 段标记为 ``sealed``；返回是否真的更新了一行。

    返回 False 表示段已被别的执行者封存、已删除，或 fencing 校验失败——调用方
    据此判断"不是我封的"，不要把它当成成功。
    """
    fence_clause = ""
    params: dict[str, Any] = {
        "segment_id": segment_id,
        "object_key": object_key,
        "object_etag": object_etag,
        "sha256": sha256,
        "byte_size": byte_size,
        "ordinal_end": ordinal_end,
        "sealed_at": sealed_at or datetime.now(UTC),
    }
    if fence is not None:
        fence_clause = (
            " AND EXISTS ("
            "  SELECT 1 FROM conversation.conversation_turns AS t"
            "  WHERE t.turn_id = s.turn_id"
            "    AND t.lease_owner = :lease_owner"
            "    AND t.lease_generation = :lease_generation"
            ")"
        )
        params["lease_owner"] = fence[0]
        params["lease_generation"] = fence[1]
    result = await session.execute(
        text(
            "UPDATE conversation.conversation_rollout_segments AS s "
            "SET status = 'sealed', object_key = :object_key, object_etag = :object_etag, "
            "    sha256 = :sha256, byte_size = :byte_size, ordinal_end = :ordinal_end, "
            "    sealed_at = :sealed_at "
            "WHERE s.segment_id = :segment_id AND s.status = 'open'" + fence_clause
        ),
        params,
    )
    return bool(getattr(result, "rowcount", 0) == 1)


async def mark_deleted(
    session: AsyncSession,
    *,
    segment_id: UUID,
    deleted_at: datetime | None = None,
) -> bool:
    """把段标记为 ``deleted``（tombstone），保留行以便审计与 reconcile 对账。"""
    result = await session.execute(
        text(
            "UPDATE conversation.conversation_rollout_segments "
            "SET status = 'deleted', deleted_at = :deleted_at "
            "WHERE segment_id = :segment_id AND status <> 'deleted'"
        ),
        {"segment_id": segment_id, "deleted_at": deleted_at or datetime.now(UTC)},
    )
    return bool(getattr(result, "rowcount", 0) == 1)


async def mark_thread_deleted(
    session: AsyncSession,
    *,
    thread_id: UUID,
    deleted_at: datetime | None = None,
) -> list[str]:
    """thread 删除：全部段置 ``deleted``，返回需要清理的对象 key（§1.8）。

    返回的 key 供调用方在本地/对象存储侧物理删除；PG 侧只留 tombstone。
    """
    rows = (
        (
            await session.execute(
                text(
                    "UPDATE conversation.conversation_rollout_segments "
                    "SET status = 'deleted', deleted_at = :deleted_at "
                    "WHERE thread_id = :thread_id AND status <> 'deleted' "
                    "RETURNING object_key"
                ),
                {"thread_id": thread_id, "deleted_at": deleted_at or datetime.now(UTC)},
            )
        )
        .scalars()
        .all()
    )
    return [key for key in rows if key]


async def list_orphan_candidates(
    session: AsyncSession, *, limit: int = 1000
) -> list[dict[str, Any]]:
    """reconcile 用：``open`` 段（可能已上传但未登记）与 ``sealed`` 段（需校验对象）。"""
    rows = (
        (
            await session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM conversation.conversation_rollout_segments "
                    "WHERE status IN ('open', 'sealed') ORDER BY created_at LIMIT :limit"
                ),
                {"limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]
