"""重试分类与退避边界单元测试（§11.1 / §11.2 / §23.1）。

退避公式：min(5 × 2^(attempt-1), 900) + 0～20% jitter；Outbox 上限 30 分钟。
jitter 通过注入固定 random() 值的随机源保证确定性。
"""

from __future__ import annotations

from backend.memory.contracts.errors import (
    CandidateAlreadyReviewedError,
    GraphNodeNotFoundError,
    GraphStateVersionConflictError,
    GraphStatusNotUserSettableError,
    InvalidPayloadError,
    MemoryNotFoundError,
    MemoryVersionConflictError,
    OpenAITimeoutError,
    StorageUnavailableError,
)
from backend.memory.worker.retry import (
    FailureAction,
    classify_failure,
    outbox_backoff_seconds,
    task_backoff_seconds,
)


class _FixedRng:
    """random() 返回固定值的随机源（jitter 边界测试）。"""

    def __init__(self, value: float) -> None:
        self._value = value

    def random(self) -> float:
        return self._value


class TestTaskBackoff:
    """§11.2：min(5 × 2^(attempt-1), 900) 秒 + 0～20% jitter。"""

    def test_base_sequence_without_jitter(self) -> None:
        rng = _FixedRng(0.0)
        assert task_backoff_seconds(1, rng=rng) == 5  # type: ignore[arg-type]
        assert task_backoff_seconds(2, rng=rng) == 10  # type: ignore[arg-type]
        assert task_backoff_seconds(3, rng=rng) == 20  # type: ignore[arg-type]
        assert task_backoff_seconds(8, rng=rng) == 640  # type: ignore[arg-type]

    def test_cap_at_900_seconds(self) -> None:
        rng = _FixedRng(0.0)
        # 5 × 2^8 = 1280 > 900，封顶
        assert task_backoff_seconds(9, rng=rng) == 900  # type: ignore[arg-type]
        assert task_backoff_seconds(20, rng=rng) == 900  # type: ignore[arg-type]

    def test_jitter_upper_bound(self) -> None:
        rng = _FixedRng(0.999999)
        assert task_backoff_seconds(1, rng=rng) < 5 * 1.2  # type: ignore[arg-type]
        assert task_backoff_seconds(1, rng=rng) >= 5  # type: ignore[arg-type]
        # 封顶后 jitter 在 900 基础上加 0～20%
        capped = task_backoff_seconds(9, rng=rng)  # type: ignore[arg-type]
        assert 900 <= capped < 900 * 1.2


class TestOutboxBackoff:
    """§14.4：指数退避上限 30 分钟（1800 秒）。"""

    def test_cap_at_1800_seconds(self) -> None:
        rng = _FixedRng(0.0)
        assert outbox_backoff_seconds(1, rng=rng) == 5  # type: ignore[arg-type]
        # 5 × 2^9 = 2560 > 1800，封顶
        assert outbox_backoff_seconds(10, rng=rng) == 1800  # type: ignore[arg-type]
        assert outbox_backoff_seconds(30, rng=rng) == 1800  # type: ignore[arg-type]

    def test_jitter_upper_bound(self) -> None:
        rng = _FixedRng(0.999999)
        value = outbox_backoff_seconds(10, rng=rng)  # type: ignore[arg-type]
        assert 1800 <= value < 1800 * 1.2


class TestClassifyFailure:
    """§11.1 / §11.2 / §11.3：永久错误 dead_letter、版本/语义冲突 needs_review。"""

    def test_version_conflicts_go_needs_review(self) -> None:
        assert classify_failure(MemoryVersionConflictError("冲突")) is FailureAction.NEEDS_REVIEW
        assert (
            classify_failure(GraphStateVersionConflictError("冲突")) is FailureAction.NEEDS_REVIEW
        )
        assert (
            classify_failure(CandidateAlreadyReviewedError("已审核")) is FailureAction.NEEDS_REVIEW
        )

    def test_permanent_errors_go_dead_letter(self) -> None:
        # 权限、目标不存在、非法状态转换不重试（§11.1）
        assert classify_failure(InvalidPayloadError("非法")) is FailureAction.DEAD_LETTER
        assert classify_failure(MemoryNotFoundError("不存在")) is FailureAction.DEAD_LETTER
        assert classify_failure(GraphNodeNotFoundError("不存在")) is FailureAction.DEAD_LETTER
        # 非 retryable 的 MemoryError 同样 dead_letter
        assert (
            classify_failure(GraphStatusNotUserSettableError("expert")) is FailureAction.DEAD_LETTER
        )

    def test_retryable_errors_retry(self) -> None:
        assert classify_failure(OpenAITimeoutError("超时")) is FailureAction.RETRY
        assert classify_failure(StorageUnavailableError("I/O")) is FailureAction.RETRY
        # 未知异常按可重试处理，耗尽 max_attempts 后由执行层转 dead_letter
        assert classify_failure(ValueError("未知")) is FailureAction.RETRY
