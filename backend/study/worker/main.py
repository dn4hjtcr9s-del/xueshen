"""Study Worker 进程（§5.2/§9.2/D17，v1.2）。

- 消费 study_operations：claim（FOR UPDATE SKIP LOCKED + lease/generation）
  → 获取用户级 durable lease（D17 同用户串行）→ 执行 LangGraph
  （Plan Generation；Daily Feed/Replan 在 Phase 3/4 接入）→ 终态 + 释放 lease；
- 模型节点经 study_model_call_records 缓存防重放重复计费（§15.2）；
- 失败按 attempt_count/max_attempts 重试，超上限进入 failed。

启动：uv run python -m backend.study.worker.main
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

from backend.settings import Settings, get_settings
from backend.study.graph.builder import build_plan_generation_graph
from backend.study.persistence.database import StudyDatabase


async def _run_daily_feed(
    *,
    operation: dict[str, Any],
    session_factory: Any,
    graphs: dict[str, Any],
    worker_id: str,
    settings: Settings,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Daily Feed Graph（§9.3）：失败把 feed run 标记 failed，不虚构推荐（§16）。"""
    from backend.study.services import feed_service

    payload = operation["payload"] or {}
    state = {
        "user_id": str(operation["user_id"]),
        "operation_id": str(operation["operation_id"]),
        "feed_run_id": payload.get("feed_run_id", ""),
        "plan_id": payload.get("plan_id", ""),
        "revision_id": payload.get("revision_id"),
        "local_date": payload.get("local_date", ""),
        "timezone": payload.get("timezone", ""),
        "memory_context": None,
        "formal_items": [],
        "recommendation_items": [],
    }
    graph = graphs["daily_feed"]
    try:
        result = await graph.ainvoke(
            state,
            config={
                "configurable": {
                    "thread_id": str(operation["operation_id"]),
                    "worker_id": worker_id,
                }
            },
        )
    except Exception as exc:
        if payload.get("feed_run_id"):
            await feed_service.fail_feed_run(
                session_factory,
                feed_run_id=UUID(payload["feed_run_id"]),
                error_code=type(exc).__name__,
            )
        raise
    return {"feed_run_id": result.get("feed_run_id")}


async def _run_operation(
    *,
    operation: dict[str, Any],
    session_factory: Any,
    graphs: dict[str, Any],
    worker_id: str,
    settings: Settings,
    logger: logging.Logger,
) -> dict[str, Any]:
    """执行单个 operation（plan_generation / daily_feed_generation）。"""
    operation_type = str(operation["operation_type"])
    if operation_type == "daily_feed_generation":
        return await _run_daily_feed(
            operation=operation,
            session_factory=session_factory,
            graphs=graphs,
            worker_id=worker_id,
            settings=settings,
            logger=logger,
        )
    if operation_type == "replan":
        from backend.study.services.replan import run_replan_operation

        async with session_factory() as session:
            return await run_replan_operation(session, operation=operation, settings=settings)
    if operation_type != "plan_generation":
        raise ValueError(f"未知 operation 类型: {operation_type}")
    graph = graphs["plan_generation"]
    payload = operation["payload"] or {}
    intent = payload.get("intent") or {}
    # 去掉内部确认标记（intake confirm 写入）
    intent = {k: v for k, v in intent.items() if not k.startswith("_")}
    initial_state = {
        "user_id": str(operation["user_id"]),
        "operation_id": str(operation["operation_id"]),
        "intent": intent,
        "memory_context": None,
        "personalization_status": "not_requested",
        "personalization_reason": None,
        "blueprint": None,
        "plan_id": None,
        "revision_id": None,
        "error": None,
    }
    result = await graph.ainvoke(
        initial_state,
        config={
            "configurable": {"thread_id": str(operation["operation_id"]), "worker_id": worker_id}
        },
    )
    return {
        "plan_id": result.get("plan_id"),
        "revision_id": result.get("revision_id"),
    }


