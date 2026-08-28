"""知识总结 Generation Job 的持久化协调器（知识总结方案 §7.5、§13、§14）。

本模块只负责队列领取、租约 fencing、模型调用审计和 Job 终态写回；不解析模型正文，
也不承载总结内容的业务合并规则。所有写回都带 worker 身份与 lease_generation，
避免过期 Worker 在新 Worker 接管后产生副作用。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.conversation import metrics
from backend.memory.persistence.database import exec_rowcount

_TRIGGER_PRIORITY_SQL = """
CASE trigger
    WHEN 'manual_refresh' THEN 0
    WHEN 'manual_retry' THEN 1
    WHEN 'manual' THEN 2
    WHEN 'ops_retry' THEN 3
    ELSE 4
END
"""


def knowledge_summary_backoff_seconds(attempt: int, *, rng: Any) -> float:
    """知识总结专用全抖动退避：1、2、4、8、16 秒上限（§13.1）。"""
    base = min(2 ** max(0, attempt - 1), 16)
    return float(rng.random()) * float(base)


async def insert_generation_job(
    session: AsyncSession,
    *,
    generation_id: UUID,
    idempotency_key: str,
    client_request_id: str | None,
    user_id: UUID,
    thread_id: UUID,
    turn_id: UUID,
    source_checkpoint_id: str,
    trigger: str,
    primary_turn_occurred_at: datetime,
) -> bool:
    """插入 Generation Job；幂等键或客户端请求键冲突时返回 False。"""
    result = await exec_rowcount(
        session,
        text(
            """
            INSERT INTO conversation.knowledge_summary_generation_jobs (
                generation_id, idempotency_key, client_request_id, user_id, thread_id, turn_id,
                source_checkpoint_id, trigger, status, primary_turn_occurred_at,
                next_attempt_at, created_at, updated_at
            ) VALUES (
                :generation_id, :idempotency_key, :client_request_id, :user_id,
                :thread_id, :turn_id,
                :source_checkpoint_id, :trigger, 'pending', :primary_turn_occurred_at,
                :now, :now, :now
            )
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "generation_id": generation_id,
            "idempotency_key": idempotency_key,
            "client_request_id": client_request_id,
            "user_id": user_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "source_checkpoint_id": source_checkpoint_id,
            "trigger": trigger,
            "primary_turn_occurred_at": primary_turn_occurred_at,
            "now": datetime.now(UTC),
        },
    )
    return result == 1


async def claim_generation_jobs(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    max_concurrency: int,
    manual_reserved_slots: int,
    auto_generation_suspended: bool = False,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """按优先级领取 Job，并为自动任务保留手动执行槽。"""
    now = now or datetime.now(UTC)
    # 全局 claim 协调锁覆盖“有效 lease 计数 → 选取 → 更新 claim”完整区间，
    # 防止多个 Worker 同时看到相同可用槽位而突破并发上限。
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_name))"),
        {"lock_name": "conversation:knowledge-summary-generation-claim"},
    )
    processing = int(
        (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM conversation.knowledge_summary_generation_jobs
                    WHERE status = 'processing'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at > :now
                    """
                ),
                {"now": now},
            )
        ).scalar_one()
    )
    available = max(0, max_concurrency - processing)
    if available == 0:
        return []

    result = await session.execute(
        text(
            f"""
            SELECT *
            FROM conversation.knowledge_summary_generation_jobs
            WHERE (
                (status IN ('pending', 'retry_wait') AND next_attempt_at <= :now)
                OR (status = 'processing' AND (
                    lease_expires_at IS NULL OR lease_expires_at <= :now
                ))
            )
              AND (:auto_generation_suspended = false OR trigger <> 'auto')
            ORDER BY {_TRIGGER_PRIORITY_SQL}, created_at ASC, generation_id ASC
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"now": now, "limit": available, "auto_generation_suspended": auto_generation_suspended},
    )
    rows = [dict(row) for row in result.mappings()]
    claimed: list[dict[str, Any]] = []
    seen_users: set[UUID] = set()
    auto_slots = max(0, available - manual_reserved_slots)
    auto_claimed = 0
    for row in rows:
        if row["user_id"] in seen_users:
            continue
        if row["trigger"] == "auto" and auto_claimed >= auto_slots:
            continue
        # 同一用户唯一 processing 索引是最终安全边界；事务内跳过冲突候选。
        generation = int(row["lease_generation"]) + 1
        expires = now + timedelta(seconds=lease_seconds)
        try:
            async with session.begin_nested():
                updated = await exec_rowcount(
                    session,
                    text(
                        """
                        UPDATE conversation.knowledge_summary_generation_jobs
                        SET status = 'processing', lease_owner = :owner,
                            lease_generation = :generation, lease_expires_at = :expires,
                            attempt_count = attempt_count + 1, updated_at = :now
                        WHERE generation_id = :generation_id
                          AND lease_generation = :current_generation
                          AND (
                            (status IN ('pending', 'retry_wait') AND next_attempt_at <= :now)
                            OR (status = 'processing' AND (
                                lease_expires_at IS NULL OR lease_expires_at <= :now
                            ))
                          )
                          AND (:auto_generation_suspended = false OR trigger <> 'auto')
                        """
                    ),
                    {
                        "owner": worker_id,
                        "generation": generation,
                        "expires": expires,
                        "now": now,
                        "generation_id": row["generation_id"],
                        "current_generation": int(row["lease_generation"]),
                        "auto_generation_suspended": auto_generation_suspended,
                    },
                )
        except IntegrityError:
            # 同一用户被另一 Worker 先领取时只回滚本次 claim savepoint。
            continue
        if updated != 1:
            continue
        row.update(
            status="processing",
            lease_owner=worker_id,
            lease_generation=generation,
            lease_expires_at=expires,
            attempt_count=int(row["attempt_count"]) + 1,
        )
        claimed.append(row)
        seen_users.add(row["user_id"])
        if row["trigger"] == "auto":
            auto_claimed += 1
    return claimed


