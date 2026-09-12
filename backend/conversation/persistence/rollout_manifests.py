"""conversation_rollout_segments 仓储（memory-rebuild §5.4 Phase 2）。

段状态机（Phase 0 决策的三段式 + I-1 修复后的复用语义）：

- ``open``：本地热段，尚未上传；``object_key``/``object_etag``/``sha256``/``byte_size``/
  ``ordinal_end`` 均可空。**懒创建**——首次真正写入（文件物化）时才建行，因此空 turn
  不会在 manifest 留下痕迹。
- ``sealed``：对象已上传且 manifest 已登记，上述字段全部必填且此后不可变
  （``put_immutable`` 对"同 key 异内容"必须抛 :class:`ObjectHashMismatchError`）。
- ``deleted``：逻辑删除 tombstone；跨节点恢复时的旧段、以及同一 turn 重试时被新段
  继承序号范围的旧段都走这条路（保留审计与原对象引用）。

一个 turn 最多一个非删除段（``uq_rollout_segment_turn_active``）。因此"重试复用"的
落地方式是：新段沿用旧段的 ``ordinal_start``（与本地热段文件），但使用**新的
``segment_id``**（因此是新的对象 key），旧行在同一事务里被标 ``deleted``。

``(turn_id)`` 与 ``(thread_id, ordinal_start)`` 的唯一性都只对**非 deleted 行**成立
（部分唯一索引，见迁移 0008 / 0009）。后者正是"标废旧行 + 复用同一 ordinal_start"
能成立的前提。

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
) -> bool:
    """登记一个 ``open`` 段，返回是否**真的插入了行**。

    调用方必须看返回值：``ON CONFLICT (segment_id) DO NOTHING`` 命中时返回 ``False``
    （该段早已登记）。I-1 的根因之一正是"登记是否成功"不可判定——调用方只能把异常
    吞成一行 warning，于是"对象已上传但没有 manifest 行"静默成立。
    """
    result = await session.execute(
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
    return bool(getattr(result, "rowcount", 0) == 1)


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


async def get_active_by_turn(session: AsyncSession, turn_id: UUID) -> dict[str, Any] | None:
    """按 turn 找**非删除**段——崩溃重跑时决定"续写、复用还是新建"（§1.7 步骤 2）。

    与 :func:`get_open_by_turn` 的区别：``sealed`` 段同样返回。一个 turn 第一次尝试
    若已走到 ``close_turn``（图抛异常 → finally 封存），重试时只剩 ``sealed`` 行；
    只查 ``open`` 会让重试无条件新建段，撞上 ``uq_rollout_segment_turn_active``。
    """
    row = (
        (
            await session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM conversation.conversation_rollout_segments "
                    "WHERE turn_id = :turn_id AND status IN ('open', 'sealed') "
                    "ORDER BY ordinal_start DESC LIMIT 1"
                ),
                {"turn_id": turn_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


async def next_ordinal_start(session: AsyncSession, thread_id: UUID) -> int:
    """该 thread 下一个**安全**的 ``ordinal_start``：所有行（含 deleted）的最大末序号 + 1。

    跨节点重建时新段的身份无法从本地文件反推（本地文件已不在），必须由 manifest 给
    出一个不会与任何既有段范围重叠的起点。把 deleted 行也算进来是有意的：软删段（可能
    还有对象、消息指针仍指向它）的序号范围也不能被新段复用。

    尚无任何段时返回 ``0``（thread 的第一个段）。
    """
    value = (
        await session.execute(
            text(
                "SELECT max(COALESCE(ordinal_end, ordinal_start)) "
                "FROM conversation.conversation_rollout_segments WHERE thread_id = :thread_id"
            ),
            {"thread_id": thread_id},
        )
    ).scalar()
    return 0 if value is None else int(value) + 1


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

    ``ordinal_end`` 只前进不后退（``GREATEST``）：复用已封存段续写时，重试可能**没有**
    产生比上次更多的记录，直接写较小的 ``ordinal_end`` 会让整个封存事务撞建表时生成的
    范围约束（``conversation_rollout_segments_check``）而失败，并留下孤儿对象。
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
            "    sha256 = :sha256, byte_size = :byte_size, "
            "    ordinal_end = GREATEST(COALESCE(s.ordinal_end, s.ordinal_start), "
            "                          :ordinal_end), "
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
    """把段标记为 ``deleted``（tombstone），保留行以便审计与 reconcile 对账。

    同时清空仍指向该段的 ``conversation_messages`` 四列指针：软删不会触发 0007 的
    ``BEFORE DELETE`` 触发器（那只在硬删行时生效），而"重试复用同一序号范围/跨节点
    重建"都会把段标废——留着指针就是一条指向已废段的悬垂引用。四列由 CHECK 约束
    要求同真同假，因此必须一次写全。
    """
    await session.execute(
        text(
            "UPDATE conversation.conversation_messages "
            "SET segment_id = NULL, rollout_ordinal = NULL, "
            "    rollout_byte_offset_start = NULL, rollout_byte_offset_end = NULL "
            "WHERE segment_id = :segment_id"
        ),
        {"segment_id": segment_id},
    )
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
