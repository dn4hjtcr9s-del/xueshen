"""LLM 边界单元测试：Schema / 策略阈值 / 预算 / Fake Client / Prompt / 评测样例（§9）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from backend.memory.contracts.errors import (
    OpenAISchemaInvalidError,
    OpenAITimeoutError,
)
from backend.memory.graph.llm_schemas import (
    CandidateExtractionResult,
    CandidateMemory,
    ExtractedEvidence,
    MutationPlanDraft,
    MutationPlanResult,
)
from backend.memory.graph.openai_client import FakeMemoryLLMClient
from backend.memory.graph.policies import (
    LLM_MAX_CALLS_PER_ATTEMPT,
    LLM_MAX_CALLS_PER_OPERATION,
    LLMBudgetExceededError,
    LLMCallBudget,
    classify_candidate,
    classify_topic_similarity,
    mapping_candidate_accepted,
)
from backend.memory.graph.prompt_loader import (
    BUILD_MUTATION_PLAN_PROMPT_VERSION,
    EXTRACT_CANDIDATES_PROMPT_VERSION,
    load_prompt,
)
from backend.settings import Settings

S = Settings(app_env="test")


def _evidence(ref: str = "m1") -> ExtractedEvidence:
    return ExtractedEvidence(
        evidence_ref=ref, evidence_type="user_solution", summary="用户独立解答", strength=0.9
    )


def _candidate(**overrides: object) -> CandidateMemory:
    base = {
        "memory_type": "mastery",
        "topic_title": "二次函数",
        "category": "understanding",
        "summary": "用户能独立配方",
        "long_term_value": "save",
        "confidence": 0.9,
        "evidence": [_evidence()],
    }
    base.update(overrides)
    return CandidateMemory.model_validate(base)


def test_extraction_schema_roundtrip_and_forbid_extra() -> None:
    result = CandidateExtractionResult(
        candidates=[_candidate()], ignored_reason_codes=["NO_LONG_TERM_VALUE"]
    )
    assert result.candidates[0].category == "understanding"
    with pytest.raises(ValidationError):
        CandidateExtractionResult.model_validate({"candidates": [], "user_id": "注入禁止字段"})


def test_mutation_plan_draft_constraints() -> None:
    draft = MutationPlanDraft(
        target_memory_type="mastery",
        topic_title="二次函数",
        action="merge",
        candidate_indexes=[0, 2],
        reasoning_summary="增量合并证据",
    )
    assert draft.action == "merge"
    with pytest.raises(ValidationError):
        MutationPlanResult(plans=[draft] * 9)  # max 8


def test_classify_candidate_boundaries() -> None:
    assert classify_candidate(long_term_value="save", confidence=0.80, settings=S) == "auto_save"
    assert classify_candidate(long_term_value="save", confidence=0.79, settings=S) == "review"
    assert classify_candidate(long_term_value="review", confidence=0.95, settings=S) == "review"
    assert classify_candidate(long_term_value="save", confidence=0.54, settings=S) == "discard"
    assert classify_candidate(long_term_value="ignore", confidence=0.99, settings=S) == "discard"


def test_classify_topic_similarity_boundaries() -> None:
    assert classify_topic_similarity(similarity=0.72, settings=S) == "auto_merge"
    assert classify_topic_similarity(similarity=0.60, settings=S) == "conflict"
    assert classify_topic_similarity(similarity=0.54, settings=S) == "distinct"


def test_mapping_candidate_acceptance() -> None:
    assert mapping_candidate_accepted(best_score=0.93, second_score=0.77, settings=S)
    assert not mapping_candidate_accepted(best_score=0.91, second_score=0.70, settings=S)
    assert not mapping_candidate_accepted(best_score=0.95, second_score=0.85, settings=S)
    assert mapping_candidate_accepted(best_score=0.95, second_score=None, settings=S)


def test_llm_call_budget() -> None:
    budget = LLMCallBudget()
    for _ in range(LLM_MAX_CALLS_PER_ATTEMPT):
        budget.consume()
    with pytest.raises(LLMBudgetExceededError):
        budget.consume()
    budget.reset_attempt()
    budget.consume()
    assert budget.operation_calls == LLM_MAX_CALLS_PER_ATTEMPT + 1
    while budget.operation_calls < LLM_MAX_CALLS_PER_OPERATION:
        budget.reset_attempt()
        budget.consume()
    assert budget.operation_exhausted
    with pytest.raises(LLMBudgetExceededError):
        budget.reset_attempt() or budget.consume()


async def test_fake_client_records_and_budget() -> None:
    client = FakeMemoryLLMClient(
        extract_queue=[CandidateExtractionResult(candidates=[_candidate()])]
    )
    budget = LLMCallBudget()
    result, record = await client.extract_candidates(source_payload="{}", budget=budget)
    assert len(result.candidates) == 1
    assert record.prompt_version == EXTRACT_CANDIDATES_PROMPT_VERSION
    assert budget.operation_calls == 1
    # 队列耗尽后报错
    with pytest.raises(OpenAISchemaInvalidError):
        await client.extract_candidates(source_payload="{}", budget=budget)


async def test_fake_client_raises_queued_exceptions() -> None:
    client = FakeMemoryLLMClient(extract_queue=[OpenAITimeoutError("超时")])
    with pytest.raises(OpenAITimeoutError):
        await client.extract_candidates(source_payload="{}", budget=LLMCallBudget())


def test_prompt_files_loadable_and_versioned() -> None:
    extract_prompt = load_prompt(EXTRACT_CANDIDATES_PROMPT_VERSION)
    plan_prompt = load_prompt(BUILD_MUTATION_PLAN_PROMPT_VERSION)
    assert "用户真实表现" in extract_prompt  # §9.4 区分助手陈述与用户表现
    assert "expected_version" in plan_prompt  # §9.2 禁止模型生成并发令牌
    with pytest.raises(FileNotFoundError):
        load_prompt("nonexistent_v99")
    with pytest.raises(ValueError, match="非法"):
        load_prompt("../etc/passwd")


# ---------------------------------------------------------------------------
# 评测样例（§24 步骤 8）
# ---------------------------------------------------------------------------

EVAL_FILE = Path(__file__).parents[2] / "evals" / "candidate_extraction_samples.jsonl"


class EvalExpectedCandidate(BaseModel):
    memory_type: str
    category: str
    long_term_value: str
    evidence_refs: list[str]


class EvalExpected(BaseModel):
    candidates: list[EvalExpectedCandidate] = []
    ignored_reason_codes_contains: list[str] = []
    must_not_use_refs: list[str] = []
    must_not_contain: list[str] = []


class EvalSample(BaseModel):
    name: str
    description: str
    source_items: list[dict]
    expected: EvalExpected


def test_eval_samples_valid_and_consistent() -> None:
    assert EVAL_FILE.exists(), "评测样例文件缺失"
    lines = [ln for ln in EVAL_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) >= 8, "评测样例覆盖不足"
    valid_categories = set(CandidateMemory.model_fields["category"].annotation.__args__)
    valid_values = set(CandidateMemory.model_fields["long_term_value"].annotation.__args__)
    valid_types = set(CandidateMemory.model_fields["memory_type"].annotation.__args__)
    for line in lines:
        sample = EvalSample.model_validate(json.loads(line))
        refs = {item["source_ref"] for item in sample.source_items}
        for candidate in sample.expected.candidates:
            assert candidate.category in valid_categories
            assert candidate.long_term_value in valid_values
            assert candidate.memory_type in valid_types
            assert candidate.evidence_refs, f"{sample.name}: 候选必须携带证据"
            assert set(candidate.evidence_refs) <= refs, (
                f"{sample.name}: 期望证据引用必须来自输入 source_ref"
            )
        for ref in sample.expected.must_not_use_refs:
            assert ref in refs, f"{sample.name}: must_not_use_refs 引用不存在的来源"