async def get_generation(session: AsyncSession, generation_id: UUID) -> dict[str, Any] | None:
    """读取单个 Generation Job。"""
    result = await session.execute(
        text(
            "SELECT * FROM conversation.knowledge_summary_generation_jobs "
            "WHERE generation_id = :generation_id"
        ),
        {"generation_id": generation_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def renew_generation_lease(
    session: AsyncSession,
    generation_id: UUID,
    *,
    worker_id: str,
    lease_generation: int,
    lease_seconds: int,
) -> bool:
    """续租 Generation Job，必须同时匹配 owner 与 lease_generation。"""
    return (
        await exec_rowcount(
            session,
            text(
                """
                UPDATE conversation.knowledge_summary_generation_jobs
                SET lease_expires_at = :expires, updated_at = :now
                WHERE generation_id = :generation_id AND status = 'processing'
                  AND lease_owner = :owner AND lease_generation = :lease_generation
                """
            ),
            {
                "generation_id": generation_id,
                "owner": worker_id,
                "lease_generation": lease_generation,
                "expires": datetime.now(UTC) + timedelta(seconds=lease_seconds),
                "now": datetime.now(UTC),
            },
        )
    ) == 1


async def update_generation_payload(
    session: AsyncSession,
    generation_id: UUID,
    *,
    worker_id: str,
    lease_generation: int,
    input_manifest: dict[str, Any] | None = None,
    extraction_result: dict[str, Any] | None = None,
    merge_plan_result: dict[str, Any] | None = None,
) -> bool:
    """保存冻结输入或已校验的结构化结果，所有字段更新均带 fencing。"""
    fields: list[str] = ["updated_at = :now"]
    params: dict[str, Any] = {
        "generation_id": generation_id,
        "owner": worker_id,
        "lease_generation": lease_generation,
        "now": datetime.now(UTC),
    }
    for name, value in (
        ("input_manifest", input_manifest),
        ("extraction_result", extraction_result),
        ("merge_plan_result", merge_plan_result),
    ):
        if value is not None:
            fields.append(f"{name} = CAST(:{name} AS jsonb)")
            params[name] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE conversation.knowledge_summary_generation_jobs SET "
                + ", ".join(fields)
                + " WHERE generation_id = :generation_id AND status = 'processing'"
                " AND lease_owner = :owner AND lease_generation = :lease_generation"
            ),
            params,
        )
    ) == 1


async def insert_model_call(
    session: AsyncSession,
    *,
    call_id: UUID,
    generation_id: UUID,
    purpose: str,
    model_name: str,
    prompt_version: str,
    schema_version: str,
    request_hash: str,
    response_payload: dict[str, Any] | None,
    input_tokens: int | None,
    output_tokens: int | None,
    latency_ms: int,
    status: str,
    error_code: str | None = None,
    attempt_no: int | None = None,
) -> None:
    """写入已脱敏模型调用审计；每次实际尝试都保留，原始响应和 Prompt 不入库。"""
    if attempt_no is None:
        result = await session.execute(
            text(
                """
                SELECT COALESCE(MAX(attempt_no), 0) + 1
                FROM conversation.knowledge_summary_model_calls
                WHERE generation_id = :generation_id AND purpose = :purpose
                """
            ),
            {"generation_id": generation_id, "purpose": purpose},
        )
        attempt_no = int(result.scalar_one())
    if attempt_no < 1:
        raise ValueError("模型调用 attempt_no 必须从 1 开始")
    await session.execute(
        text(
            """
            INSERT INTO conversation.knowledge_summary_model_calls (
                call_id, generation_id, purpose, attempt_no, model_name, prompt_version,
                schema_version, request_hash, response_payload, input_tokens, output_tokens,
                latency_ms, status, error_code
            ) VALUES (
                :call_id, :generation_id, :purpose, :attempt_no, :model_name, :prompt_version,
                :schema_version, :request_hash, CAST(:response_payload AS jsonb), :input_tokens,
                :output_tokens, :latency_ms, :status, :error_code
            )
            """
        ),
        {
            "call_id": call_id,
            "generation_id": generation_id,
            "purpose": purpose,
            "attempt_no": attempt_no,
            "model_name": model_name,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "request_hash": request_hash,
            "response_payload": json.dumps(response_payload, ensure_ascii=False)
            if response_payload is not None
            else None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
            "status": status,
            "error_code": error_code,
        },
    )
    metrics.knowledge_summary_model_calls_total.labels(
        purpose=purpose, result=status, model=model_name
    ).inc()
    if input_tokens is not None:
        metrics.knowledge_summary_model_tokens_total.labels(purpose=purpose, direction="input").inc(
            input_tokens
        )
    if output_tokens is not None:
        metrics.knowledge_summary_model_tokens_total.labels(
            purpose=purpose, direction="output"
        ).inc(output_tokens)


