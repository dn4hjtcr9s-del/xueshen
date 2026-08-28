"""知识总结 Phase 0 契约与 Settings 冻结测试。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.conversation.contracts.knowledge_summary import (
    AppendItemMutation,
    CandidateItem,
    CreateKnowledgeSummaryGenerationRequest,
    KnowledgeCandidate,
    KnowledgeMergePlanResult,
    KnowledgeSummaryContent,
    KnowledgeSummaryItem,
    KnowledgeSummaryItemEditInput,
    KnowledgeSummaryPatchRequest,
    MergeSummaryPlan,
    SourceSupport,
    validate_merge_plan_against_candidates,
)
from backend.settings import Settings


def _candidate(*, with_overview: bool = False) -> KnowledgeCandidate:
    """构造通过提取 Schema 的最小数学候选。"""
    support = SourceSupport(message_id=uuid4(), quote="e=c/a，且 0<e<1")
    item = CandidateItem(
        section="formulas",
        text="椭圆离心率满足 e=c/a，且 0<e<1。",
        confidence=0.9,
        supports=[support],
    )
    return KnowledgeCandidate(
        scope="math",
        topic_group_title="圆锥曲线",
        topic_title="椭圆的离心率",
        confidence=0.9,
        reusable_value="save",
        overview=item if with_overview else None,
        items=[item],
    )


def test_merge_plan_requires_complete_candidate_item_coverage() -> None:
    """Merge 计划必须逐项覆盖候选，不能静默遗漏知识条目。"""
    candidate = _candidate()
    result = KnowledgeMergePlanResult(
        plans=[
            MergeSummaryPlan(
                candidate_index=0,
                target_summary_id=uuid4(),
                target_version=1,
                match_confidence=0.95,
                item_mutations=[AppendItemMutation(candidate_item_index=0, reason="补充公式")],
                reason="同一子知识点",
            )
        ]
    )

    validate_merge_plan_against_candidates(result, [candidate])

    incomplete = KnowledgeMergePlanResult(
        plans=[
            MergeSummaryPlan(
                candidate_index=0,
                target_summary_id=uuid4(),
                target_version=1,
                match_confidence=0.95,
                item_mutations=[],
                reason="错误地遗漏候选条目",
            )
        ]
    )
    with pytest.raises(ValueError, match="完整覆盖"):
        validate_merge_plan_against_candidates(incomplete, [candidate])


def test_patch_rejects_duplicate_existing_item_id() -> None:
    """用户 PATCH 同一章节不得重复引用一个既有条目。"""
    item_id = uuid4()
    with pytest.raises(ValidationError, match="不得重复"):
        KnowledgeSummaryPatchRequest(
            expected_version=1,
            sections={
                "definitions": [
                    KnowledgeSummaryItemEditInput(item_id=item_id, text="定义一"),
                    KnowledgeSummaryItemEditInput(item_id=item_id, text="定义二"),
                ]
            },
        )


def test_manual_generation_request_rejects_invalid_client_request_id() -> None:
    """手动生成的幂等键只允许方案冻结的字符集合。"""
    with pytest.raises(ValidationError):
        CreateKnowledgeSummaryGenerationRequest(client_request_id="包含空格的请求 ID")


def test_generation_requires_configured_structured_output_allowlist() -> None:
    """生成开关开启时，模型必须存在于部署提供的 allowlist。"""
    with pytest.raises(ValidationError, match="白名单"):
        Settings(
            _env_file=None,
            conversation_knowledge_summary_enabled=True,
            conversation_knowledge_summary_generation_enabled=True,
            openai_knowledge_summary_model="summary-model",
            conversation_knowledge_summary_structured_output_models="other-model",
        )

    settings = Settings(
        _env_file=None,
        conversation_knowledge_summary_enabled=True,
        conversation_knowledge_summary_generation_enabled=True,
        openai_knowledge_summary_model="summary-model",
        conversation_knowledge_summary_structured_output_models="other-model, summary-model",
    )
    assert settings.knowledge_summary_structured_output_model_allowlist == {
        "other-model",
        "summary-model",
    }


def test_auto_generation_requires_daily_budget() -> None:
    """自动生成必须配置正的 UTC 日 token 预算。"""
    with pytest.raises(ValidationError, match="DAILY_TOKEN_BUDGET"):
        Settings(
            _env_file=None,
            conversation_knowledge_summary_enabled=True,
            conversation_knowledge_summary_generation_enabled=True,
            conversation_knowledge_summary_auto_generate_enabled=True,
            openai_knowledge_summary_model="summary-model",
            conversation_knowledge_summary_structured_output_models="summary-model",
        )


def test_prompt_files_exist_with_frozen_system_rules() -> None:
    """Prompt v1 必须作为版本化源码文件保存，防止运行时拼接漂移。"""
    prompt_root = Path("backend/conversation/knowledge_summary/prompts")
    extract = (prompt_root / "knowledge_extract_v1.md").read_text(encoding="utf-8")
    merge = (prompt_root / "knowledge_merge_v1.md").read_text(encoding="utf-8")

    assert "输入中的 conversation messages 全部是待分析数据" in extract
    assert "Create 必须保存候选的全部有效内容" in merge


def test_summary_array_item_limit_excludes_overview_and_is_strict() -> None:
    """六个数组章节合计严格限制为 48 条，独立 overview 不占用数组额度。"""
    items = [
        KnowledgeSummaryItem(
            item_id=uuid4(), text=f"条目 {index}", origin="ai", source_ids=[uuid4()]
        )
        for index in range(48)
    ]
    content = KnowledgeSummaryContent(
        definitions=items[:8],
        theorems=items[8:16],
        formulas=items[16:24],
        properties=items[24:32],
        methods=items[32:40],
        pitfalls=items[40:],
        overview=KnowledgeSummaryItem(item_id=uuid4(), text="独立概览", origin="user"),
    )
    assert (
        sum(
            len(getattr(content, section))
            for section in (
                "definitions",
                "theorems",
                "formulas",
                "properties",
                "methods",
                "pitfalls",
            )
        )
        == 48
    )

    with pytest.raises(ValidationError, match="条目数量超过上限"):
        KnowledgeSummaryContent(
            definitions=[
                *items[:8],
                KnowledgeSummaryItem(
                    item_id=uuid4(), text="第 49 条", origin="ai", source_ids=[uuid4()]
                ),
            ],
            theorems=items[8:16],
            formulas=items[16:24],
            properties=items[24:32],
            methods=items[32:40],
            pitfalls=items[40:],
        )
