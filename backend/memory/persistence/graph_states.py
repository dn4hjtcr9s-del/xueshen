"""图谱 Overlay、审计与 activity 仓储（规格 §13.9 / §13.10 / §13.8.1）。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def get_overlay(
    session: AsyncSession, *, user_id: UUID, node_id: str, for_update: bool = False
) -> dict[str, Any] | None:
    sql = "SELECT * FROM graph_user_states WHERE user_id = :user_id AND node_id = :node_id"
    if for_update:
        sql += " FOR UPDATE"
    result = await session.execute(text(sql), {"user_id": user_id, "node_id": node_id})
    row = result.mappings().first()
    return dict(row) if row else None


async def list_overlays(session: AsyncSession, *, user_id: UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        text("SELECT * FROM graph_user_states WHERE user_id = :user_id ORDER BY node_id"),
        {"user_id": user_id},
    )
    return [dict(r) for r in result.mappings().all()]


async def lock_overlays(
    session: AsyncSession, *, user_id: UUID, node_ids: list[str]
) -> list[dict[str, Any]]:
    """按 node_id 字典序锁定（§13.18 锁顺序）。"""
    result = await session.execute(
        text(
            "SELECT * FROM graph_user_states "
            "WHERE user_id = :user_id AND node_id = ANY(:node_ids) "
            "ORDER BY node_id ASC FOR UPDATE"
        ),
        {"user_id": user_id, "node_ids": sorted(node_ids)},
    )
    return [dict(r) for r in result.mappings().all()]


async def upsert_overlay(
    session: AsyncSession,
    *,
    user_id: UUID,
    node_id: str,
    status: str,
    status_source: str,
    source_memory_id: str | None,
    source_memory_version: int | None,
    evidence_snapshot: list[dict[str, Any]],
    evidence_count: int,
    last_user_action_at: datetime | None,
    last_evidence_at: datetime | None,
) -> int:
    """插入或更新 Overlay；返回新版本号。"""
    result = await session.execute(
        text(
            """
            INSERT INTO graph_user_states (
                user_id, node_id, status, version, status_source,
                source_memory_id, source_memory_version, evidence_snapshot,
                evidence_count, last_user_action_at, last_evidence_at
            ) VALUES (
                :user_id, :node_id, :status, 1, :status_source,
                :source_memory_id, :source_memory_version,
                CAST(:evidence_snapshot AS jsonb),
                :evidence_count, :last_user_action_at, :last_evidence_at
            )
            ON CONFLICT (user_id, node_id) DO UPDATE
            SET status = EXCLUDED.status,
                version = graph_user_states.version + 1,
                status_source = EXCLUDED.status_source,
                source_memory_id = EXCLUDED.source_memory_id,
                source_memory_version = EXCLUDED.source_memory_version,
                evidence_snapshot = EXCLUDED.evidence_snapshot,
                evidence_count = EXCLUDED.evidence_count,
                last_user_action_at = COALESCE(
                    EXCLUDED.last_user_action_at, graph_user_states.last_user_action_at
                ),
                last_evidence_at = EXCLUDED.last_evidence_at,
                updated_at = now()
            RETURNING version
            """
        ),
        {
            "user_id": user_id,
            "node_id": node_id,
            "status": status,
            "status_source": status_source,
            "source_memory_id": source_memory_id,
            "source_memory_version": source_memory_version,
            "evidence_snapshot": json.dumps(evidence_snapshot, ensure_ascii=False),
            "evidence_count": evidence_count,
            "last_user_action_at": last_user_action_at,
            "last_evidence_at": last_evidence_at,
        },
    )
    return int(result.scalar_one())


async def delete_overlay(session: AsyncSession, *, user_id: UUID, node_id: str) -> int | None:
    """clear/重算降级为无状态：删除活动 Overlay 行（§2.5），返回删除前版本。"""
    result = await session.execute(
        text(
            "DELETE FROM graph_user_states WHERE user_id = :user_id AND node_id = :node_id "
            "RETURNING version"
        ),
        {"user_id": user_id, "node_id": node_id},
    )
    row = result.first()
    return int(row[0]) if row else None


async def insert_audit(
    session: AsyncSession,
    *,
    audit_id: UUID,
    operation_id: UUID | None,
    user_id: UUID,
    node_id: str,
    before_status: str | None,
    after_status: str | None,
    before_version: int | None,
    after_version: int | None,
    actor_type: str,
    reason_codes: list[str],
    evidence_refs: list[str],
    explanation_summary: str | None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO graph_state_audit (
                audit_id, operation_id, user_id, node_id,
                before_status, after_status, before_version, after_version,
                actor_type, reason_codes, evidence_refs, explanation_summary
            ) VALUES (
                :audit_id, :operation_id, :user_id, :node_id,
                :before_status, :after_status, :before_version, :after_version,
                :actor_type, :reason_codes, CAST(:evidence_refs AS jsonb),
                :explanation_summary
            )
            """
        ),
        {
            "audit_id": audit_id,
            "operation_id": operation_id,
            "user_id": user_id,
            "node_id": node_id,
            "before_status": before_status,
            "after_status": after_status,
            "before_version": before_version,
            "after_version": after_version,
            "actor_type": actor_type,
            "reason_codes": reason_codes,
            "evidence_refs": json.dumps(evidence_refs, ensure_ascii=False),
            "explanation_summary": explanation_summary,
        },
    )


