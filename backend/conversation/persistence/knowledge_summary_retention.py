"""知识总结 retention 与运行控制 SQL（方案 §20.5、§21.5）。

所有操作都在调用方事务中执行，使用小批量锁定和可重复执行的 UPDATE/DELETE，避免维护任务
在单个异常行上回滚整批数据。tombstone 表不在本模块物理清理范围内。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount

_TERMINAL_STATUSES = "('succeeded', 'no_change', 'dead_letter', 'cancelled')"


async def scrub_model_call_payloads(
    session: AsyncSession, *, cutoff: datetime, batch_size: int
) -> int:
    """清除终态模型调用正文，仅保留稳定 scrub 标记。"""
    result = await exec_rowcount(
        session,
        text(
            f"""
            WITH candidates AS (
                SELECT m.call_id
                FROM conversation.knowledge_summary_model_calls m
                JOIN conversation.knowledge_summary_generation_jobs j
                  ON j.generation_id = m.generation_id
                WHERE m.response_payload IS NOT NULL
                  AND m.payload_scrubbed_at IS NULL
                  AND j.status IN {_TERMINAL_STATUSES}
                  AND j.completed_at <= :cutoff
                ORDER BY m.created_at ASC, m.call_id ASC
                LIMIT :batch_size
                FOR UPDATE OF m SKIP LOCKED
            )
            UPDATE conversation.knowledge_summary_model_calls m
            SET response_payload = jsonb_build_object('scrubbed', true),
                payload_scrubbed_at = now()
            FROM candidates c
            WHERE m.call_id = c.call_id
            """
        ),
        {"cutoff": cutoff, "batch_size": batch_size},
    )
    return result


async def scrub_generation_payloads(
    session: AsyncSession, *, cutoff: datetime, batch_size: int
) -> int:
    """清除 Job 中候选/提案正文，保留输入 hash 和来源 checkpoint 元数据。"""
    result = await exec_rowcount(
        session,
        text(
            f"""
            WITH candidates AS (
                SELECT j.generation_id
                FROM conversation.knowledge_summary_generation_jobs j
                WHERE (
                    j.status IN {_TERMINAL_STATUSES} OR (
                        j.status = 'needs_review'
                        AND NOT EXISTS (
                            SELECT 1 FROM conversation.knowledge_summary_reviews r
                            WHERE r.generation_id = j.generation_id AND r.status = 'pending'
                        )
                    )
                )
                AND j.completed_at <= :cutoff
                AND (j.input_manifest IS NOT NULL OR j.extraction_result IS NOT NULL
                     OR j.merge_plan_result IS NOT NULL)
                ORDER BY j.completed_at ASC, j.generation_id ASC
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            UPDATE conversation.knowledge_summary_generation_jobs j
            SET input_manifest = CASE
                    WHEN j.input_manifest IS NULL THEN NULL
                    ELSE jsonb_build_object(
                        'schema_version', j.input_manifest->'schema_version',
                        'input_hash', j.input_manifest->'input_hash',
                        'source_checkpoint_id', j.source_checkpoint_id
                    )
                END,
                extraction_result = CASE
                    WHEN j.extraction_result IS NULL THEN NULL
                    ELSE jsonb_build_object('scrubbed', true)
                END,
                merge_plan_result = CASE
                    WHEN j.merge_plan_result IS NULL THEN NULL
                    ELSE jsonb_build_object('scrubbed', true)
                END,
                updated_at = now()
            FROM candidates c
            WHERE j.generation_id = c.generation_id
            """
        ),
        {"cutoff": cutoff, "batch_size": batch_size},
    )
    return result


async def list_deleted_summary_ids(
    session: AsyncSession, *, cutoff: datetime, batch_size: int
) -> list[UUID]:
    """锁定达到保留期的 deleted summary，供调用方按父子 FK 顺序删除。"""
    result = await session.execute(
        text(
            """
            SELECT summary_id
            FROM conversation.knowledge_summaries
            WHERE status = 'deleted' AND deleted_at <= :cutoff
            ORDER BY deleted_at ASC, summary_id ASC
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"cutoff": cutoff, "batch_size": batch_size},
    )
    return [UUID(str(value)) for value in result.scalars()]


async def delete_summary_parent(session: AsyncSession, *, summary_id: UUID) -> int:
    """按 FK 级联物理删除一张已到期 summary；tombstone 独立保留。"""
    return await exec_rowcount(
        session,
        text(
            """
            DELETE FROM conversation.knowledge_summaries
            WHERE summary_id = :summary_id AND status = 'deleted'
            """
        ),
        {"summary_id": summary_id},
    )


