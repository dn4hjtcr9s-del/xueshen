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
from uuid import UUID, uuid4

from sqlalchemy import text

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
            except Exception:
                logger.info("本轮扫描已有并发执行，跳过")
                return 0
        candidates = (
            (
                await session.execute(
                    text(
                        """
                    SELECT p.plan_id, p.user_id,
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
                )
                if (
                    row["feed_run_id"] is not None
                    and row["status"] == "succeeded"
                    and row["input_hash"] == current_hash
                    and row["local_date"] == local_date
                ):
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
    if triggered:
        logger.info("daily feed scan 触发 %s 个计划", triggered)

    # Phase 4（§9.4）：每周复盘 replan（STUDY_AUTO_REPLAN_ENABLED 门控，
    # 每计划每 ISO 周幂等；重大调整仍生成 proposed revision 待确认）
    if settings.study_auto_replan_enabled:
        weekly = await _trigger_weekly_replan(db, settings, logger)
        if weekly:
            logger.info("weekly replan 触发 %s 个计划", weekly)
    return triggered


async def _trigger_weekly_replan(
    db: StudyDatabase, settings: Settings, logger: logging.Logger
) -> int:
    """每周复盘：每 active plan 每 ISO 周入队一次 replan operation（幂等）。"""
    from backend.study.persistence import repositories as repo

    now = datetime.now().astimezone()
    iso_year, iso_week, _ = now.isocalendar()
    triggered = 0
    async with db.session_factory() as session:
        plans = (
            (
                await session.execute(
                    text(
                        """
                    SELECT plan_id, user_id FROM study_plans
                    WHERE status = 'active' AND target_date >= current_date
                    """
                    )
                )
            )
            .mappings()
            .all()
        )
    for plan_row in plans:
        plan_id = UUID(str(plan_row["plan_id"]))
        key = f"weekly_replan:{plan_id}:{iso_year}:{iso_week}"
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
        except Exception:
            # 幂等键冲突 = 本周已触发；跳过
            continue
    return triggered


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
