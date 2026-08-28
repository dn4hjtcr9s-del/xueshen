"""知识总结 Phase 3 的纯单元测试（知识总结方案 §10、§19.3、§22.2）。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from backend.conversation.contracts.knowledge_summary import (
    CandidateItem,
    CreateSummaryPlan,
    KnowledgeCandidate,
    KnowledgeExtractionResult,
    KnowledgeMergePlanResult,
    SourceSupport,
)
from backend.conversation.gateways.knowledge_summary_openai import (
    EXTRACT_PROMPT_VERSION,
    EXTRACT_SCHEMA_VERSION,
    KnowledgeSummaryOpenAIGateway,
    build_request_hash,
)


class FakeResponses:
    """只返回 SDK parse 后的 Pydantic 对象，验证 Gateway 不解析自由文本。"""

    def __init__(self, parsed: Any) -> None:
        self.parsed = parsed
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_parsed=self.parsed,
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )


class FakeSettings:
    openai_api_key = "test-key"
    openai_base_url = None
    openai_knowledge_summary_model = "model-v1"
    conversation_knowledge_summary_structured_output_models = "model-v1"
    conversation_knowledge_summary_model_timeout_seconds = 30
    conversation_knowledge_summary_extract_max_output_tokens = 6000
    conversation_knowledge_summary_merge_max_output_tokens = 6000
    conversation_knowledge_summary_sdk_max_retries = 0

    @property
    def knowledge_summary_structured_output_model_allowlist(self) -> frozenset[str]:
        return frozenset({"model-v1"})


@pytest.mark.asyncio
async def test_gateway_uses_responses_parse_and_frozen_parameters() -> None:
    message_id = uuid4()
    result = KnowledgeExtractionResult(
        candidates=[
            KnowledgeCandidate(
                scope="math",
                topic_group_title="圆锥曲线",
                topic_title="椭圆离心率",
                confidence=0.9,
                reusable_value="save",
                items=[
                    CandidateItem(
                        section="formulas",
                        text="e=c/a。",
                        confidence=0.9,
                        supports=[SourceSupport(message_id=message_id, quote="e=c/a。")],
                    )
                ],
            )
        ]
    )
    responses = FakeResponses(result)
    gateway = KnowledgeSummaryOpenAIGateway(
        settings=cast(Any, FakeSettings()),
        client=SimpleNamespace(responses=responses),
    )

    parsed, usage = await gateway.extract({"input_manifest": {"input_hash": "a"}})

    assert parsed == result
    assert usage["input_tokens"] == 11
    assert usage["output_tokens"] == 7
    assert int(usage["latency_ms"] or 0) >= 0
    assert len(responses.calls) == 1
    call = responses.calls[0]
    assert call["text_format"] is KnowledgeExtractionResult
    assert call["max_output_tokens"] == 6000
    assert call["timeout"] == 30
    assert call["temperature"] == 0
    assert str(call["instructions"]).startswith("# knowledge_extract_v1")


def test_request_hash_sorts_existing_summaries_and_changes_with_request() -> None:
    first = uuid4()
    second = uuid4()

    def make_hash(existing: list[dict[str, Any]], request: dict[str, Any]) -> str:
        return build_request_hash(
            model="model-v1",
            purpose="extract",
            prompt_version=EXTRACT_PROMPT_VERSION,
            schema_version=EXTRACT_SCHEMA_VERSION,
            input_manifest_hash="input",
            existing_summaries=existing,
            request=request,
        )

    left = make_hash(
        [
            {"summary_id": str(second), "version": 1, "state_hash": "b"},
            {"summary_id": str(first), "version": 2, "state_hash": "a"},
        ],
        {"messages": ["a"]},
    )
    right = make_hash(
        [
            {"summary_id": str(first), "version": 2, "state_hash": "a"},
            {"summary_id": str(second), "version": 1, "state_hash": "b"},
        ],
        {"messages": ["a"]},
    )
    changed = make_hash([], {"messages": ["a"]})
    assert left == right
    assert left != changed


@pytest.mark.asyncio
async def test_ambiguous_tombstone_finishes_as_no_change_without_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模糊墓碑只能阻止自动更新，不能产出没有 review 对象的 needs_review。"""
    from backend.conversation.services import knowledge_summary_generation as generation_module
    from backend.conversation.services.knowledge_summary_generation import (
        FrozenInput,
        KnowledgeSummaryGenerationService,
    )

    user_id = uuid4()
    thread_id = uuid4()
    turn_id = uuid4()
    generation_id = uuid4()
    occurred_at = datetime(2026, 8, 20, tzinfo=UTC)
    candidate = KnowledgeCandidate(
        scope="math",
        topic_group_title="圆锥曲线",
        topic_title="椭圆离心率",
        confidence=0.95,
        reusable_value="save",
        items=[
            CandidateItem(
                section="formulas",
                text="e=c/a。",
                confidence=0.95,
                supports=[SourceSupport(message_id=uuid4(), quote="e=c/a。")],
            )
        ],
    )
    plan = KnowledgeMergePlanResult(
        plans=[
            CreateSummaryPlan(
                candidate_index=0,
                match_confidence=0.1,
                reason="无现有总结",
            )
        ]
    )

    class _Transaction:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Session:
        def begin(self) -> _Transaction:
            return _Transaction()

    class _SessionContext:
        async def __aenter__(self) -> _Session:
            return _Session()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _SessionFactory:
        def __call__(self) -> _SessionContext:
            return _SessionContext()

    finish_calls: list[dict[str, object]] = []

    async def fake_lock(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {}

    async def fake_source_rows(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "thread_status": "active",
            "turn_status": "completed",
            "user_status": "completed",
            "assistant_status": "completed",
            "assistant_eligible_for_context": True,
            "source_checkpoint_id": "checkpoint",
            "assistant_role": "assistant",
            "user_role": "user",
            "user_occurred_at": occurred_at,
        }

    async def fake_tombstone(*_args: object, **_kwargs: object) -> str:
        return "ambiguous"

    async def fake_finish(*_args: object, **kwargs: object) -> bool:
        finish_calls.append(kwargs)
        return True

    monkeypatch.setattr(generation_module, "_lock_fenced_generation", fake_lock)
    monkeypatch.setattr(generation_module, "_candidate_tombstone_suppressed", fake_tombstone)
    monkeypatch.setattr(
        generation_module.summaries_repo, "get_generation_source_rows", fake_source_rows
    )

    async def fake_lock_rows(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(generation_module.summaries_repo, "lock_summary_rows", fake_lock_rows)
    monkeypatch.setattr(generation_module.generations_repo, "finish_generation", fake_finish)

    service = KnowledgeSummaryGenerationService(
        session_factory=cast(Any, _SessionFactory()),
        config=SimpleNamespace(),
        gateway=cast(Any, object()),
        token_counter=cast(Any, object()),
        worker_id="unit-worker",
    )
    await service._commit(
        {
            "generation_id": generation_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "user_id": user_id,
            "source_checkpoint_id": "checkpoint",
            "primary_turn_occurred_at": occurred_at,
            "lease_generation": 1,
            "input_manifest": None,
        },
        frozen=FrozenInput(manifest={}, messages={}, conversation_summary=None),
        candidates=[candidate],
        contexts=[],
        merge_plan=plan,
        warning_codes=[],
    )

    assert finish_calls == [
        {
            "worker_id": "unit-worker",
            "lease_generation": 1,
            "status": "no_change",
            "affected_summary_ids": [],
            "warning_codes": ["AMBIGUOUS_DELETED_TOPIC"],
        }
    ]


def test_manual_retry_idempotency_rejects_force_true() -> None:
    """同一 client_request_id 的 manual_retry 只能匹配 force=false 请求。"""
    from backend.conversation.services.knowledge_summary_generation_api import (
        KnowledgeSummaryGenerationApiService,
    )
    from backend.shared.ratelimit import FixedWindowRateLimiter

    user_id, thread_id, turn_id = uuid4(), uuid4(), uuid4()
    service = KnowledgeSummaryGenerationApiService(
        session_factory=cast(Any, None),
        settings=cast(Any, SimpleNamespace()),
        rate_limiter=FixedWindowRateLimiter(),
    )
    job = {
        "trigger": "manual_retry",
        "user_id": user_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "source_checkpoint_id": "checkpoint",
    }

    assert service._same_manual_request(
        job,
        user_id=user_id,
        thread_id=thread_id,
        turn_id=turn_id,
        source_checkpoint_id="checkpoint",
        force=False,
    )
    assert not service._same_manual_request(
        job,
        user_id=user_id,
        thread_id=thread_id,
        turn_id=turn_id,
        source_checkpoint_id="checkpoint",
        force=True,
    )
