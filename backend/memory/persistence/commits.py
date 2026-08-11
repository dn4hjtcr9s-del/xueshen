"""memory_commits 仓储（规格 §13.4）。"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def get_by_mutation_id(session: AsyncSession, mutation_id: UUID) -> dict[str, Any] | None:
    """副作用重放判定：同一 mutation_id 已有 commit 则复用（§11.3）。"""
    result = await session.execute(
        text("SELECT * FROM memory_commits WHERE mutation_id = :mutation_id"),
        {"mutation_id": mutation_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def insert_commit(
    session: AsyncSession,
    *,
    commit_id: UUID,
    mutation_id: UUID,
    operation_id: UUID,
    user_id: UUID,
    memory_id: str,
    action: str,
    before_version: int | None,
    after_version: int | None,
    storage_key: str | None,
    checksum: str | None,
    actor_type: str,
    evidence_refs: list[str],
    commit_payload: dict[str, Any],
    prompt_version: str | None,
    model_name: str | None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO memory_commits (
                commit_id, mutation_id, operation_id, user_id, memory_id, action,
                before_version, after_version, storage_key, checksum, actor_type,
                evidence_refs, commit_payload, prompt_version, model_name
            ) VALUES (
                :commit_id, :mutation_id, :operation_id, :user_id, :memory_id, :action,
                :before_version, :after_version, :storage_key, :checksum, :actor_type,
                CAST(:evidence_refs AS jsonb), CAST(:commit_payload AS jsonb),
                :prompt_version, :model_name
            )
            """
        ),
        {
            "commit_id": commit_id,
            "mutation_id": mutation_id,
            "operation_id": operation_id,
            "user_id": user_id,
            "memory_id": memory_id,
            "action": action,
            "before_version": before_version,
            "after_version": after_version,
            "storage_key": storage_key,
            "checksum": checksum,
            "actor_type": actor_type,
            "evidence_refs": json.dumps(evidence_refs, ensure_ascii=False),
            "commit_payload": json.dumps(commit_payload, ensure_ascii=False),
            "prompt_version": prompt_version,
            "model_name": model_name,
        },
    )


async def list_recent_accepted_candidate_weights(
    session: AsyncSession, *, user_id: UUID, memory_id: str, limit: int = 5
) -> list[dict[str, Any]]:
    """最近 N 条已接受候选（confidence 加权聚合，§9.3）。"""
    result = await session.execute(
        text(
            "SELECT confidence, evidence_refs FROM memory_review_candidates "
            "WHERE user_id = :user_id "
            "AND (base_memory_id = :memory_id OR target_memory_id = :memory_id) "
            "AND status IN ('accepted', 'corrected') "
            "ORDER BY reviewed_at DESC NULLS LAST LIMIT :limit"
        ),
        {"user_id": user_id, "memory_id": memory_id, "limit": limit},
    )
    return [dict(r) for r in result.mappings().all()]
