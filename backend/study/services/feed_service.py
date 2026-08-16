"""Study Daily Feed 领域服务（§7.7/§9.3/D9/D20，v1.2）。

- 业务幂等锚 study_daily_feed_runs UNIQUE(user_id, plan_id, local_date)：
  Scheduler、ensure-today 与 Worker 重放只能创建或复用同一 run（D20）；
- 当天 active plan/revision 或推荐输入变化 → 原 run 标记 stale、同一行
  generation+1 后重新生成；旧 active items 原子标记 expired；
- ensure_daily_feed 只创建 queued run/operation，模型调用发生在 Worker 的
  Daily Feed Graph（首页 GET 无副作用，§9.3/D9）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.study.contracts.errors import StudyNoActivePlanError
from backend.study.graph.builder import FEED_PROMPT_VERSION
from backend.study.persistence import repositories as repo

OP_DAILY_FEED = "daily_feed_generation"


async def ensure_daily_feed(
    session: AsyncSession,
    *,
    user_id: UUID,
    local_date: date,
    settings: Any,
) -> tuple[UUID, UUID | None]:
    """ensure-today / Scheduler 共用入口（§9.3：同一应用服务）。

    返回 (feed_run_id, operation_id)。已有成功 run 且 input_hash 仍匹配当前
    active revision → 复用（operation_id=None）；否则 stale + generation+1
    并创建新的 feed_generation operation（幂等：同 run 不允许第二个 operation）。
    """
    plan = await repo.get_active_plan_row(session, user_id=user_id)
    if plan is None:
        raise StudyNoActivePlanError("没有 active 计划（D22）")
    plan_id = UUID(str(plan["plan_id"]))
    timezone = str(plan["timezone"])
    revision_id = plan["current_revision_id"]
    current_hash = feed_input_hash(
        plan_id=plan_id,
        revision_id=revision_id,
        local_date=local_date,
        daily_feed_enabled=bool(settings.study_daily_feed_enabled),
        memory_read_enabled=bool(settings.study_memory_read_enabled),
    )

    existing = await _get_feed_run(session, user_id=user_id, plan_id=plan_id, local_date=local_date)
    if existing is not None:
        run_id = UUID(str(existing["feed_run_id"]))
        if existing["status"] == "succeeded" and feed_run_matches_deterministic(
            existing["input_hash"], current_hash
        ):
            return run_id, None
        if existing["status"] in ("queued", "running"):
            return run_id, existing["operation_id"]
        # 失败/stale → 同一行递增 generation 重试（§7.7）
        await session.execute(
            text(
                """
                UPDATE study_daily_feed_runs
                SET status = 'queued', generation = generation + 1,
                    attempt_count = attempt_count + 1, input_hash = :input_hash,
                    revision_id = :revision_id, updated_at = now(),
                    last_error_code = NULL, operation_id = NULL
                WHERE feed_run_id = :run_id
                """
            ),
            {
                "input_hash": current_hash,
                "revision_id": revision_id,
                "run_id": run_id,
            },
        )
    else:
        run_id = uuid4()
        # 并发 ensure/scheduler 由唯一键兜底：ON CONFLICT DO NOTHING 后重查复用
        await session.execute(
            text(
                """
                INSERT INTO study_daily_feed_runs (feed_run_id, user_id, plan_id,
                    revision_id, local_date, timezone, status, input_hash)
                VALUES (:run_id, :user_id, :plan_id, :revision_id, :local_date,
                    :timezone, 'queued', :input_hash)
                ON CONFLICT (user_id, plan_id, local_date) DO NOTHING
                """
            ),
            {
                "run_id": run_id,
                "user_id": user_id,
                "plan_id": plan_id,
                "revision_id": revision_id,
                "local_date": local_date,
                "timezone": timezone,
                "input_hash": current_hash,
            },
        )
        existing = await _get_feed_run(
            session, user_id=user_id, plan_id=plan_id, local_date=local_date
        )
        if existing is not None and UUID(str(existing["feed_run_id"])) != run_id:
            # 并发创建：复用对方 run
            if existing["status"] == "succeeded" and feed_run_matches_deterministic(
                existing["input_hash"], current_hash
            ):
                await session.commit()
                return UUID(str(existing["feed_run_id"])), None
            if existing["status"] in ("queued", "running") and existing["operation_id"]:
                await session.commit()
                return UUID(str(existing["feed_run_id"])), UUID(str(existing["operation_id"]))
            # 对方 run 失败/stale：由本轮继续在其上重试（generation+1）
            await session.execute(
                text(
                    """
                    UPDATE study_daily_feed_runs
                    SET status = 'queued', generation = generation + 1,
                        attempt_count = attempt_count + 1, input_hash = :input_hash,
                        revision_id = :revision_id, updated_at = now(),
                        last_error_code = NULL, operation_id = NULL
                    WHERE feed_run_id = :run_id
                    """
                ),
                {
                    "input_hash": current_hash,
                    "revision_id": revision_id,
                    "run_id": existing["feed_run_id"],
                },
            )
            run_id = UUID(str(existing["feed_run_id"]))

    operation_id = uuid4()
    await repo.insert_operation(
        session,
        operation_id=operation_id,
        user_id=user_id,
        operation_type=OP_DAILY_FEED,
        payload={
            "feed_run_id": str(run_id),
            "plan_id": str(plan_id),
            "revision_id": str(revision_id) if revision_id else None,
            "local_date": local_date.isoformat(),
            "timezone": timezone,
        },
    )
    await session.execute(
        text(
            "UPDATE study_daily_feed_runs SET operation_id = :operation_id "
            "WHERE feed_run_id = :run_id"
        ),
        {"operation_id": operation_id, "run_id": run_id},
    )
    await session.commit()
    return run_id, operation_id


async def persist_feed_result(
    session: AsyncSession,
    *,
    feed_run_id: UUID,
    items: list[dict[str, Any]],
    now: datetime,
    memory_context_hash: str | None = None,
) -> None:
    """Worker 落库 feed items（§7.8）：旧 active 原子 expired，插入新 items。

    input_hash 由确定性前缀 + Memory 快照哈希组成（§7.7 推荐输入指纹）。
    """
    await session.execute(
        text(
            """
            UPDATE study_daily_feed_items
            SET status = 'expired'
            WHERE feed_run_id = :run_id AND status = 'active'
            """
        ),
        {"run_id": feed_run_id},
    )
    for item in items:
        await session.execute(
            text(
                """
                INSERT INTO study_daily_feed_items (feed_item_id, feed_run_id, source_type,
                    task_id, topic_key, graph_node_id, title, reason, reason_codes,
                    estimated_minutes, launch_payload, expires_at)
                VALUES (:feed_item_id, :feed_run_id, :source_type, :task_id, :topic_key,
                    :graph_node_id, :title, :reason, :reason_codes, :estimated_minutes,
                    :launch_payload, :expires_at)
                """
            ),
            {
                "feed_item_id": uuid4(),
                "feed_run_id": feed_run_id,
                "source_type": item["source_type"],
                "task_id": item.get("task_id"),
                "topic_key": item.get("topic_key"),
                "graph_node_id": item.get("graph_node_id"),
                "title": item["title"],
                "reason": item.get("reason", ""),
                "reason_codes": _json(item.get("reason_codes", [])),
                "estimated_minutes": item.get("estimated_minutes"),
                "launch_payload": _json(item.get("launch_payload", {})),
                "expires_at": now.replace(hour=23, minute=59, second=59, microsecond=0),
            },
        )
    run = await _get_feed_run_by_id(session, feed_run_id=feed_run_id)
    if run is not None and run["input_hash"]:
        deterministic = str(run["input_hash"]).split(":", 1)[0]
        await session.execute(
            text(
                "UPDATE study_daily_feed_runs SET status = 'succeeded', completed_at = :now, "
                "input_hash = :full_hash, updated_at = :now WHERE feed_run_id = :run_id"
            ),
            {
                "now": now,
                "full_hash": feed_run_hash_with_memory(deterministic, memory_context_hash),
                "run_id": feed_run_id,
            },
        )
    else:
        await session.execute(
            text(
                "UPDATE study_daily_feed_runs SET status = 'succeeded', completed_at = :now, "
                "updated_at = :now WHERE feed_run_id = :run_id"
            ),
            {"now": now, "run_id": feed_run_id},
        )
    await session.commit()


async def _get_feed_run_by_id(session: AsyncSession, *, feed_run_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM study_daily_feed_runs WHERE feed_run_id = :run_id"),
        {"run_id": feed_run_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def fail_feed_run(session: AsyncSession, *, feed_run_id: UUID, error_code: str) -> None:
    await session.execute(
        text(
            "UPDATE study_daily_feed_runs SET status = 'failed', last_error_code = :code, "
            "completed_at = now(), updated_at = now() WHERE feed_run_id = :run_id"
        ),
        {"code": error_code, "run_id": feed_run_id},
    )
    await session.commit()


RECOMMENDATION_INPUT_VERSION = "v1"


def feed_input_hash(
    *,
    plan_id: UUID,
    revision_id: UUID | None,
    local_date: date,
    daily_feed_enabled: bool = False,
    memory_read_enabled: bool = False,
) -> str:
    """§12.6/§7.7：成功 run 复用判定用确定性输入哈希。

    包含推荐输入指纹（prompt 版本/推荐输入版本/开关），推荐生成逻辑变化时
    旧 run 自动 stale（评审必改 #7）。
    """
    from backend.study.services.idempotency import request_hash

    return request_hash(
        {
            "plan_id": str(plan_id),
            "revision_id": str(revision_id),
            "local_date": local_date.isoformat(),
            "feed_prompt_version": FEED_PROMPT_VERSION,
            "recommendation_input_version": RECOMMENDATION_INPUT_VERSION,
            "daily_feed_enabled": daily_feed_enabled,
            "memory_read_enabled": memory_read_enabled,
        }
    )


def feed_run_hash_with_memory(deterministic_hash: str, memory_context_hash: str | None) -> str:
    """持久化用完整指纹：确定性部分 + Memory 快照哈希（§7.7 推荐输入变化判定）。"""
    return f"{deterministic_hash}:{memory_context_hash or 'none'}"


def feed_run_matches_deterministic(run_input_hash: str | None, deterministic_hash: str) -> bool:
    """ensure/home 的 O(1) 比较：只比较确定性前缀（不读取 Memory）。"""
    if not run_input_hash:
        return False
    return run_input_hash.split(":", 1)[0] == deterministic_hash


async def _get_feed_run(
    session: AsyncSession, *, user_id: UUID, plan_id: UUID, local_date: date
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT * FROM study_daily_feed_runs WHERE user_id = :user_id "
            "AND plan_id = :plan_id AND local_date = :local_date"
        ),
        {"user_id": user_id, "plan_id": plan_id, "local_date": local_date},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