async def _claim_batch(
    session_factory: Any, *, worker_id: str, lease_seconds: int, batch_size: int
) -> list[dict[str, Any]]:
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                text(
                    """
                    SELECT * FROM study_operations
                    WHERE status = 'queued'
                      AND (lease_expires_at IS NULL OR lease_expires_at < now())
                    ORDER BY created_at
                    LIMIT :batch
                    FOR UPDATE SKIP LOCKED
                    """
                ),
                {"batch": batch_size},
            )
            rows = [dict(r) for r in result.mappings().all()]
            for row in rows:
                await session.execute(
                    text(
                        """
                        UPDATE study_operations
                        SET status = 'running', lease_owner = :owner,
                            lease_expires_at = :expires, lease_generation = lease_generation + 1,
                            attempt_count = attempt_count + 1, updated_at = now()
                        WHERE operation_id = :op_id
                        """
                    ),
                    {
                        "owner": worker_id,
                        "expires": datetime.now(UTC) + timedelta(seconds=lease_seconds),
                        "op_id": row["operation_id"],
                    },
                )
    return rows


async def _try_acquire_user_lease(
    session_factory: Any,
    *,
    user_id: UUID,
    operation_id: UUID,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    """D17：同用户串行的 durable user lease（过期自动接管）。"""
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                text(
                    """
                    INSERT INTO study_user_leases (user_id, operation_id, lease_generation,
                        locked_by, lease_expires_at)
                    VALUES (:user_id, :op_id, 1, :owner, :expires)
                    ON CONFLICT (user_id) DO UPDATE
                    SET operation_id = EXCLUDED.operation_id,
                        lease_generation = study_user_leases.lease_generation + 1,
                        locked_by = EXCLUDED.locked_by,
                        lease_expires_at = EXCLUDED.lease_expires_at,
                        updated_at = now()
                    WHERE study_user_leases.lease_expires_at < now()
                    """
                ),
                {
                    "user_id": user_id,
                    "op_id": operation_id,
                    "owner": worker_id,
                    "expires": datetime.now(UTC) + timedelta(seconds=lease_seconds),
                },
            )
            if result.rowcount == 0:
                return False
    return True


async def _release_user_lease(session_factory: Any, *, user_id: UUID, operation_id: UUID) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "DELETE FROM study_user_leases WHERE user_id = :user_id "
                    "AND operation_id = :op_id"
                ),
                {"user_id": user_id, "op_id": operation_id},
            )


async def _finish_operation(
    session_factory: Any,
    *,
    operation_id: UUID,
    status: str,
    result_payload: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    max_attempts: int = 10,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT attempt_count, max_attempts FROM study_operations "
                            "WHERE operation_id = :op_id"
                        ),
                        {"op_id": operation_id},
                    )
                )
                .mappings()
                .first()
            )
            attempts = int(row["attempt_count"]) if row else 0
            cap = int(row["max_attempts"]) if row and row["max_attempts"] else max_attempts
            if status == "failed" and attempts < cap:
                # 重试：回 queued，指数退避由 lease_expires_at 置空 + 下次 claim 处理
                await session.execute(
                    text(
                        "UPDATE study_operations SET status = 'queued', lease_owner = NULL, "
                        "lease_expires_at = NULL, error_code = :code, error_message = :msg, "
                        "updated_at = now() WHERE operation_id = :op_id"
                    ),
                    {"code": error_code, "msg": error_message, "op_id": operation_id},
                )
                return
            from backend.study.persistence import repositories as repo

            await repo.update_operation_status(
                session,
                operation_id=operation_id,
                expected_status=None,
                new_status=status,
                result_payload=result_payload,
                error_code=error_code,
                error_message=error_message,
            )


