"""conversation_jobs 仓储（方案 §7.7 / §1.5 R3 / §8.6）。

可靠任务：generate_title / summarize_thread / delete_thread。
- 标题与摘要按 job_type + thread_id + target_sequence 幂等；
- 删除按 job_type + thread_id + deletion_generation 幂等（部分唯一索引）；
- 全部采用与 Turn 相同的 lease/fencing 原则；
- delete_thread 等待时只更新 next_attempt_at，不递增 attempt_count（R3）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount
from backend.memory.worker.retry import task_backoff_seconds

_INSERT_SQL = text(
    """
    INSERT INTO conversation.conversation_jobs (
        job_id, job_type, thread_id, user_id, target_sequence, deletion_generation,
        status, attempt_count, next_attempt_at, lease_owner, lease_generation,
        lease_expires_at, last_error_code, created_at, updated_at
    ) VALUES (
        :job_id, :job_type, :thread_id, :user_id, :target_sequence, :deletion_generation,
        'pending', 0, :next_attempt_at, NULL, 0, NULL, NULL, :now, :now
    )
    ON CONFLICT ON CONSTRAINT conversation_jobs_job_type_thread_id_target_sequence_key
    DO NOTHING
    """
)

# P2（评审）：delete_thread 幂等键是部分唯一索引
# uq_conv_jobs_delete_generation (job_type, thread_id, deletion_generation) WHERE
# job_type='delete_thread' —— 普通唯一约束不覆盖它，重复插入会 IntegrityError。
_DELETE_INSERT_SQL = text(
    """
    INSERT INTO conversation.conversation_jobs (
        job_id, job_type, thread_id, user_id, target_sequence, deletion_generation,
        status, attempt_count, next_attempt_at, lease_owner, lease_generation,
        lease_expires_at, last_error_code, created_at, updated_at
    ) VALUES (
        :job_id, 'delete_thread', :thread_id, :user_id, NULL, :deletion_generation,
        'pending', 0, :next_attempt_at, NULL, 0, NULL, NULL, :now, :now
    )
    ON CONFLICT (job_type, thread_id, deletion_generation)
      WHERE job_type = 'delete_thread' AND deletion_generation IS NOT NULL
    DO NOTHING
    """
)


async def insert_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    job_type: str,
    thread_id: UUID,
    user_id: UUID,
    target_sequence: int | None = None,
    deletion_generation: int | None = None,
) -> bool:
    """插入 Job（幂等）；已存在返回 False。

    P2（评审）：delete_thread 走部分唯一索引的 ON CONFLICT 分支。
    """
    now = datetime.now(UTC)
    sql = _DELETE_INSERT_SQL if job_type == "delete_thread" else _INSERT_SQL
    return (
        await exec_rowcount(
            session,
            sql,
            {
                "job_id": job_id,
                "job_type": job_type,
                "thread_id": thread_id,
                "user_id": user_id,
                "target_sequence": target_sequence,
                "deletion_generation": deletion_generation,
                "next_attempt_at": now,
                "now": now,
            },
        )
    ) == 1


async def claim_jobs(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """批量 claim 可执行 Job（pending/retry_wait 且到点）。"""
    now = now or datetime.now(UTC)
    result = await session.execute(
        text(
            "SELECT * FROM conversation.conversation_jobs "
            "WHERE ("
            "  (status IN ('pending', 'retry_wait') AND next_attempt_at <= :now)"
            "  OR (status = 'processing' AND lease_expires_at IS NOT NULL"
            "      AND lease_expires_at < :now)"
            ") "
            "ORDER BY created_at LIMIT :limit FOR UPDATE SKIP LOCKED"
        ),
        {"now": now, "limit": limit},
    )
    rows = [dict(r) for r in result.mappings()]
    for row in rows:
        generation = int(row["lease_generation"]) + 1
        await session.execute(
            text(
                "UPDATE conversation.conversation_jobs "
                "SET status = 'processing', lease_owner = :owner, "
                "    lease_generation = :generation, lease_expires_at = :expires, "
                "    attempt_count = attempt_count + 1 "
                "WHERE job_id = :job_id AND lease_generation = :current"
            ),
            {
                "owner": worker_id,
                "generation": generation,
                "expires": now + timedelta(seconds=lease_seconds),
                "job_id": row["job_id"],
                "current": int(row["lease_generation"]),
            },
        )
        row["status"] = "processing"
        row["lease_owner"] = worker_id
        row["lease_generation"] = generation
        row["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
        row["attempt_count"] = int(row["attempt_count"]) + 1
    return rows


async def mark_job_done(session: AsyncSession, job_id: UUID, *, worker_id: str) -> bool:
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE conversation.conversation_jobs "
                "SET status = 'done', lease_owner = NULL, lease_expires_at = NULL, "
                "    updated_at = :now "
                "WHERE job_id = :job_id AND lease_owner = :owner AND status = 'processing'"
            ),
            {"job_id": job_id, "owner": worker_id, "now": datetime.now(UTC)},
        )
    ) == 1


async def mark_job_retry_wait(
    session: AsyncSession,
    job_id: UUID,
    *,
    worker_id: str,
    error_code: str,
    now: datetime | None = None,
    rng: Any = None,
) -> bool:
    """可重试失败：retry_wait + 任务退避（cap 900s，附录 A.1 同公式）。"""
    now = now or datetime.now(UTC)
    import random as _random

    rng = rng or _random.Random()
    row = await get_job(session, job_id)
    if row is None or row["lease_owner"] != worker_id:
        return False
    backoff = task_backoff_seconds(int(row["attempt_count"]), rng=rng)
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE conversation.conversation_jobs "
                "SET status = 'retry_wait', next_attempt_at = :next_attempt_at, "
                "    last_error_code = :error_code, lease_owner = NULL, lease_expires_at = NULL, "
                "    updated_at = :now "
                "WHERE job_id = :job_id AND lease_owner = :owner AND status = 'processing'"
            ),
            {
                "next_attempt_at": now + timedelta(seconds=backoff),
                "error_code": error_code,
                "job_id": job_id,
                "owner": worker_id,
                "now": now,
            },
        )
    ) == 1


async def mark_job_dead_letter(
    session: AsyncSession, job_id: UUID, *, worker_id: str, error_code: str
) -> bool:
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE conversation.conversation_jobs "
                "SET status = 'dead_letter', last_error_code = :error_code, "
                "    lease_owner = NULL, lease_expires_at = NULL, updated_at = :now "
                "WHERE job_id = :job_id AND lease_owner = :owner AND status = 'processing'"
            ),
            {
                "error_code": error_code,
                "job_id": job_id,
                "owner": worker_id,
                "now": datetime.now(UTC),
            },
        )
    ) == 1


async def wait_job(
    session: AsyncSession,
    job_id: UUID,
    *,
    worker_id: str,
    wait_seconds: int,
    error_code: str = "WAIT_PENDING_CONDITION",
) -> bool:
    """delete_thread 等待语义（R3）：不递增 attempt_count，只推迟 next_attempt_at。"""
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE conversation.conversation_jobs "
                "SET status = 'retry_wait', next_attempt_at = :next_attempt_at, "
                "    last_error_code = :error_code, lease_owner = NULL, lease_expires_at = NULL, "
                "    updated_at = :now "
                "WHERE job_id = :job_id AND lease_owner = :owner AND status = 'processing'"
            ),
            {
                "next_attempt_at": datetime.now(UTC) + timedelta(seconds=wait_seconds),
                "error_code": error_code,
                "job_id": job_id,
                "owner": worker_id,
                "now": datetime.now(UTC),
            },
        )
    ) == 1


async def get_job(session: AsyncSession, job_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM conversation.conversation_jobs WHERE job_id = :job_id"),
        {"job_id": job_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None
