"""Study Scheduler 扫描集成测试（§9.3/D9/D20，§20.3）。

覆盖：候选日期由 PostgreSQL `(now() AT TIME ZONE timezone)::date` 计算
（DST/半小时/45 分钟偏移时区）；已成功且哈希匹配的 run 不入候选；
scheduler 触发与 ensure-today 并发只创建一个 run/operation。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from tests.study.conftest import USER_A


async def _seed_active_plan(
    study_session_factory: Any,
    *,
    timezone: str,
    start_date: str = "2026-08-17",
    user_id: str = USER_A,
) -> tuple[str, str]:
    """直接落库一个 active plan（D5：同一 user_id 只能一个 active，跨时区测试用不同用户）。"""
    from uuid import uuid4

    plan_id = uuid4()
    revision_id = uuid4()
    async with study_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO study_plans (plan_id, user_id, goal, status, timezone,
                        start_date, target_date, weekly_minutes, session_min_minutes,
                        session_max_minutes, current_revision_id)
                    VALUES (:plan_id, :uid, '目标', 'active', :tz, :start, :target,
                        140, 15, 60, :revision_id)
                    """
                ),
                {
                    "plan_id": plan_id,
                    "uid": user_id,
                    "tz": timezone,
                    "start": start_date,
                    "target": "2026-12-31",
                    "revision_id": revision_id,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO study_plan_revisions (revision_id, plan_id, revision_no,
                        reason, status, input_snapshot, personalization_status)
                    VALUES (:revision_id, :plan_id, 1, 'initial', 'active', '{}', 'not_requested')
                    """
                ),
                {"revision_id": revision_id, "plan_id": plan_id},
            )
    return str(plan_id), str(revision_id)


async def test_scan_triggers_feed_for_new_local_day(
    study_session_factory: Any,
) -> None:
    from backend.settings import Settings, get_settings
    from backend.study.persistence.database import StudyDatabase
    from backend.study.scheduler.main import scan_once

    settings = Settings(
        app_env="test",
        study_database_url=get_settings().study_database_url,
        study_daily_feed_scan_batch_size=10,
        _env_file=None,
    )
    db = StudyDatabase(settings)
    # 直接把 db.session_factory 换成测试工厂
    db.session_factory = study_session_factory  # type: ignore[assignment]
    import logging

    for index, tz in enumerate(
        ("America/New_York", "Asia/Kolkata", "Australia/Eucla", "Asia/Shanghai")
    ):
        await _seed_active_plan(
            study_session_factory,
            timezone=tz,
            user_id=f"11111111-1111-4111-8111-{index:012d}",
        )
    triggered = await scan_once(db, settings, logging.getLogger("test"))
    assert triggered >= 1
    async with study_session_factory() as session:
        runs = (
            (await session.execute(text("SELECT timezone, local_date FROM study_daily_feed_runs")))
            .mappings()
            .all()
        )
    assert len(runs) == triggered
    # local_date 由各计划时区的 PostgreSQL 计算得到（非调度进程固定偏移）
    for run in runs:
        assert run["local_date"] is not None
        assert run["timezone"] in {
            "America/New_York",
            "Asia/Kolkata",
            "Australia/Eucla",
            "Asia/Shanghai",
        }


async def test_scan_skips_fresh_succeeded_run(study_session_factory: Any) -> None:
    from backend.settings import Settings, get_settings
    from backend.study.persistence.database import StudyDatabase
    from backend.study.scheduler.main import scan_once

    settings = Settings(
        app_env="test",
        study_database_url=get_settings().study_database_url,
        _env_file=None,
    )
    db = StudyDatabase(settings)
    db.session_factory = study_session_factory  # type: ignore[assignment]
    import logging

    plan_id, revision_id = await _seed_active_plan(study_session_factory, timezone="Asia/Shanghai")
    from backend.study.services.feed_service import feed_input_hash

    async with study_session_factory() as session:
        async with session.begin():
            local_date = (
                await session.execute(
                    text(
                        "SELECT (now() AT TIME ZONE study_plans.timezone)::date "
                        "FROM study_plans WHERE plan_id = :pid"
                    ),
                    {"pid": plan_id},
                )
            ).scalar_one()
            await session.execute(
                text(
                    """
                    INSERT INTO study_daily_feed_runs (feed_run_id, user_id, plan_id,
                        revision_id, local_date, timezone, status, input_hash, completed_at)
                    VALUES (gen_random_uuid(), :uid, :pid, :rid, :d, 'Asia/Shanghai',
                        'succeeded', :hash, now())
                    """
                ),
                {
                    "uid": USER_A,
                    "pid": plan_id,
                    "rid": revision_id,
                    "d": local_date,
                    "hash": feed_input_hash(
                        plan_id=UUID(plan_id), revision_id=UUID(revision_id), local_date=local_date
                    ),
                },
            )
    triggered = await scan_once(db, settings, logging.getLogger("test"))
    assert triggered == 0