async def get_cached_model_call(
    session: AsyncSession, *, generation_id: UUID, purpose: str, request_hash: str
) -> dict[str, Any] | None:
    """读取同一 Job、用途和 request_hash 的成功缓存。"""
    result = await session.execute(
        text(
            """
            SELECT * FROM conversation.knowledge_summary_model_calls
            WHERE generation_id = :generation_id AND purpose = :purpose
              AND request_hash = :request_hash AND status = 'succeeded'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"generation_id": generation_id, "purpose": purpose, "request_hash": request_hash},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def count_failed_model_calls(
    session: AsyncSession, *, generation_id: UUID, purpose: str
) -> int:
    """统计同一 Job 某阶段已经记录的失败调用，用于单次业务校验重试门禁。"""
    result = await session.execute(
        text(
            "SELECT COUNT(*) FROM conversation.knowledge_summary_model_calls "
            "WHERE generation_id = :generation_id AND purpose = :purpose AND status = 'failed'"
        ),
        {"generation_id": generation_id, "purpose": purpose},
    )
    return int(result.scalar_one())


async def finish_generation(
    session: AsyncSession,
    generation_id: UUID,
    *,
    worker_id: str,
    lease_generation: int,
    status: str,
    affected_summary_ids: list[UUID] | None = None,
    warning_codes: list[str] | None = None,
    error_code: str | None = None,
) -> bool:
    """写入终态；0 行表示 lease 已失效，调用者不得继续写副作用。"""
    return (
        await exec_rowcount(
            session,
            text(
                """
                UPDATE conversation.knowledge_summary_generation_jobs
                SET status = :status, affected_summary_ids = :affected_summary_ids,
                    warning_codes = :warning_codes, last_error_code = :error_code,
                    lease_owner = NULL, lease_expires_at = NULL,
                    completed_at = now(), updated_at = now()
                WHERE generation_id = :generation_id AND status = 'processing'
                  AND lease_owner = :owner AND lease_generation = :lease_generation
                """
            ),
            {
                "generation_id": generation_id,
                "owner": worker_id,
                "lease_generation": lease_generation,
                "status": status,
                "affected_summary_ids": affected_summary_ids or [],
                "warning_codes": warning_codes or [],
                "error_code": error_code,
            },
        )
    ) == 1


async def retry_generation(
    session: AsyncSession,
    generation_id: UUID,
    *,
    worker_id: str,
    lease_generation: int,
    error_code: str,
    attempt_count: int,
    now: datetime | None = None,
    rng: Any = None,
) -> bool:
    """可重试失败进入 retry_wait，使用全抖动指数退避。"""
    now = now or datetime.now(UTC)
    import random as _random

    backoff = knowledge_summary_backoff_seconds(attempt_count, rng=rng or _random.Random())
    return (
        await exec_rowcount(
            session,
            text(
                """
                UPDATE conversation.knowledge_summary_generation_jobs
                SET status = 'retry_wait', next_attempt_at = :next_attempt_at,
                    last_error_code = :error_code, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = :now
                WHERE generation_id = :generation_id AND status = 'processing'
                  AND lease_owner = :owner AND lease_generation = :lease_generation
                """
            ),
            {
                "generation_id": generation_id,
                "owner": worker_id,
                "lease_generation": lease_generation,
                "next_attempt_at": now + timedelta(seconds=backoff),
                "error_code": error_code,
                "now": now,
            },
        )
    ) == 1


async def clear_merge_plan(
    session: AsyncSession,
    generation_id: UUID,
    *,
    worker_id: str,
    lease_generation: int,
) -> bool:
    """版本冲突时丢弃旧合并计划，保留 extraction 以便按最新目标重算。"""
    return (
        await exec_rowcount(
            session,
            text(
                """
                UPDATE conversation.knowledge_summary_generation_jobs
                SET merge_plan_result = NULL, updated_at = :now
                WHERE generation_id = :generation_id AND status = 'processing'
                  AND lease_owner = :owner AND lease_generation = :lease_generation
                """
            ),
            {
                "generation_id": generation_id,
                "owner": worker_id,
                "lease_generation": lease_generation,
                "now": datetime.now(UTC),
            },
        )
    ) == 1