async def list_expired_generation_ids(
    session: AsyncSession, *, cutoff: datetime, batch_size: int
) -> list[UUID]:
    """锁定可物理删除的 Generation Job，needs_review 必须没有 pending review。"""
    result = await session.execute(
        text(
            """
            SELECT j.generation_id
            FROM conversation.knowledge_summary_generation_jobs j
            WHERE j.completed_at <= :cutoff
              AND j.status <> 'processing'
              AND (
                j.status <> 'needs_review'
                OR NOT EXISTS (
                    SELECT 1 FROM conversation.knowledge_summary_reviews r
                    WHERE r.generation_id = j.generation_id AND r.status = 'pending'
                )
              )
            ORDER BY j.completed_at ASC, j.generation_id ASC
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"cutoff": cutoff, "batch_size": batch_size},
    )
    return [UUID(str(value)) for value in result.scalars()]


async def delete_generation_parent(session: AsyncSession, *, generation_id: UUID) -> int:
    """物理删除 Generation Job 及其 model call/review/duplicate 子表。"""
    return await exec_rowcount(
        session,
        text(
            """
            DELETE FROM conversation.knowledge_summary_generation_jobs
            WHERE generation_id = :generation_id AND status <> 'processing'
            """
        ),
        {"generation_id": generation_id},
    )


async def get_queue_depths(session: AsyncSession) -> dict[tuple[str, str], int]:
    """读取待处理队列深度，供 Prometheus Gauge 使用。"""
    result = await session.execute(
        text(
            """
            SELECT status, trigger, COUNT(*)::integer AS depth
            FROM conversation.knowledge_summary_generation_jobs
            WHERE status IN ('pending', 'retry_wait')
            GROUP BY status, trigger
            """
        )
    )
    return {
        (str(row["status"]), str(row["trigger"])): int(row["depth"]) for row in result.mappings()
    }


def auto_suspension_reasons(
    *,
    queue_depth: int,
    oldest_at: datetime | None,
    failure_calls: int,
    total_calls: int,
    tokens_today: int,
    queue_limit: int,
    oldest_limit_seconds: int,
    failure_rate: float,
    minimum_calls: int,
    daily_token_budget: int | None,
    now: datetime,
) -> list[str]:
    """按 §21.5 冻结阈值计算自动熔断原因；不执行数据库写入。"""
    reasons: list[str] = []
    if queue_depth >= queue_limit:
        reasons.append("QUEUE_DEPTH")
    if oldest_at is not None and (now - oldest_at).total_seconds() > oldest_limit_seconds:
        reasons.append("OLDEST_JOB_AGE")
    if total_calls >= minimum_calls and failure_calls / total_calls >= failure_rate:
        reasons.append("MODEL_FAILURE_RATE")
    if daily_token_budget is not None and tokens_today > daily_token_budget:
        reasons.append("DAILY_TOKEN_BUDGET")
    return reasons


async def evaluate_auto_suspension(
    session: AsyncSession,
    *,
    queue_limit: int,
    oldest_limit_seconds: int,
    failure_rate: float,
    minimum_calls: int,
    daily_token_budget: int | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """按冻结阈值执行一次自动生成熔断；已暂停时保持人工恢复语义。"""
    now = now or datetime.now(UTC)
    control = (
        (
            await session.execute(
                text(
                    """
                SELECT * FROM conversation.knowledge_summary_runtime_control
                WHERE control_key = 'global' FOR UPDATE
                """
                )
            )
        )
        .mappings()
        .first()
    )
    if control is None or bool(control["auto_generation_suspended"]):
        return None

    queue_depth, oldest_at = (
        await session.execute(
            text(
                """
                SELECT COUNT(*)::integer, MIN(created_at)
                FROM conversation.knowledge_summary_generation_jobs
                WHERE trigger = 'auto' AND status IN ('pending', 'retry_wait')
                """
            )
        )
    ).one()
    failure_calls, total_calls = (
        await session.execute(
            text(
                """
                SELECT
                    COUNT(*) FILTER (WHERE c.status = 'failed')::integer,
                    COUNT(*)::integer
                FROM conversation.knowledge_summary_model_calls c
                JOIN conversation.knowledge_summary_generation_jobs j
                  ON j.generation_id = c.generation_id
                WHERE j.trigger = 'auto' AND c.created_at >= :since
                """
            ),
            {"since": now - timedelta(minutes=5)},
        )
    ).one()
    tokens_today = (
        await session.execute(
            text(
                """
                SELECT COALESCE(SUM(COALESCE(c.input_tokens, 0) + COALESCE(c.output_tokens, 0)), 0)
                FROM conversation.knowledge_summary_model_calls c
                JOIN conversation.knowledge_summary_generation_jobs j
                  ON j.generation_id = c.generation_id
                WHERE j.trigger = 'auto'
                  AND c.created_at >= date_trunc('day', :now AT TIME ZONE 'UTC')
                """
            ),
            {"now": now},
        )
    ).scalar_one()

    reasons = auto_suspension_reasons(
        queue_depth=int(queue_depth),
        oldest_at=oldest_at,
        failure_calls=int(failure_calls),
        total_calls=int(total_calls),
        tokens_today=int(tokens_today),
        queue_limit=queue_limit,
        oldest_limit_seconds=oldest_limit_seconds,
        failure_rate=failure_rate,
        minimum_calls=minimum_calls,
        daily_token_budget=daily_token_budget,
        now=now,
    )
    if not reasons:
        return None

    snapshot = {
        "queue_depth": int(queue_depth),
        "oldest_job_age_seconds": int((now - oldest_at).total_seconds()) if oldest_at else 0,
        "failure_calls": int(failure_calls),
        "total_calls": int(total_calls),
        "tokens_today": int(tokens_today),
    }
    await session.execute(
        text(
            """
            UPDATE conversation.knowledge_summary_runtime_control
            SET auto_generation_suspended = true,
                suspend_reason_code = :reason,
                suspend_snapshot = CAST(:snapshot AS jsonb),
                suspended_at = :now,
                updated_by = 'system:auto-suspension',
                updated_at = :now
            WHERE control_key = 'global' AND auto_generation_suspended = false
            """
        ),
        {"reason": ",".join(reasons), "snapshot": json.dumps(snapshot), "now": now},
    )
    return {"reasons": reasons, **snapshot}
