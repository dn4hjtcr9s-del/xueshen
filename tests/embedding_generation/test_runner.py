"""Embedding Runner 测试：覆盖批处理、并发、重试、拆批、缓存和恢复。"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from scripts.embedding_generation.artifacts import ArtifactStore, load_chunk_dataset
from scripts.embedding_generation.client import EmbeddingRequestError
from scripts.embedding_generation.runner import EmbeddingRunner, RequestRateLimiter
from scripts.embedding_generation.schemas import ClientBatchResponse, UsageStats
from scripts.embedding_generation.settings import EmbeddingSettings
from tests.embedding_generation.helpers import write_chunk_root


def _settings(**overrides: object) -> EmbeddingSettings:
    values: dict[str, object] = {
        "base_url": "https://example.invalid/v1",
        "api_key": "secret",
        "dimensions": 3,
        "batch_size": 10,
        "concurrency": 2,
        "max_attempts": 3,
        "initial_backoff_seconds": 1.0,
        "max_backoff_seconds": 10.0,
        "jitter_seconds": 0.0,
    }
    values.update(overrides)
    return EmbeddingSettings(**values)  # type: ignore[arg-type]


def _store(
    tmp_path: Path,
    texts: list[str],
    settings: EmbeddingSettings,
    *,
    duplicate: bool = False,
) -> ArtifactStore:
    dataset = load_chunk_dataset(
        write_chunk_root(tmp_path / "chunks", texts, duplicate_first=duplicate)
    )
    return ArtifactStore.open(
        tmp_path / "embeddings",
        dataset=dataset,
        model=settings.model,
        dimensions=settings.dimensions,
        batch_size=settings.batch_size,
    )


class RecordingClient:
    """记录每次请求，并为所有文本返回确定性非零向量。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> ClientBatchResponse:
        self.calls.append(list(texts))
        return ClientBatchResponse(
            vectors=tuple((float(index + 1), 0.0, 0.0) for index, _ in enumerate(texts)),
            usage=UsageStats(prompt_tokens=len(texts) * 2, total_tokens=len(texts) * 2),
        )


def test_runner_uses_stable_batch_sizes_and_publishes_ready(tmp_path: Path) -> None:
    settings = _settings(batch_size=10, concurrency=2)
    store = _store(tmp_path, [f"text-{index}" for index in range(12)], settings)
    client = RecordingClient()

    summary = EmbeddingRunner(settings=settings, client=client, store=store).run()

    assert sorted(len(call) for call in client.calls) == [2, 10]
    assert summary.manifest["status"] == "ready"
    assert summary.completed_batches == 2
    assert summary.deferred_batches == ()


def test_runner_retries_with_retry_after_then_exponential_backoff(tmp_path: Path) -> None:
    settings = _settings(batch_size=1, concurrency=1)
    store = _store(tmp_path, ["text"], settings)
    sleeps: list[float] = []

    class RetryClient:
        def __init__(self) -> None:
            self.attempt = 0

        def embed(self, texts: Sequence[str]) -> ClientBatchResponse:
            self.attempt += 1
            if self.attempt == 1:
                raise EmbeddingRequestError(
                    code="http_429",
                    retryable=True,
                    retry_after=3.0,
                    message="rate limited",
                )
            if self.attempt == 2:
                raise EmbeddingRequestError(
                    code="http_503",
                    retryable=True,
                    message="unavailable",
                )
            return ClientBatchResponse(
                vectors=((1.0, 0.0, 0.0),),
                usage=UsageStats(prompt_tokens=2, total_tokens=2),
            )

    summary = EmbeddingRunner(
        settings=settings,
        client=RetryClient(),
        store=store,
        sleep=sleeps.append,
        random_value=lambda: 0.0,
    ).run()

    assert sleeps == [3.0, 2.0]
    assert summary.manifest["status"] == "ready"
    usage = json.loads((store.output_root / "usage.json").read_text())
    assert usage["request_count"] == 3
    assert usage["retry_count"] == 2


