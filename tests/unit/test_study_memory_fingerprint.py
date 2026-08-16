"""Study Memory 指纹稳定性单元测试（评审必改：指纹永不匹配抖动）。

覆盖 context_hash 稳定子集语义与 scheduler._memory_fingerprint_stale：
- query 回显 / 时间衰减 recommendations / token_usage / truncated 不参与哈希；
- learner/mastery/graph_states 变化才判 stale；
- 写入与校验使用同一 FEED_MEMORY_QUERY 常量。
"""

from __future__ import annotations

from typing import Any

from backend.study.gateways.memory import FEED_MEMORY_QUERY, context_hash
from backend.study.scheduler.main import _memory_fingerprint_stale


def _context(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "user_id": "11111111-1111-4111-8111-111111111111",
        "query": "任意 query（应被排除）",
        "learner": {
            "preferences": ["喜欢图形化讲解"],
            "goals": ["掌握线性代数"],
            "plans": ["六周计划"],
            "version": 1,
            "updated_at": "2026-08-16T00:00:00",
            "evidence_refs": [],
        },
        "mastery": [
            {
                "memory_id": "m1",
                "topic_key": "va",
                "title": "向量",
                "overview": "",
                "understood": [],
                "difficulties": [],
                "review_advice": [],
                "version": 1,
                "updated_at": "2026-08-16T00:00:00",
                "evidence_refs": [],
            }
        ],
        "graph_states": [
            {"node_id": "n1", "title": "向量", "status": "learning", "reason_codes": []}
        ],
        "recommendations": [{"score": 0.9, "reason": "时间衰减（应被排除）"}],
        "token_usage": {"budget": 3000, "estimated": 100, "remaining": 2900},
        "truncated": False,
    }
    base.update(overrides)
    return base


class TestContextHashStability:
    def test_unstable_fields_excluded(self) -> None:
        a = _context()
        b = _context(
            query="完全不同的 query",
            recommendations=[{"score": 0.1, "reason": "衰减后的另一个排序"}],
            token_usage={"budget": 3000, "estimated": 500, "remaining": 2500},
            truncated=True,
        )
        assert context_hash(a) == context_hash(b)

    def test_learner_change_changes_hash(self) -> None:
        a = _context()
        b = _context(
            learner={
                "preferences": ["喜欢代数推导"],
                "goals": ["掌握线性代数"],
                "plans": ["六周计划"],
                "version": 2,
                "updated_at": "2026-08-17T00:00:00",
                "evidence_refs": [],
            }
        )
        assert context_hash(a) != context_hash(b)

    def test_mastery_change_changes_hash(self) -> None:
        a = _context()
        b = _context(mastery=[])
        assert context_hash(a) != context_hash(b)

    def test_none_context_hashes_none(self) -> None:
        assert context_hash(None) is None


class FakeMemoryGateway:
    def __init__(self, context: dict[str, Any] | None, error: Exception | None = None) -> None:
        self._context = context
        self._error = error
        self.seen_queries: list[str] = []

    async def read_context(self, *, query: str, token_budget: int | None = None) -> Any:
        self.seen_queries.append(query)
        if self._error is not None:
            raise self._error
        if self._context is None:
            raise RuntimeError("no context")
        return self._context


class TestMemoryFingerprintStale:
    async def test_same_stable_subset_not_stale(self) -> None:
        context = _context()
        deterministic = "abc123"
        run_row = {"input_hash": f"{deterministic}:{context_hash(context)}"}
        gateway = FakeMemoryGateway(context)
        assert await _memory_fingerprint_stale(gateway, run_row, "目标") is False
        # 写入与校验使用同一常量 query
        assert gateway.seen_queries == [FEED_MEMORY_QUERY]

    async def test_query_echo_difference_not_stale(self) -> None:
        # 写入侧 query 与校验侧不同（旧行为会判 stale）——稳定子集哈希后不受影响
        context = _context(query="今日学习推荐")
        deterministic = "abc123"
        run_row = {"input_hash": f"{deterministic}:{context_hash(context)}"}
        gateway = FakeMemoryGateway(_context(query="别的 query"))
        assert await _memory_fingerprint_stale(gateway, run_row, "目标") is False

    async def test_mastery_change_is_stale(self) -> None:
        context = _context()
        deterministic = "abc123"
        run_row = {"input_hash": f"{deterministic}:{context_hash(context)}"}
        gateway = FakeMemoryGateway(_context(mastery=[]))
        assert await _memory_fingerprint_stale(gateway, run_row, "目标") is True

    async def test_gateway_none_not_stale(self) -> None:
        run_row = {"input_hash": "abc:hash"}
        assert await _memory_fingerprint_stale(None, run_row, "目标") is False

    async def test_gateway_error_not_stale(self) -> None:
        run_row = {"input_hash": "abc:hash"}
        gateway = FakeMemoryGateway(None, error=RuntimeError("memory down"))
        assert await _memory_fingerprint_stale(gateway, run_row, "目标") is False

    async def test_missing_input_hash_not_stale(self) -> None:
        gateway = FakeMemoryGateway(_context())
        assert await _memory_fingerprint_stale(gateway, {}, "目标") is False