async def run_forever(settings: Settings) -> None:
    logger = logging.getLogger("study.worker")
    db = StudyDatabase(settings)
    worker_id = f"study-worker-{os.getpid()}"
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    url = settings.study_database_url
    if url.startswith("postgresql+psycopg://"):
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    separator = "&" if "?" in url else "?"
    saver_url = f"{url}{separator}options=-csearch_path%3D{settings.study_graph_checkpoint_schema}"

    from backend.study.gateways.memory import StudyMemoryGateway
    from backend.study.gateways.openai import StudyOpenAIGateway

    openai_gateway: StudyOpenAIGateway | None = None
    try:
        openai_gateway = StudyOpenAIGateway(settings=settings, logger=logger)
    except ValueError:
        logger.warning("未配置 OPENAI_API_KEY，plan_generation 将只能走降级模板")
    memory_gateway: StudyMemoryGateway | None = None
    if settings.study_memory_read_enabled and settings.memory_api_base_url:
        from backend.memory.client import MemoryClient

        memory_gateway = StudyMemoryGateway(
            client=MemoryClient(
                settings.memory_api_base_url,
                token=settings.memory_agent_token,
                timeout=settings.memory_context_timeout_seconds,
            )
        )

    from backend.study.graph.builder import build_daily_feed_graph

    graphs: dict[str, Any] = {}
    graphs["plan_generation"] = build_plan_generation_graph(
        settings=settings,
        session_factory=db.session_factory,
        openai_gateway=openai_gateway,
        memory_gateway=memory_gateway,
        logger=logger,
    )
    graphs["daily_feed"] = build_daily_feed_graph(
        settings=settings,
        session_factory=db.session_factory,
        openai_gateway=openai_gateway,
        memory_gateway=memory_gateway,
        logger=logger,
    )
    async with AsyncPostgresSaver.from_conn_string(saver_url) as saver:
        await saver.setup()
        for name in graphs:
            graphs[name] = graphs[name].with_config(checkpointer=saver)
        logger.info(
            "Study Worker 启动: %s concurrency=%s", worker_id, settings.study_worker_concurrency
        )
        while True:
            claimed = await _claim_batch(
                db.session_factory,
                worker_id=worker_id,
                lease_seconds=settings.study_operation_lease_seconds,
                batch_size=settings.study_worker_concurrency,
            )
            for operation in claimed:
                user_id = UUID(str(operation["user_id"]))
                operation_id = UUID(str(operation["operation_id"]))
                acquired = await _try_acquire_user_lease(
                    db.session_factory,
                    user_id=user_id,
                    operation_id=operation_id,
                    worker_id=worker_id,
                    lease_seconds=settings.study_operation_lease_seconds,
                )
                if not acquired:
                    # D17：该用户已有在途 operation，回队列让出
                    await _finish_operation(
                        db.session_factory,
                        operation_id=operation_id,
                        status="failed",
                        error_code="USER_LEASE_BUSY",
                        error_message="同用户已有在途 operation",
                    )
                    continue
                try:
                    result = await _run_operation(
                        operation=operation,
                        session_factory=db.session_factory,
                        graphs=graphs,
                        worker_id=worker_id,
                        settings=settings,
                        logger=logger,
                    )
                    await _finish_operation(
                        db.session_factory,
                        operation_id=operation_id,
                        status="succeeded",
                        result_payload=result,
                    )
                except Exception as exc:
                    logger.warning("operation %s 失败: %s", operation_id, exc)
                    await _finish_operation(
                        db.session_factory,
                        operation_id=operation_id,
                        status="failed",
                        error_code=type(exc).__name__,
                        error_message=str(exc)[:500],
                    )
                finally:
                    await _release_user_lease(
                        db.session_factory, user_id=user_id, operation_id=operation_id
                    )
            await asyncio.sleep(settings.study_worker_poll_seconds)


def main() -> None:
    settings = get_settings()
    from backend.memory.logging_config import configure_logging

    configure_logging(settings)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run_forever(settings))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
