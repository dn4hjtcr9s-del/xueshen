"""Study 幂等键辅助（D16/§15.1，无模型 I/O，SQL 在 repositories）。

- key 必须是 1–200 个 ASCII 可见字符（0x21–0x7E）；
- 作用域 (user_id, operation_name, idempotency_key)；
- payload hash = 规范化 JSON 的 sha256；
- 同键同 payload → 返回首次记录；同键不同 payload → STUDY_IDEMPOTENCY_CONFLICT；
- 保留 7 天（STUDY_IDEMPOTENCY_RETENTION_DAYS），到期由 Scheduler 清理（Phase 3）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.study.contracts.errors import StudyIdempotencyConflictError
from backend.study.persistence import repositories as repo


def validate_idempotency_key(key: str) -> None:
    """D16：1–200 个 ASCII 可见字符。"""
    if not 1 <= len(key) <= 200:
        raise ValueError("Idempotency-Key 长度必须为 1–200 个字符")
    if not all(0x21 <= ord(ch) <= 0x7E for ch in key):
        raise ValueError("Idempotency-Key 只能是 ASCII 可见字符")


def request_hash(payload: Any) -> str:
    """规范化 JSON 的 sha256（D16：规范化请求体 + 资源 path 参数由调用方拼入）。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdempotencyOpen:
    """open_idempotent_request 结果。

    - replay=True：已有成功记录（含 response_status），直接重放；
    - replay=False：首次执行或上次尝试未完成（允许同键重试，§15.1）。
    """

    replay: bool
    response_status: int | None = None
    response_body: dict[str, Any] | None = None
    operation_id: str | None = None


async def open_idempotent_request(
    session: Any,
    *,
    user_id: Any,
    operation_name: str,
    idempotency_key: str,
    payload: Any,
    now: datetime,
    retention_days: int,
    operation_id: Any | None = None,
) -> IdempotencyOpen:
    """打开幂等窗口：已有记录且 payload 不一致 → 409；已完成 → 重放；
    否则（首次或上次失败）继续执行。并发同键由唯一约束兜底。
    """
    validate_idempotency_key(idempotency_key)
    existing = await repo.get_idempotency_row(
        session, user_id=user_id, operation_name=operation_name, idempotency_key=idempotency_key
    )
    if existing is not None:
        if existing["request_hash"] != request_hash(payload):
            raise StudyIdempotencyConflictError("同一幂等键对应不同的请求体（§15.1）")
        return IdempotencyOpen(
            replay=existing["response_status"] is not None,
            response_status=existing["response_status"],
            response_body=existing["response_body"],
            operation_id=str(existing["operation_id"]) if existing["operation_id"] else None,
        )
    inserted = await repo.insert_idempotency_row(
        session,
        idempotency_request_id=_new_uuid(),
        user_id=user_id,
        operation_name=operation_name,
        idempotency_key=idempotency_key,
        request_hash=request_hash(payload),
        expires_at=repo.idempotency_expiry(now, retention_days),
        operation_id=operation_id,
    )
    if inserted:
        return IdempotencyOpen(replay=False)
    # 并发同键：重查一次，按 payload 一致 replay / 不一致 409
    existing = await repo.get_idempotency_row(
        session, user_id=user_id, operation_name=operation_name, idempotency_key=idempotency_key
    )
    if existing is not None and existing["request_hash"] != request_hash(payload):
        raise StudyIdempotencyConflictError("同一幂等键对应不同的请求体（§15.1）")
    if existing is not None:
        return IdempotencyOpen(
            replay=existing["response_status"] is not None,
            response_status=existing["response_status"],
            response_body=existing["response_body"],
            operation_id=str(existing["operation_id"]) if existing["operation_id"] else None,
        )
    raise StudyIdempotencyConflictError("幂等请求并发冲突，请重试")


async def record_idempotent_result(
    session: Any,
    *,
    user_id: Any,
    operation_name: str,
    idempotency_key: str,
    response_status: int,
    response_body: dict[str, Any] | None,
    operation_id: Any | None = None,
) -> None:
    await repo.update_idempotency_result(
        session,
        user_id=user_id,
        operation_name=operation_name,
        idempotency_key=idempotency_key,
        response_status=response_status,
        response_body=response_body,
        operation_id=operation_id,
    )


def _new_uuid() -> Any:
    from uuid import uuid4

    return uuid4()
