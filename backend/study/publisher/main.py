"""Study Outbox Publisher 进程（§5.2/§14/§15.4，v1.2）。

- 向 Memory 等其他域可靠投递 Study 领域事件（§15.4：不做跨库事务，最终一致）；
- 活动映射（§14）：practice/assessment → exercise_attempt，
  review → review_result，learn/Session → check_in；
- 幂等键 = outbox idempotency_key，Memory 侧幂等去重；失败按
  attempt_count/max_attempts 退避重试，超上限 dead_letter 并告警；
- 未配置 MEMORY_API_BASE_URL / token 或 STUDY_MEMORY_WRITEBACK_ENABLED=false
  时进程直接退出（回写链路未获批准不启用，§19）。

启动：uv run python -m backend.study.publisher.main
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

from backend.settings import Settings, get_settings
from backend.study.persistence.database import StudyDatabase

_ACTIVITY_MAP = {
    "study.task_completed:learn": "check_in",
    "study.task_completed:practice": "exercise_attempt",
    "study.task_completed:assessment": "exercise_attempt",
    "study.task_completed:review": "review_result",
    "study.plan_activated": "check_in",
}


async def publish_once(db: StudyDatabase, settings: Settings, logger: logging.Logger) -> int:
    """领取一批 pending 事件并投递 Memory；返回投递成功数。"""
    if not settings.memory_api_base_url or not settings.memory_agent_token:
        logger.error("Memory 回写未配置 base_url/token，Publisher 退出")
        raise SystemExit(2)
    from backend.memory.client import MemoryClient

    client = MemoryClient(
        settings.memory_api_base_url,
        token=settings.memory_agent_token,
        timeout=30.0,
    )
    now = datetime.now(UTC)
    delivered = 0
    async with db.session_factory() as session:
        async with session.begin():
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT * FROM study_outbox
                        WHERE status = 'pending'
                          AND (available_at IS NULL OR available_at <= now())
                          AND (lease_expires_at IS NULL OR lease_expires_at < now())
                        ORDER BY created_at
                        LIMIT 50
                        FOR UPDATE SKIP LOCKED
                        """
                        )
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                await session.execute(
                    text(
                        """
                        UPDATE study_outbox SET status = 'delivering',
                            lease_owner = 'study-publisher',
                            lease_expires_at = :expires, attempt_count = attempt_count + 1
                        WHERE event_id = :event_id
                        """
                    ),
                    {
                        "expires": now + timedelta(seconds=settings.study_operation_lease_seconds),
                        "event_id": row["event_id"],
                    },
                )
        for raw in rows:
            event_row = dict(raw)
            event_id = UUID(str(event_row["event_id"]))
            try:
                await _deliver(client, event_row, logger)
                async with db.session_factory() as session:
                    async with session.begin():
                        await session.execute(
                            text(
                                "UPDATE study_outbox SET status = 'delivered', "
                                "delivered_at = now(), lease_owner = NULL, "
                                "lease_expires_at = NULL WHERE event_id = :event_id"
                            ),
                            {"event_id": event_id},
                        )
                delivered += 1
            except Exception as exc:
                logger.warning("outbox 投递失败 %s: %s", event_id, exc)
                async with db.session_factory() as session:
                    async with session.begin():
                        current = (
                            (
                                await session.execute(
                                    text(
                                        "SELECT attempt_count, max_attempts FROM study_outbox "
                                        "WHERE event_id = :event_id"
                                    ),
                                    {"event_id": event_id},
                                )
                            )
                            .mappings()
                            .first()
                        )
                        attempts = int(current["attempt_count"]) if current else 0
                        cap = (
                            int(current["max_attempts"])
                            if current and current["max_attempts"]
                            else 10
                        )
                        status = "dead_letter" if attempts >= cap else "pending"
                        backoff = min(2 ** max(attempts, 1), 300)
                        await session.execute(
                            text(
                                "UPDATE study_outbox SET status = :status, last_error = :err, "
                                "lease_owner = NULL, lease_expires_at = NULL, "
                                "available_at = now() + make_interval(secs => :backoff) "
                                "WHERE event_id = :event_id"
                            ),
                            {
                                "status": status,
                                "err": str(exc)[:500],
                                "event_id": event_id,
                                "backoff": backoff,
                            },
                        )
    return delivered


async def _deliver(client: Any, row: dict[str, Any], logger: logging.Logger) -> None:
    """按 §14 映射投递 Memory evidence。"""
    event_type = str(row["event_type"])
    payload = row["payload"] or {}
    if event_type == "study.plan_activated":
        await client.submit_activity_evidence(
            idempotency_key=str(row["idempotency_key"]),
            activity_type="check_in",
            activity_ids=[f"study:plan:{payload.get('plan_id', '')}"],
            content_ref=payload.get("summary"),
            topic_hints=[],
            graph_node_hints=[],
        )
        return
    if event_type == "study.task_completed":
        task_type = str(payload.get("task_type", "learn"))
        activity_type = _ACTIVITY_MAP.get(f"{event_type}:{task_type}", "check_in")
        await client.submit_activity_evidence(
            idempotency_key=str(row["idempotency_key"]),
            activity_type=activity_type,  # Memory 侧 Literal 已含映射值
            activity_ids=[f"study:task:{payload.get('task_id', '')}"],
            content_ref=payload.get("title"),
            topic_hints=[payload["topic_key"]] if payload.get("topic_key") else [],
            graph_node_hints=[payload["graph_node_id"]] if payload.get("graph_node_id") else [],
            window_started_at=payload.get("started_at"),
            window_ended_at=payload.get("completed_at"),
        )
        return
    raise ValueError(f"未知 Study outbox 事件类型: {event_type}")


async def run_forever(settings: Settings) -> None:
    logger = logging.getLogger("study.publisher")
    if not settings.study_memory_writeback_enabled:
        logger.info("STUDY_MEMORY_WRITEBACK_ENABLED=false，Publisher 不启动（§19）")
        return
    db = StudyDatabase(settings)
    logger.info("Study Outbox Publisher 启动")
    while True:
        try:
            delivered = await publish_once(db, settings, logger)
            if delivered:
                logger.info("outbox 投递成功 %s 条", delivered)
        except SystemExit:
            raise
        except Exception:
            logger.exception("outbox 投递循环失败")
        await asyncio.sleep(1.0)


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
