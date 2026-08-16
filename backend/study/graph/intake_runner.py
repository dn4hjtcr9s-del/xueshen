"""Study Intake 同步 Runner（§9.1/D10/§12.1，v1.2）。

- POST /intakes/{id}/messages 在 API 请求内同步运行单轮抽取与追问，
  只允许一次快模型结构化调用，不创建 operation（D10）；
- 模型返回严格 IntakeExtraction；未知字段/缺失关键信息 → 继续追问，
  禁止模型自行补造（§8.7）；
- 8 轮/2,000 字符/24 小时上限（§7.1）；模型失败不改变 intake 状态
  （§12.1：只有 expires_at 到期才是 expired）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.settings import Settings
from backend.study.contracts.errors import (
    StudyIntakeExpiredError,
    StudyIntakeLimitExceededError,
    StudyPlanInputIncompleteError,
)
from backend.study.gateways.openai import StudyOpenAIGateway

PROMPT_VERSION = "intake-v1"

INTAKE_SYSTEM_PROMPT = """你是学习计划录入助手。从用户消息中提取学习目标与时间约束，
只输出结构化 JSON。规则：
1. 只把用户明确说过的信息写入 intent_patch，禁止猜测、补造任何字段；
2. 缺失的信息写入 missing_fields（可选值：goal、start_date、target_date、
   duration_weeks、timezone、weekly_availability、session_min_minutes、
   session_max_minutes）；
3. 每周可学习日使用 ISO 8601（1=周一，7=周日），可用分钟为整数；
4. 信息不完整时 clarifying_questions 给出 1–2 个最关键的中文追问，ready=false；
5. 全部信息齐备时 ready=true。"""


async def run_intake_turn(
    *,
    session: AsyncSession,
    settings: Settings,
    intake_row: dict[str, Any],
    message: str,
    gateway: StudyOpenAIGateway,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str, str]:
    """执行单轮 intake 抽取；返回 (更新后 intake_row, 回复文本, 状态)。"""
    now = now or datetime.now(UTC)
    status = str(intake_row["status"])
    if status in ("confirmed", "expired", "exhausted"):
        if status == "expired":
            raise StudyIntakeExpiredError("intake 已过期，请新建 intake")
        raise StudyIntakeLimitExceededError(f"intake 状态为 {status}，不能继续输入（§12.1）")
    if now >= intake_row["expires_at"]:
        raise StudyIntakeExpiredError("intake 已过期，请新建 intake")
    if len(message) > settings.study_intake_message_max_chars:
        raise StudyIntakeLimitExceededError(
            f"单条消息最多 {settings.study_intake_message_max_chars} 个字符（§7.1）"
        )
    if int(intake_row["message_count"]) >= settings.study_intake_max_messages:
        raise StudyIntakeLimitExceededError(
            f"intake 最多 {settings.study_intake_max_messages} 轮消息（§7.1）"
        )

    messages: list[dict[str, str]] = list(intake_row["recent_messages"] or [])
    messages.append({"role": "user", "content": message})

    from backend.study.contracts.graph import IntakeExtraction

    extraction = await gateway.structured_call(
        session=session,
        user_id=UUID(str(intake_row["user_id"])),
        operation_id=None,
        purpose="intake",
        prompt_version=PROMPT_VERSION,
        system_prompt=INTAKE_SYSTEM_PROMPT,
        user_payload={
            "known_intent": intake_row["normalized_intent"],
            "missing_fields": intake_row["missing_fields"],
            "dialogue": messages[-6:],
        },
        text_format=IntakeExtraction,
        cache_retention_days=settings.study_model_response_cache_retention_days,
        now=now,
    )

    normalized = dict(intake_row["normalized_intent"] or {})
    for key, value in extraction.intent_patch.items():
        if value is not None:
            normalized[key] = value
    missing = list(dict.fromkeys(extraction.missing_fields))
    ready = extraction.ready

    new_status = "ready" if ready else "collecting"
    if ready:
        try:
            _validate_complete_intent(normalized)
        except StudyPlanInputIncompleteError:
            ready = False
            new_status = "collecting"
            missing = missing or ["goal", "start_date", "timezone", "weekly_availability"]

    new_count = int(intake_row["message_count"]) + 1
    if new_status == "collecting" and new_count >= settings.study_intake_max_messages:
        new_status = "exhausted"

    await session.execute(
        text(
            """
            UPDATE study_plan_intakes
            SET normalized_intent = :normalized, missing_fields = :missing,
                recent_messages = :messages, message_count = :count,
                status = :status, version = version + 1, updated_at = now()
            WHERE intake_id = :intake_id AND version = :expected_version
            """
        ),
        {
            "normalized": __import__("json").dumps(normalized, ensure_ascii=False),
            "missing": __import__("json").dumps(missing, ensure_ascii=False),
            "messages": __import__("json").dumps(messages, ensure_ascii=False),
            "count": new_count,
            "status": new_status,
            "intake_id": intake_row["intake_id"],
            "expected_version": intake_row["version"],
        },
    )
    await session.commit()
    refreshed = await _get_intake_row(session, UUID(str(intake_row["intake_id"])))
    assert refreshed is not None

    if new_status == "exhausted":
        reply = "已达 8 轮上限，信息仍不完整。请新建一次计划录入（§12.1）。"
    elif ready:
        reply = "目标与时间信息已收集完整，请确认创建计划。"
    elif extraction.clarifying_questions:
        reply = "；".join(extraction.clarifying_questions)
    else:
        reply = "还缺少一些关键信息，请继续补充学习目标或时间安排。"
    return refreshed, reply, new_status


def _validate_complete_intent(normalized: dict[str, Any]) -> None:
    """完整意图预检（§8 约束 1–6；不通过 → needs_input，禁止补造）。"""
    from backend.study.contracts.api import PlanIntent

    try:
        PlanIntent.model_validate(normalized)
    except Exception as exc:
        raise StudyPlanInputIncompleteError(
            "结构化意图仍不完整：" + str(exc).split("\n")[0][:200]
        ) from exc


async def _get_intake_row(session: AsyncSession, intake_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM study_plan_intakes WHERE intake_id = :intake_id"),
        {"intake_id": intake_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def create_intake(
    session: AsyncSession, *, user_id: UUID, settings: Settings, now: datetime | None = None
) -> dict[str, Any]:
    """创建新 intake（§12.1：24 小时 TTL）。"""
    from uuid import uuid4

    now = now or datetime.now(UTC)
    intake_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO study_plan_intakes (intake_id, user_id, expires_at)
            VALUES (:intake_id, :user_id, :expires_at)
            """
        ),
        {
            "intake_id": intake_id,
            "user_id": user_id,
            "expires_at": now + timedelta(hours=settings.study_intake_ttl_hours),
        },
    )
    await session.commit()
    row = await _get_intake_row(session, intake_id)
    assert row is not None
    return row


