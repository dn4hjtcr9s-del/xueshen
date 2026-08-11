"""memory_review_candidates 持久层（§6.3 / §8.8 / §13.6）。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount


async def insert_candidate(
    session: AsyncSession,
    *,
    candidate_id: UUID,
    operation_id: UUID,
    user_id: UUID,
    candidate_type: str,
    normalized_match_key: str,
    candidate_payload: dict[str, Any],
    evidence_refs: list[str],
    confidence: float,
    base_memory_id: str | None = None,
    base_version: int | None = None,
    topic_key: str | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO memory_review_candidates (
                candidate_id, operation_id, user_id, candidate_type,
                base_memory_id, base_version, topic_key, normalized_match_key,
                candidate_payload, evidence_refs, confidence, status
            ) VALUES (
                :candidate_id, :operation_id, :user_id, :candidate_type,
                :base_memory_id, :base_version, :topic_key, :match_key,
                :payload, :evidence_refs, :confidence, 'pending'
            )
            """
        ),
        {
            "candidate_id": candidate_id,
            "operation_id": operation_id,
            "user_id": user_id,
            "candidate_type": candidate_type,
            "base_memory_id": base_memory_id,
            "base_version": base_version,
            "topic_key": topic_key,
            "match_key": normalized_match_key,
            "payload": json.dumps(candidate_payload, ensure_ascii=False),
            "evidence_refs": json.dumps(evidence_refs, ensure_ascii=False),
            "confidence": confidence,
        },
    )


async def get_candidate(session: AsyncSession, *, candidate_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM memory_review_candidates WHERE candidate_id = :id"),
        {"id": candidate_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def list_pending_candidates(
    session: AsyncSession, *, user_id: UUID, limit: int = 50
) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT * FROM memory_review_candidates "
            "WHERE user_id = :user_id AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT :limit"
        ),
        {"user_id": user_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]


async def resolve_candidate(
    session: AsyncSession,
    *,
    candidate_id: UUID,
    status: str,
    reviewed_by: UUID,
    reviewed_at: datetime,
    resolution_target: str | None,
    target_memory_id: str | None,
    resolved_operation_id: UUID,
    tombstone_until: datetime | None,
) -> bool:
    """保存 accept/correct/reject 决议字段（§23.3 可审计重放）。"""
    rowcount = await exec_rowcount(
        session,
        text(
            """
            UPDATE memory_review_candidates
            SET status = :status,
                reviewed_by = :reviewed_by,
                reviewed_at = :reviewed_at,
                resolution_target = :resolution_target,
                target_memory_id = :target_memory_id,
                resolved_operation_id = :resolved_operation_id,
                tombstone_until = :tombstone_until,
                updated_at = now()
            WHERE candidate_id = :candidate_id AND status = 'pending'
            """
        ),
        {
            "candidate_id": candidate_id,
            "status": status,
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
            "resolution_target": resolution_target,
            "target_memory_id": target_memory_id,
            "resolved_operation_id": resolved_operation_id,
            "tombstone_until": tombstone_until,
        },
    )
    return rowcount == 1


async def has_recent_rejected_match(
    session: AsyncSession, *, user_id: UUID, match_key: str, now: datetime
) -> bool:
    """§8.8：30 天内相同匹配键不得重复生成候选。"""
    result = await session.execute(
        text(
            """
            SELECT 1 FROM memory_review_candidates
            WHERE user_id = :user_id AND normalized_match_key = :match_key
              AND status = 'rejected'
              AND (tombstone_until IS NULL OR tombstone_until > :now)
            LIMIT 1
            """
        ),
        {"user_id": user_id, "match_key": match_key, "now": now},
    )
    return result.first() is not None