async def latest_audit(
    session: AsyncSession, *, user_id: UUID, node_id: str
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT * FROM graph_state_audit "
            "WHERE user_id = :user_id AND node_id = :node_id "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"user_id": user_id, "node_id": node_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def get_audit_by_operation(
    session: AsyncSession, *, operation_id: UUID
) -> dict[str, Any] | None:
    """按 operation_id 查图谱 mutation 审计（评审 #11 crash-window replay 记录）。

    用户图谱命令的 Overlay/audit/Outbox 在同一事务提交；operation 终态未落库
    而进程崩溃后，重放凭此记录重建首次结果，不再重复版本校验与状态写入。
    """
    result = await session.execute(
        text(
            "SELECT * FROM graph_state_audit "
            "WHERE operation_id = :operation_id ORDER BY created_at ASC LIMIT 1"
        ),
        {"operation_id": operation_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def upsert_node_activity(
    session: AsyncSession,
    *,
    user_id: UUID,
    node_id: str,
    activity_type: str,
    event_count: int,
    occurred_at: datetime,
) -> None:
    """activity exposure 幂等聚合（§10.3.1）；去重由调用方保证。"""
    column = {
        "page_view": "last_viewed_at",
        "bookmark": "last_bookmarked_at",
        "check_in": "last_check_in_at",
    }[activity_type]
    await session.execute(
        text(
            f"""
            INSERT INTO graph_user_node_activity (
                user_id, node_id, {column}, event_count
            ) VALUES (
                :user_id, :node_id, :occurred_at, :event_count
            )
            ON CONFLICT (user_id, node_id) DO UPDATE
            SET {column} = GREATEST(
                    graph_user_node_activity.{column}, EXCLUDED.{column}
                ),
                event_count = graph_user_node_activity.event_count + :event_count,
                updated_at = now()
            """
        ),
        {
            "user_id": user_id,
            "node_id": node_id,
            "occurred_at": occurred_at,
            "event_count": event_count,
        },
    )


async def record_activity_event_once(
    session: AsyncSession,
    *,
    user_id: UUID,
    node_id: str,
    activity_type: str,
    activity_id: str,
    event_count: int,
    occurred_at: datetime,
) -> bool:
    """seen 去重 + 计数 upsert 同事务（§10.3.1 / 裁决 A）。

    返回 True 表示首次见到该事件并已累加计数；False 表示重复事件，未改动。
    """
    from backend.memory.persistence.database import exec_rowcount

    inserted = await exec_rowcount(
        session,
        text(
            """
            INSERT INTO graph_activity_seen_events (
                user_id, node_id, activity_type, activity_id
            ) VALUES (:user_id, :node_id, :activity_type, :activity_id)
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "user_id": user_id,
            "node_id": node_id,
            "activity_type": activity_type,
            "activity_id": activity_id,
        },
    )
    if inserted != 1:
        return False
    await upsert_node_activity(
        session,
        user_id=user_id,
        node_id=node_id,
        activity_type=activity_type,
        event_count=event_count,
        occurred_at=occurred_at,
    )
    return True


async def list_node_activity(session: AsyncSession, *, user_id: UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        text("SELECT * FROM graph_user_node_activity WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    return [dict(r) for r in result.mappings().all()]


# ---------------------------------------------------------------------------
# memory_graph_links（§13.8.1）
# ---------------------------------------------------------------------------


async def upsert_graph_link(
    session: AsyncSession,
    *,
    user_id: UUID,
    memory_id: str,
    node_id: str,
    memory_version: int,
    mapping_method: str,
    mapping_confidence: float,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO memory_graph_links (
                user_id, memory_id, node_id, memory_version,
                mapping_method, mapping_confidence, active
            ) VALUES (
                :user_id, :memory_id, :node_id, :memory_version,
                :mapping_method, :mapping_confidence, true
            )
            ON CONFLICT (user_id, memory_id, node_id) DO UPDATE
            SET memory_version = EXCLUDED.memory_version,
                mapping_method = EXCLUDED.mapping_method,
                mapping_confidence = EXCLUDED.mapping_confidence,
                active = true, updated_at = now()
            """
        ),
        {
            "user_id": user_id,
            "memory_id": memory_id,
            "node_id": node_id,
            "memory_version": memory_version,
            "mapping_method": mapping_method,
            "mapping_confidence": mapping_confidence,
        },
    )


async def deactivate_graph_links(
    session: AsyncSession,
    *,
    user_id: UUID,
    memory_id: str,
    except_node_ids: list[str] | None = None,
) -> None:
    """映射失效或记忆删除时置 active=false（§16.4）。

    ``except_node_ids`` 是**一次排除的集合**：集合内的 node_id 保持原状，其余全部置
    inactive；传 ``None`` 表示"一个不留"（记忆删除路径）。

    为什么必须是集合而不是单个 node_id（评审新发现 1）：本 SQL **没有版本谓词**，
    调用方若按 node_id 循环调用 ``deactivate(except_node_id=kept)``，"本次要保留"的
    节点会在后续轮次里被前一轮的调用连带置 false——两个节点就会把两条 link 同时
    杀掉（实测 active links = 0/2）。集合语义下这段判断只发生一次，不存在轮次之间的
    相互覆盖。
    """
    await session.execute(
        text(
            """
            UPDATE memory_graph_links
            SET active = false, updated_at = now()
            WHERE user_id = :user_id AND memory_id = :memory_id
              AND (
                    CAST(:except_node_ids AS text[]) IS NULL
                    OR node_id <> ALL(CAST(:except_node_ids AS text[]))
                  )
            """
        ),
        {"user_id": user_id, "memory_id": memory_id, "except_node_ids": except_node_ids},
    )


async def list_links_for_memory(
    session: AsyncSession, *, user_id: UUID, memory_id: str
) -> list[dict[str, Any]]:
    """该记忆的**全部** link 行（含 inactive 与旧版本），按 node_id 稳定排序。

    供提交路径一次取齐"上一版活动的映射"与"每个节点最近一次的映射来源"：
    ``memory_graph_links`` 的主键是 ``(user_id, memory_id, node_id)``，同一节点至多
    一行，因此它天然就是"每个节点最近一行的快照"，不需要两次查询。
    """
    result = await session.execute(
        text(
            "SELECT * FROM memory_graph_links "
            "WHERE user_id = :user_id AND memory_id = :memory_id "
            "ORDER BY node_id ASC"
        ),
        {"user_id": user_id, "memory_id": memory_id},
    )
    return [dict(r) for r in result.mappings().all()]


async def list_active_links_for_memory(
    session: AsyncSession, *, user_id: UUID, memory_id: str, active_version: int
) -> list[dict[str, Any]]:
    """projection/推荐只读 active=true 且版本等于活动版本的 link（§13.8.1）。"""
    result = await session.execute(
        text(
            "SELECT * FROM memory_graph_links "
            "WHERE user_id = :user_id AND memory_id = :memory_id "
            "AND active = true AND memory_version = :active_version"
        ),
        {"user_id": user_id, "memory_id": memory_id, "active_version": active_version},
    )
    return [dict(r) for r in result.mappings().all()]


async def list_current_active_links(
    session: AsyncSession, *, user_id: UUID
) -> list[dict[str, Any]]:
    """推荐接口用：全部 active=true 且版本等于当前活动 mastery 版本的 link（§16.5）。"""
    result = await session.execute(
        text(
            """
            SELECT l.* FROM memory_graph_links l
            JOIN memory_documents d
              ON d.user_id = l.user_id AND d.memory_id = l.memory_id
            WHERE l.user_id = :user_id
              AND l.active = true AND l.memory_version = d.active_version
              AND d.deleted_at IS NULL
            ORDER BY l.node_id, l.memory_id
            """
        ),
        {"user_id": user_id},
    )
    return [dict(r) for r in result.mappings().all()]


async def list_active_links_for_node(
    session: AsyncSession, *, user_id: UUID, node_id: str
) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            """
            SELECT l.* FROM memory_graph_links l
            JOIN memory_documents d
              ON d.user_id = l.user_id AND d.memory_id = l.memory_id
            WHERE l.user_id = :user_id AND l.node_id = :node_id
              AND l.active = true AND l.memory_version = d.active_version
              AND d.deleted_at IS NULL
            """
        ),
        {"user_id": user_id, "node_id": node_id},
    )
    return [dict(r) for r in result.mappings().all()]