async def confirm_intake(
    session: AsyncSession,
    *,
    intake_row: dict[str, Any],
    user_id: UUID,
) -> UUID:
    """confirm（§12.1：ready → confirmed + 创建 plan_generation operation）。

    幂等：confirmed 后重复 confirm 返回第一次的 operation_id（存于
    normalized_intent._confirmed_operation_id）。
    """
    from uuid import uuid4

    status = str(intake_row["status"])
    if status == "confirmed":
        op_id = (intake_row["normalized_intent"] or {}).get("_confirmed_operation_id")
        if op_id:
            return UUID(str(op_id))
        raise StudyIntakeLimitExceededError("intake 已确认但缺少 operation 记录")
    if status != "ready":
        raise StudyPlanInputIncompleteError(
            f"只有 ready 状态的 intake 可以确认，当前 {status}（§12.1）"
        )
    operation_id = uuid4()
    normalized = dict(intake_row["normalized_intent"] or {})
    normalized["_confirmed_operation_id"] = str(operation_id)
    # CAS on status='ready'：并发 confirm 只能有一个成功（评审必改 #3）
    result = await session.execute(
        text(
            """
            UPDATE study_plan_intakes
            SET status = 'confirmed', normalized_intent = :normalized,
                version = version + 1, updated_at = now()
            WHERE intake_id = :intake_id AND status = 'ready'
            """
        ),
        {
            "normalized": __import__("json").dumps(normalized, ensure_ascii=False),
            "intake_id": intake_row["intake_id"],
        },
    )
    if not getattr(result, "rowcount", 0):
        refreshed = await _get_intake_row(session, UUID(str(intake_row["intake_id"])))
        if refreshed is not None and refreshed["status"] == "confirmed":
            existing = (refreshed["normalized_intent"] or {}).get("_confirmed_operation_id")
            if existing:
                await session.commit()
                return UUID(str(existing))
        raise StudyPlanInputIncompleteError("intake 状态已变化，confirm 失败（请重试）")
    await repo_insert_operation(session, operation_id, user_id, normalized)
    await session.commit()
    return operation_id


async def repo_insert_operation(
    session: AsyncSession, operation_id: UUID, user_id: UUID, normalized: dict[str, Any]
) -> None:
    from backend.study.persistence import repositories as repo

    await repo.insert_operation(
        session,
        operation_id=operation_id,
        user_id=user_id,
        operation_type="plan_generation",
        payload={"intent": normalized, "source": "intake"},
    )
