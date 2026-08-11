"""Embedding 调度器：实现有界并发、限速、重试、拆批隔离和断点续传。"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from scripts.embedding_generation.artifacts import ArtifactStore
from scripts.embedding_generation.client import EmbeddingClient, EmbeddingRequestError
from scripts.embedding_generation.schemas import (
    ArtifactRecord,
    BatchOutcome,
    BatchPlan,
    ClientBatchResponse,
    EmbeddingJob,
    RunSummary,
    UsageStats,
)
from scripts.embedding_generation.settings import EmbeddingSettings


class RequestRateLimiter:
    """线程安全的最小请求间隔限制器；配置为零时完全关闭。"""

    def __init__(
        self,
        *,
        requests_per_second: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """等待到下一个允许的全局请求时刻。"""
        if self._interval == 0:
            return
        with self._lock:
            now = self._monotonic()
            wait_seconds = max(0.0, self._next_allowed - now)
            if wait_seconds > 0:
                self._sleep(wait_seconds)
                now = self._monotonic()
            self._next_allowed = max(now, self._next_allowed) + self._interval


@dataclass(frozen=True, slots=True)
class _ExecutionResult:
    records: tuple[ArtifactRecord, ...]
    usage: UsageStats
    request_count: int
    retry_count: int


class _PermanentRequestError(RuntimeError):
    def __init__(
        self,
        error: EmbeddingRequestError,
        *,
        request_count: int,
        retry_count: int,
    ) -> None:
        super().__init__(str(error))
        self.error = error
        self.request_count = request_count
        self.retry_count = retry_count


class _RetryExhaustedError(RuntimeError):
    def __init__(
        self,
        error: EmbeddingRequestError,
        *,
        request_count: int,
        retry_count: int,
    ) -> None:
        super().__init__(str(error))
        self.error = error
        self.request_count = request_count
        self.retry_count = retry_count


class _BatchDeferredError(RuntimeError):
    """瞬时错误耗尽重试；不写 shard，以便后续原样恢复。"""

    def __init__(self, batch_index: int, error: _RetryExhaustedError) -> None:
        super().__init__(str(error))
        self.batch_index = batch_index
        self.request_count = error.request_count
        self.retry_count = error.retry_count


class EmbeddingRunner:
    """执行 ArtifactStore 的未完成稳定批次，并逐批持久化完整结果。"""

    def __init__(
        self,
        *,
        settings: EmbeddingSettings,
        client: EmbeddingClient,
        store: ArtifactStore,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings
        self._client = client
        self._store = store
        self._sleep = sleep
        self._random_value = random_value
        self._rate_limiter = RequestRateLimiter(
            requests_per_second=settings.requests_per_second,
            monotonic=monotonic,
            sleep=sleep,
        )

    def _backoff(self, failed_attempt: int, error: EmbeddingRequestError) -> float:
        exponential = min(
            self._settings.max_backoff_seconds,
            self._settings.initial_backoff_seconds * (2 ** (failed_attempt - 1)),
        )
        jitter = self._settings.jitter_seconds * self._random_value()
        calculated = float(exponential + jitter)
        retry_after: float = error.retry_after if error.retry_after is not None else 0.0
        return calculated if calculated >= retry_after else retry_after

    def _request(self, jobs: Sequence[EmbeddingJob]) -> tuple[ClientBatchResponse, int, int]:
        request_count = 0
        retry_count = 0
        for attempt in range(1, self._settings.max_attempts + 1):
            self._rate_limiter.acquire()
            request_count += 1
            try:
                response = self._client.embed([job.embedding_text for job in jobs])
            except EmbeddingRequestError as exc:
                if not exc.retryable:
                    raise _PermanentRequestError(
                        exc,
                        request_count=request_count,
                        retry_count=retry_count,
                    ) from exc
                if attempt >= self._settings.max_attempts:
                    raise _RetryExhaustedError(
                        exc,
                        request_count=request_count,
                        retry_count=retry_count,
                    ) from exc
                retry_count += 1
                self._sleep(self._backoff(attempt, exc))
                continue
            return response, request_count, retry_count
        raise AssertionError("重试循环不应自然结束")

    @staticmethod
    def _merge(
        first: _ExecutionResult,
        second: _ExecutionResult,
        *,
        request_count: int = 0,
        retry_count: int = 0,
    ) -> _ExecutionResult:
        return _ExecutionResult(
            records=first.records + second.records,
            usage=first.usage + second.usage,
            request_count=request_count + first.request_count + second.request_count,
            retry_count=retry_count + first.retry_count + second.retry_count,
        )

    def _execute_jobs(self, jobs: Sequence[EmbeddingJob]) -> _ExecutionResult:
        try:
            response, request_count, retry_count = self._request(jobs)
        except _PermanentRequestError as exc:
            if len(jobs) == 1:
                failure_records = self._store.failure_records(
                    jobs[0],
                    error_code=exc.error.code,
                    error_message=str(exc.error),
                    attempts=exc.request_count,
                )
                return _ExecutionResult(
                    records=failure_records,
                    usage=UsageStats(),
                    request_count=exc.request_count,
                    retry_count=exc.retry_count,
                )
            midpoint = len(jobs) // 2
            first = self._execute_jobs(jobs[:midpoint])
            second = self._execute_jobs(jobs[midpoint:])
            return self._merge(
                first,
                second,
                request_count=exc.request_count,
                retry_count=exc.retry_count,
            )
        except _RetryExhaustedError:
            raise

        records: list[ArtifactRecord] = []
        for job, vector in zip(jobs, response.vectors, strict=True):
            records.extend(self._store.success_records(job, vector))
        return _ExecutionResult(
            records=tuple(records),
            usage=response.usage,
            request_count=request_count,
            retry_count=retry_count,
        )

    def _execute_batch(self, plan: BatchPlan) -> BatchOutcome:
        try:
            result = self._execute_jobs(plan.jobs)
        except _RetryExhaustedError as exc:
            raise _BatchDeferredError(plan.batch_index, exc) from exc
        return BatchOutcome(
            batch_index=plan.batch_index,
            batch_id=plan.batch_id,
            records=result.records,
            usage=result.usage,
            request_count=result.request_count,
            retry_count=result.retry_count,
            api_input_count=len(plan.jobs),
        )

    @staticmethod
    def _chunk_coverage(plan: BatchPlan) -> int:
        return sum(len(job.chunks) for job in plan.jobs)

    def _select_batches(self, pending: list[BatchPlan], limit: int | None) -> list[BatchPlan]:
        if limit is None:
            return pending
        if limit <= 0:
            raise ValueError("limit 必须是正整数")
        selected: list[BatchPlan] = []
        coverage = 0
        for plan in pending:
            if coverage >= limit:
                break
            selected.append(plan)
            coverage += self._chunk_coverage(plan)
        return selected

    def run(
        self,
        *,
        limit: int | None = None,
        retry_failures: bool = False,
    ) -> RunSummary:
        """运行本轮批次；瞬时失败保持 pending，其余完整批次立即写 shard。"""
        completed_before = self._store.load_completed_batches(
            retry_failures=retry_failures
        )
        pending = [
            plan for plan in self._store.batches if plan.batch_index not in completed_before
        ]
        selected = self._select_batches(pending, limit)
        completed_this_run = 0
        deferred: list[int] = []
        if selected:
            with ThreadPoolExecutor(max_workers=self._settings.concurrency) as executor:
                futures = {
                    executor.submit(self._execute_batch, plan): plan for plan in selected
                }
                for future in as_completed(futures):
                    plan = futures[future]
                    try:
                        outcome = future.result()
                    except _BatchDeferredError:
                        deferred.append(plan.batch_index)
                        continue
                    self._store.write_batch(outcome)
                    completed_this_run += 1
        manifest = self._store.publish_summary()
        return RunSummary(
            manifest=manifest,
            completed_batches=completed_this_run,
            deferred_batches=tuple(sorted(deferred)),
        )