def test_runner_splits_permanent_batch_error_and_isolates_bad_input(tmp_path: Path) -> None:
    settings = _settings(batch_size=3, concurrency=1)
    store = _store(tmp_path, ["good-1", "bad", "good-2"], settings)

    class RejectBadClient(RecordingClient):
        def embed(self, texts: Sequence[str]) -> ClientBatchResponse:
            self.calls.append(list(texts))
            if "bad" in texts:
                raise EmbeddingRequestError(
                    code="http_400",
                    retryable=False,
                    message="bad input",
                )
            return ClientBatchResponse(
                vectors=tuple((1.0, 0.0, 0.0) for _ in texts),
                usage=UsageStats(prompt_tokens=len(texts), total_tokens=len(texts)),
            )

    client = RejectBadClient()
    summary = EmbeddingRunner(settings=settings, client=client, store=store).run()

    assert summary.manifest["status"] == "partial"
    assert summary.manifest["counts"]["successful_chunks"] == 2
    assert summary.manifest["counts"]["failed_chunks"] == 1
    assert len(client.calls) == 5
    failures = [
        json.loads(line) for line in (store.output_root / "failures.jsonl").read_text().splitlines()
    ]
    assert failures[0]["chunk_id"] == "chunk-1"
    assert failures[0]["error_code"] == "http_400"


def test_runner_deduplicates_cache_key_and_resume_skips_completed_batch(tmp_path: Path) -> None:
    settings = _settings(batch_size=10, concurrency=1)
    store = _store(tmp_path, ["same", "ignored"], settings, duplicate=True)
    first_client = RecordingClient()

    first = EmbeddingRunner(settings=settings, client=first_client, store=store).run()
    second_client = RecordingClient()
    second = EmbeddingRunner(settings=settings, client=second_client, store=store).run()

    assert len(first_client.calls) == 1
    assert len(first_client.calls[0]) == 1
    assert first.manifest["status"] == "ready"
    assert second.completed_batches == 0
    assert second_client.calls == []
    records = [
        json.loads(line)
        for line in (store.output_root / "embeddings.jsonl").read_text().splitlines()
    ]
    assert len(records) == 2
    assert records[1]["cached_from_chunk_id"] == "chunk-0"


def test_retry_failures_reexecutes_and_overwrites_failed_shard(tmp_path: Path) -> None:
    settings = _settings(batch_size=1, concurrency=1)
    store = _store(tmp_path, ["text"], settings)

    class PermanentClient:
        def embed(self, texts: Sequence[str]) -> ClientBatchResponse:
            raise EmbeddingRequestError(
                code="http_400",
                retryable=False,
                message="bad input",
            )

    first = EmbeddingRunner(settings=settings, client=PermanentClient(), store=store).run()
    success_client = RecordingClient()
    second = EmbeddingRunner(settings=settings, client=success_client, store=store).run(
        retry_failures=True
    )

    assert first.manifest["counts"]["failed_chunks"] == 1
    assert second.manifest["status"] == "ready"
    assert len(success_client.calls) == 1


def test_transient_exhaustion_leaves_batch_pending_for_resume(tmp_path: Path) -> None:
    settings = _settings(batch_size=1, concurrency=1, max_attempts=2)
    store = _store(tmp_path, ["text"], settings)

    class DownClient:
        def embed(self, texts: Sequence[str]) -> ClientBatchResponse:
            raise EmbeddingRequestError(
                code="http_503",
                retryable=True,
                message="down",
            )

    summary = EmbeddingRunner(
        settings=settings,
        client=DownClient(),
        store=store,
        sleep=lambda _seconds: None,
        random_value=lambda: 0.0,
    ).run()

    assert summary.deferred_batches == (0,)
    assert summary.manifest["counts"]["pending_chunks"] == 1
    assert not (store.parts_dir / "batch-000000.jsonl").exists()


def test_runner_never_exceeds_configured_concurrency(tmp_path: Path) -> None:
    settings = _settings(batch_size=1, concurrency=2)
    store = _store(tmp_path, [f"text-{index}" for index in range(6)], settings)

    class ConcurrentClient:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def embed(self, texts: Sequence[str]) -> ClientBatchResponse:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.02)
            with self.lock:
                self.active -= 1
            return ClientBatchResponse(
                vectors=((1.0, 0.0, 0.0),),
                usage=UsageStats(prompt_tokens=1, total_tokens=1),
            )

    client = ConcurrentClient()
    EmbeddingRunner(settings=settings, client=client, store=store).run()

    assert 1 < client.max_active <= 2


def test_request_rate_limiter_spaces_requests() -> None:
    current = [0.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return current[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        current[0] += seconds

    limiter = RequestRateLimiter(
        requests_per_second=2.0,
        monotonic=monotonic,
        sleep=sleep,
    )

    limiter.acquire()
    limiter.acquire()
    limiter.acquire()

    assert sleeps == [0.5, 0.5]
