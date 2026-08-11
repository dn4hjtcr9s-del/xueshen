"""重试分类与退避（§11.1 / §11.2）。

退避公式：min(5 × 2^(attempt-1), 900) 秒 + 0～20% jitter；
Outbox 指数退避上限 30 分钟。jitter 通过注入随机源保证测试确定性。
"""

from __future__ import annotations

import random
from enum import Enum

from backend.memory.contracts.errors import (
    CandidateAlreadyReviewedError,
    CandidateNotFoundError,
    GraphNodeNotFoundError,
    GraphStateVersionConflictError,
    GraphStateVersionRequiredError,
    InvalidPayloadError,
    MemoryDeletedError,
    MemoryError,
    MemoryNotFoundError,
    MemoryRestoreExpiredError,
    MemoryVersionConflictError,
)

TASK_BACKOFF_BASE_SECONDS = 5
TASK_BACKOFF_CAP_SECONDS = 900
OUTBOX_BACKOFF_CAP_SECONDS = 1800
JITTER_RATIO = 0.2


class FailureAction(Enum):
    RETRY = "retry"
    NEEDS_REVIEW = "needs_review"
    DEAD_LETTER = "dead_letter"


def task_backoff_seconds(attempt: int, *, rng: random.Random) -> float:
    """任务级退避（§11.2）：attempt 从 1 开始。"""
    base = min(TASK_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), TASK_BACKOFF_CAP_SECONDS)
    return float(base) * (1 + rng.random() * JITTER_RATIO)


def outbox_backoff_seconds(attempt: int, *, rng: random.Random) -> float:
    """Outbox 退避（§14.4）：指数退避上限 30 分钟。"""
    base = min(TASK_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), OUTBOX_BACKOFF_CAP_SECONDS)
    return float(base) * (1 + rng.random() * JITTER_RATIO)


#: 可由用户处理的版本/语义冲突 → needs_review（§11.2/§11.3）
_NEEDS_REVIEW_ERRORS = (
    MemoryVersionConflictError,
    MemoryDeletedError,
    MemoryRestoreExpiredError,
    GraphStateVersionConflictError,
    GraphStateVersionRequiredError,
    CandidateAlreadyReviewedError,
)

#: 权限、目标不存在、非法状态转换 → 不重试（§11.1）
_PERMANENT_ERRORS = (
    InvalidPayloadError,
    MemoryNotFoundError,
    GraphNodeNotFoundError,
    CandidateNotFoundError,
)


def classify_failure(exc: BaseException) -> FailureAction:
    """异常 → 任务级处置（§11.1/§11.2）。"""
    if isinstance(exc, _NEEDS_REVIEW_ERRORS):
        return FailureAction.NEEDS_REVIEW
    if isinstance(exc, _PERMANENT_ERRORS):
        return FailureAction.DEAD_LETTER
    if isinstance(exc, MemoryError):
        return FailureAction.RETRY if exc.retryable else FailureAction.DEAD_LETTER
    # 未知异常按可重试处理，耗尽 max_attempts 后由执行层转 dead_letter
    return FailureAction.RETRY
