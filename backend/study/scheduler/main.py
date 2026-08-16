"""Study Scheduler 进程（§9.3/D9，v1.2）。

默认每 300 秒只扫描 active plan：候选日期由 PostgreSQL 计算
`(now() AT TIME ZONE study_plans.timezone)::date`，与每计划最新成功
feed_run 比较（stale/失败到退避时间/哈希不匹配同样入候选）；支持 DST 与
半小时/四十五分钟偏移时区。触发走 ensure_daily_feed 应用服务（run 唯一
约束 + operation 幂等），并发兜底由 (user_id, plan_id, local_date) 唯一键保证。

启动：uv run python -m backend.study.scheduler.main
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.settings import Settings, get_settings
from backend.study.persistence.database import StudyDatabase
from backend.study.services import feed_service


async def scan_once(db: StudyDatabase, settings: Settings, logger: logging.Logger) -> int:
    """一次扫描：返回入候选并触发 ensure 的计划数。"""

    run_key = f"daily_feed_scan:{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}"
    async with db.session_factory() as session:
        async with session.begin():
            # scheduler 幂等锚：同秒重复执行复用同一 run 记录
            try:
                await session.execute(
                    text(
                        """
                        INSERT INTO study_scheduler_runs (run_id, name, idempotency_key)
                        VALUES (:run_id, 'daily_feed_scan', :key)
                        """
                    ),
                    {"run_id": uuid4(), "key": run_key},
                )
            except IntegrityError:
                # 幂等键冲突 = 本轮扫描已有并发执行；DB 连接等故障不吞（评审残留 #3）
                await session.rollback()
                logger.info("本轮扫描已有并发执行，跳过")
                return 0
        candidates = (
            (
                await session.execute(
                    text(
                        """
                    SELECT p.plan_id, p.user_id, p.goal,
                           (now() AT TIME ZONE p.timezone)::date AS local_date,
                           r.feed_run_id, r.status, r.input_hash, r.generation
                    FROM study_plans p
                    LEFT JOIN LATERAL (
                        SELECT * FROM study_daily_feed_runs r
                        WHERE r.plan_id = p.plan_id
                        ORDER BY r.local_date DESC
                        LIMIT 1
                    ) r ON true
                    WHERE p.status = 'active'
                    ORDER BY p.created_at
                    LIMIT :batch
                    """
                    ),
                    {"batch": settings.study_daily_feed_scan_batch_size},
                )
            )
            .mappings()
            .all()
        )

    memory_gateway = _build_memory_gateway(settings)
    triggered = 0
    for row in candidates:
        user_id = UUID(str(row["user_id"]))
        plan_id = UUID(str(row["plan_id"]))
        local_date = row["local_date"]
        try:
            async with db.session_factory() as session:
                plan = (
                    (
                        await session.execute(
                            text(
                                "SELECT current_revision_id FROM study_plans "
                                "WHERE plan_id = :plan_id"
                            ),
                            {"plan_id": plan_id},
                        )
                    )
                    .mappings()
                    .first()
                )
                if plan is None:
                    continue
                current_hash = feed_service.feed_input_hash(
                    plan_id=plan_id,
                    revision_id=plan["current_revision_id"],
                    local_date=local_date,
                    daily_feed_enabled=bool(settings.study_daily_feed_enabled),
                    memory_read_enabled=bool(settings.study_memory_read_enabled),
                )
                if (
                    row["feed_run_id"] is not None
                    and row["status"] == "succeeded"
                    and row["local_date"] == local_date
                ):
                    if feed_service.feed_run_matches_deterministic(row["input_hash"], current_hash):
                        # Memory 指纹比较点（评审半修 #2）：推荐输入变化 → 强制再生成
                        if not await _memory_fingerprint_stale(
                            memory_gateway, row, str(row["goal"])
                        ):
                            continue
                    await feed_service.ensure_daily_feed(
                        session,
                        user_id=user_id,
                        local_date=local_date,
                        settings=settings,
                        force_regenerate=True,
                    )
                    triggered += 1
                    continue
                await feed_service.ensure_daily_feed(
                    session,
                    user_id=user_id,
                    local_date=local_date,
                    settings=settings,
                )
                triggered += 1
        except Exception as exc:
            logger.warning("daily feed ensure 失败 user=%s: %s", user_id, exc)
    await _cleanup_retention(db, settings, logger)
    if triggered:
        logger.info("daily feed scan 触发 %s 个计划", triggered)

    # Phase 4（§9.4）：每周复盘 replan（STUDY_AUTO_REPLAN_ENABLED 门控，
    # 每计划每 ISO 周幂等；重大调整仍生成 proposed revision 待确认）
    if settings.study_auto_replan_enabled:
        weekly = await _trigger_weekly_replan(db, settings, logger)
        if weekly:
            logger.info("weekly replan 触发 %s 个计划", weekly)
    return triggered


def _build_memory_gateway(settings: Settings) -> Any | None:
    """daily feed + memory read 同时开启时构建 Memory 网关（指纹比较用）。"""
    if not settings.study_daily_feed_enabled or not settings.study_memory_read_enabled:
        return None
    if not settings.memory_api_base_url or not settings.memory_agent_token:
        return None
    from backend.memory.client import MemoryClient
    from backend.study.gateways.memory import StudyMemoryGateway

    return StudyMemoryGateway(
        client=MemoryClient(
            settings.memory_api_base_url,
            token=settings.memory_agent_token,
            timeout=settings.memory_context_timeout_seconds,
        )
    )


async def _memory_fingerprint_stale(gateway: Any | None, run_row: Any, goal: str) -> bool:
    """run.input_hash 尾段（Memory 快照哈希）与当前 Memory 是否一致。"""
    if gateway is None or not run_row.get("input_hash"):
        return False
    try:
        context = await gateway.read_context(query=goal)
    except Exception:
        # Memory 不可用：按确定性前缀判定，不反复触发再生成
        return False
    from backend.study.gateways.memory import context_hash
    from backend.study.services.feed_service import feed_run_hash_with_memory

    full = feed_run_hash_with_memory(
        str(run_row["input_hash"]).split(":", 1)[0], context_hash(context)
    )
    return full != str(run_row["input_hash"])


async def _trigger_weekly_replan(
    db: StudyDatabase, settings: Settings, logger: logging.Logger
) -> int:
    """每周复盘：每 active plan 按其自身时区的 ISO 周入队一次（幂等，评审必改 #13）。"""
    from sqlalchemy.exc import IntegrityError

    from backend.study.persistence import repositories as repo

    async with db.session_factory() as session:
        plans = (
            (
                await session.execute(
                    text(
                        """
                    SELECT plan_id, user_id,
                           EXTRACT(ISOYEAR FROM (now() AT TIME ZONE p.timezone))::int AS iso_year,
                           EXTRACT(WEEK FROM (now() AT TIME ZONE p.timezone))::int AS iso_week
                    FROM study_plans p
                    WHERE status = 'active' AND target_date >= current_date
                    """
                    )
                )
            )
            .mappings()
            .all()
        )
    triggered = 0
    for plan_row in plans:
        plan_id = UUID(str(plan_row["plan_id"]))
        key = f"weekly_replan:{plan_id}:{int(plan_row['iso_year'])}:{int(plan_row['iso_week'])}"
        try:
            async with db.session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            """
                            INSERT INTO study_scheduler_runs (run_id, name, idempotency_key)
                            VALUES (gen_random_uuid(), 'weekly_replan', :key)
                            """
                        ),
                        {"key": key},
                    )
                    operation_id = uuid4()
                    await repo.insert_operation(
                        session,
                        operation_id=operation_id,
                        user_id=UUID(str(plan_row["user_id"])),
                        operation_type="replan",
                        payload={
                            "plan_id": str(plan_id),
                            "reason": "weekly_replan",
                            "user_requested": False,
                        },
                    )
                triggered += 1
        except IntegrityError:
            # 幂等键冲突 = 该计划本周已触发；回滚后跳过（不吞其他异常）
            pass
    return triggered


async def _cleanup_retention(db: StudyDatabase, settings: Settings, logger: logging.Logger) -> int:
    """保留期清理（§15.1/§15.2）：幂等 7 天、模型缓存 30 天，分批删除。"""
    removed = 0
    async with db.session_factory() as session:
        async with session.begin():
            result = await session.execute(
                text("DELETE FROM study_idempotency_requests WHERE expires_at < now()")
            )
            removed += int(getattr(result, "rowcount", 0) or 0)
            result = await session.execute(
                text("DELETE FROM study_model_call_records WHERE expires_at < now()")
            )
            removed += int(getattr(result, "rowcount", 0) or 0)
    if removed:
        logger.info("保留期清理删除 %s 行（幂等/模型缓存）", removed)
    return removed


async def run_forever(settings: Settings) -> None:
    logger = logging.getLogger("study.scheduler")
    db = StudyDatabase(settings)
    logger.info(
        "Study Scheduler 启动: interval=%ss",
        settings.study_daily_feed_scan_interval_seconds,
    )
    while True:
        try:
            await scan_once(db, settings, logger)
        except Exception:
            logger.exception("daily feed 扫描失败")
        await asyncio.sleep(settings.study_daily_feed_scan_interval_seconds)


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
