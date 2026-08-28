"""知识总结 retention 维护服务（方案 §20.5）。

维护任务只操作 Conversation 知识总结表，不依赖用户读写/生成开关；每次运行使用独立小事务，
单批失败可重试，且不会因为一条坏数据删除父行。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation import metrics
from backend.conversation.persistence import knowledge_summary_retention as retention_repo

logger = logging.getLogger("conversation.services.knowledge_summary_retention")


async def run_retention_once(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """执行一次 scrub、已删除 summary 清理和 Generation 元数据清理。"""
    now = now or datetime.now(UTC)
    batch_size = int(settings.conversation_knowledge_summary_retention_batch_size)
    result = {
        "model_calls_scrubbed": 0,
        "jobs_scrubbed": 0,
        "summaries_deleted": 0,
        "jobs_deleted": 0,
    }

    async with session_factory() as session:
        async with session.begin():
            result["model_calls_scrubbed"] = await retention_repo.scrub_model_call_payloads(
                session,
                cutoff=now
                - timedelta(
                    days=int(
                        settings.conversation_knowledge_summary_model_call_payload_retention_days
                    )
                ),
                batch_size=batch_size,
            )
            result["jobs_scrubbed"] = await retention_repo.scrub_generation_payloads(
                session,
                cutoff=now
                - timedelta(
                    days=int(settings.conversation_knowledge_summary_job_payload_retention_days)
                ),
                batch_size=batch_size,
            )

    # 父行删除单独成事务；任一 FK/锁异常只影响当前 summary，不回滚已完成 scrub。
    async with session_factory() as session:
        async with session.begin():
            summary_ids = await retention_repo.list_deleted_summary_ids(
                session,
                cutoff=now
                - timedelta(
                    days=int(settings.conversation_knowledge_summary_deleted_summary_retention_days)
                ),
                batch_size=batch_size,
            )
            for summary_id in summary_ids:
                try:
                    # 单行删除使用 savepoint，FK/锁异常不会污染同批的外层事务。
                    async with session.begin_nested():
                        result["summaries_deleted"] += await retention_repo.delete_summary_parent(
                            session, summary_id=summary_id
                        )
                except Exception:
                    metrics.knowledge_summary_retention_operations_total.labels(
                        operation="delete_summaries", result="failure"
                    ).inc()
                    logger.exception(
                        "知识总结物理删除失败: summary_id_hash=%s", str(summary_id)[:8]
                    )

    async with session_factory() as session:
        async with session.begin():
            generation_ids = await retention_repo.list_expired_generation_ids(
                session,
                cutoff=now
                - timedelta(
                    days=int(settings.conversation_knowledge_summary_generation_retention_days)
                ),
                batch_size=batch_size,
            )
            for generation_id in generation_ids:
                try:
                    # 与 summary 删除相同：单条 Generation 失败不回滚整批。
                    async with session.begin_nested():
                        result["jobs_deleted"] += await retention_repo.delete_generation_parent(
                            session, generation_id=generation_id
                        )
                except Exception:
                    metrics.knowledge_summary_retention_operations_total.labels(
                        operation="delete_generations", result="failure"
                    ).inc()
                    logger.exception(
                        "Generation 物理删除失败: generation_id_hash=%s", str(generation_id)[:8]
                    )

    for operation, count in (
        ("scrub_model_calls", result["model_calls_scrubbed"]),
        ("scrub_jobs", result["jobs_scrubbed"]),
        ("delete_summaries", result["summaries_deleted"]),
        ("delete_generations", result["jobs_deleted"]),
    ):
        metrics.knowledge_summary_retention_operations_total.labels(
            operation=operation, result="success"
        ).inc(count)
    return result
