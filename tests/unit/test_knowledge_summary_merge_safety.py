"""知识总结自动合并的确定性保护、去重和容量裁决单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from backend.conversation.contracts.knowledge_summary import (
    KnowledgeCandidate,
    KnowledgeSummaryContent,
    KnowledgeSummaryItem,
    MergeSummaryPlan,
    ReplaceItemMutation,
    ReplaceOverviewMutation,
    SetOverviewMutation,
)
from backend.conversation.knowledge_summary.normalization import content_hash_v1
from backend.conversation.services import knowledge_summary_generation as generation_module
from backend.conversation.services.knowledge_summary_generation import (
    FrozenInput,
    GenerationDeadLetter,
    _apply_merge,
    _apply_merge_mutations,
    _union_source_ids,
)


def _target(*, protected: list[str] | None = None) -> dict[str, object]:
    return {"protected_sections": protected or []}


def _candidate(text: str, *, section: str = "formulas") -> KnowledgeCandidate:
    message_id = uuid4()
    return KnowledgeCandidate.model_validate(
        {
            "scope": "math",
            "topic_group_title": "函数",
            "topic_title": "导数",
            "confidence": 0.95,
            "reusable_value": "save",
            "items": [
                {
                    "section": section,
                    "text": text,
                    "confidence": 0.95,
                    "supports": [{"message_id": str(message_id), "quote": text}],
                }
            ],
        }
    )


def _source_map(
    candidate: KnowledgeCandidate,
) -> tuple[dict[UUID, UUID], dict[UUID, tuple[datetime, int, UUID]]]:
    source_item = candidate.overview if candidate.overview is not None else candidate.items[0]
    message_id = source_item.supports[0].message_id
    source_id = uuid4()
    now = datetime(2026, 8, 19, tzinfo=UTC)
    return {message_id: source_id}, {source_id: (now, 1, source_id)}


def test_overview_set_cannot_overwrite_existing_or_protected_content() -> None:
    existing_id = uuid4()
    current = KnowledgeSummaryContent(
        overview=KnowledgeSummaryItem(item_id=existing_id, text="用户定义", origin="user")
    )
    candidate = KnowledgeCandidate.model_validate(
        {
            "scope": "math",
            "topic_group_title": "函数",
            "topic_title": "导数",
            "confidence": 0.95,
            "reusable_value": "save",
            "overview": {
                "section": "formulas",
                "text": "新的概览",
                "confidence": 0.95,
                "supports": [{"message_id": str(uuid4()), "quote": "新的概览"}],
            },
            "items": [],
        }
    )
    source_ids, sort_keys = _source_map(candidate)
    plan = MergeSummaryPlan(
        candidate_index=0,
        target_summary_id=uuid4(),
        target_version=1,
        match_confidence=0.95,
        overview_mutation=SetOverviewMutation(reason="set"),
        item_mutations=[],
        reason="merge",
    )
    with pytest.raises(GenerationDeadLetter, match="OUT_OF_SCOPE"):
        _apply_merge_mutations(
            current,
            candidate,
            plan,
            source_ids,
            _target(),
            source_sort_keys=sort_keys,
            warning_codes=[],
        )

    protected_plan = plan.model_copy(
        update={"overview_mutation": SetOverviewMutation(reason="set")}
    )
    with pytest.raises(GenerationDeadLetter, match="PROTECTED_SECTION"):
        _apply_merge_mutations(
            KnowledgeSummaryContent(),
            candidate,
            protected_plan,
            source_ids,
            _target(protected=["overview"]),
            source_sort_keys=sort_keys,
            warning_codes=[],
        )


def test_overview_replace_rejects_user_origin() -> None:
    overview_id = uuid4()
    current = KnowledgeSummaryContent(
        overview=KnowledgeSummaryItem(item_id=overview_id, text="用户维护", origin="user")
    )
    candidate = KnowledgeCandidate.model_validate(
        {
            "scope": "math",
            "topic_group_title": "函数",
            "topic_title": "导数",
            "confidence": 0.95,
            "reusable_value": "save",
            "overview": {
                "section": "formulas",
                "text": "模型替换",
                "confidence": 0.95,
                "supports": [{"message_id": str(uuid4()), "quote": "模型替换"}],
            },
            "items": [],
        }
    )
    source_ids, sort_keys = _source_map(candidate)
    plan = MergeSummaryPlan(
        candidate_index=0,
        target_summary_id=uuid4(),
        target_version=1,
        match_confidence=0.95,
        overview_mutation=ReplaceOverviewMutation(
            existing_overview_item_id=overview_id, reason="replace"
        ),
        item_mutations=[],
        reason="merge",
    )
    with pytest.raises(GenerationDeadLetter, match="UNSAFE_REPLACE"):
        _apply_merge_mutations(
            current,
            candidate,
            plan,
            source_ids,
            _target(),
            source_sort_keys=sort_keys,
            warning_codes=[],
        )


def test_append_duplicate_merges_source_without_duplicate_item() -> None:
    existing_source = uuid4()
    current = KnowledgeSummaryContent(
        formulas=[
            KnowledgeSummaryItem(
                item_id=uuid4(), text="结论。", origin="ai", source_ids=[existing_source]
            )
        ]
    )
    candidate = _candidate("结论.")
    source_ids, sort_keys = _source_map(candidate)
    plan = MergeSummaryPlan(
        candidate_index=0,
        target_summary_id=uuid4(),
        target_version=1,
        match_confidence=0.95,
        overview_mutation=None,
        item_mutations=[{"action": "append", "candidate_item_index": 0, "reason": "append"}],
        reason="merge",
    )
    result = _apply_merge_mutations(
        current,
        candidate,
        plan,
        source_ids,
        _target(),
        source_sort_keys=sort_keys,
        warning_codes=[],
    )
    assert len(result.formulas) == 1
    assert len(result.formulas[0].source_ids) == 2


def test_append_at_capacity_is_skipped_with_warning() -> None:
    current = KnowledgeSummaryContent(
        formulas=[
            KnowledgeSummaryItem(
                item_id=uuid4(), text=f"条目 {index}", origin="ai", source_ids=[uuid4()]
            )
            for index in range(12)
        ]
    )
    candidate = _candidate("新条目")
    source_ids, sort_keys = _source_map(candidate)
    plan = MergeSummaryPlan(
        candidate_index=0,
        target_summary_id=uuid4(),
        target_version=1,
        match_confidence=0.95,
        overview_mutation=None,
        item_mutations=[{"action": "append", "candidate_item_index": 0, "reason": "append"}],
        reason="merge",
    )
    warnings: list[str] = []
    result = _apply_merge_mutations(
        current,
        candidate,
        plan,
        source_ids,
        _target(),
        source_sort_keys=sort_keys,
        warning_codes=warnings,
    )
    assert len(result.formulas) == 12
    assert "SECTION_LIMIT_REACHED" in warnings


def test_source_union_keeps_latest_100_and_serializes_stably() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    source_ids = [uuid4() for _ in range(101)]
    keys = {
        source_id: (base + timedelta(minutes=index), index, source_id)
        for index, source_id in enumerate(source_ids)
    }
    result = _union_source_ids([], source_ids, keys)
    assert len(result) == 100
    assert source_ids[0] not in result
    assert result == sorted(result, key=lambda value: keys[value])


def test_append_at_global_array_capacity_is_skipped_with_warning() -> None:
    """合并追加前检查六个数组章节的总容量，而不是只检查单章节。"""
    current = KnowledgeSummaryContent(
        definitions=[
            KnowledgeSummaryItem(
                item_id=uuid4(), text=f"定义 {index}", origin="ai", source_ids=[uuid4()]
            )
            for index in range(8)
        ],
        theorems=[
            KnowledgeSummaryItem(
                item_id=uuid4(), text=f"定理 {index}", origin="ai", source_ids=[uuid4()]
            )
            for index in range(8)
        ],
        formulas=[
            KnowledgeSummaryItem(
                item_id=uuid4(), text=f"公式 {index}", origin="ai", source_ids=[uuid4()]
            )
            for index in range(8)
        ],
        properties=[
            KnowledgeSummaryItem(
                item_id=uuid4(), text=f"性质 {index}", origin="ai", source_ids=[uuid4()]
            )
            for index in range(8)
        ],
        methods=[
            KnowledgeSummaryItem(
                item_id=uuid4(), text=f"方法 {index}", origin="ai", source_ids=[uuid4()]
            )
            for index in range(8)
        ],
        pitfalls=[
            KnowledgeSummaryItem(
                item_id=uuid4(), text=f"易错点 {index}", origin="ai", source_ids=[uuid4()]
            )
            for index in range(8)
        ],
    )
    candidate = _candidate("全局容量外的新条目")
    source_ids, sort_keys = _source_map(candidate)
    plan = MergeSummaryPlan(
        candidate_index=0,
        target_summary_id=uuid4(),
        target_version=1,
        match_confidence=0.95,
        overview_mutation=None,
        item_mutations=[{"action": "append", "candidate_item_index": 0, "reason": "append"}],
        reason="merge",
    )
    warnings: list[str] = []
    result = _apply_merge_mutations(
        current,
        candidate,
        plan,
        source_ids,
        _target(),
        source_sort_keys=sort_keys,
        warning_codes=warnings,
    )
    assert (
        sum(
            len(getattr(result, section))
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
    assert "SECTION_LIMIT_REACHED" in warnings


class _MergeSideEffectProbe:
    """记录 _apply_merge 在超限时是否错误写入来源或版本副作用。"""

    def __init__(self) -> None:
        self.source_rows: list[object] = []
        self.snapshots: list[object] = []
        self.revisions: list[object] = []
        self.counts: list[object] = []
        self.aliases: list[object] = []


def _merge_row(content: KnowledgeSummaryContent, *, generation_id: UUID) -> dict[str, object]:
    return {
        "generation_id": generation_id,
        "user_id": uuid4(),
        "source_checkpoint_id": "checkpoint",
        "trigger": "manual",
    }


def _frozen_for_message(message_id: UUID) -> FrozenInput:
    thread_id = uuid4()
    turn_id = uuid4()
    occurred_at = datetime(2026, 8, 20, tzinfo=UTC)
    return FrozenInput(
        manifest={},
        messages={
            message_id: {
                "message_id": message_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "role": "assistant",
                "sequence": 2,
                "occurred_at": occurred_at,
            }
        },
        conversation_summary=None,
    )


def _install_merge_probe(
    monkeypatch: pytest.MonkeyPatch,
    probe: _MergeSideEffectProbe,
    *,
    existing_source_id: UUID,
) -> None:
    async def source_ids(*_args: object, **_kwargs: object) -> dict[UUID, UUID]:
        return {}

    async def source_sort_keys(
        *_args: object, **_kwargs: object
    ) -> dict[UUID, tuple[datetime, int, UUID]]:
        return {existing_source_id: (datetime(2026, 8, 19, tzinfo=UTC), 1, existing_source_id)}

    async def insert_sources(*_args: object, **kwargs: object) -> None:
        probe.source_rows.append(kwargs)

    async def update_snapshot(*_args: object, **kwargs: object) -> None:
        probe.snapshots.append(kwargs)

    async def insert_revision(*_args: object, **kwargs: object) -> None:
        probe.revisions.append(kwargs)

    async def recalculate(*_args: object, **kwargs: object) -> None:
        probe.counts.append(kwargs)

    async def upsert_alias(*_args: object, **kwargs: object) -> None:
        probe.aliases.append(kwargs)

    monkeypatch.setattr(generation_module.summaries_repo, "get_source_ids_by_messages", source_ids)
    monkeypatch.setattr(generation_module.summaries_repo, "get_source_sort_keys", source_sort_keys)
    monkeypatch.setattr(
        generation_module.summaries_repo, "insert_source_rows_with_ids", insert_sources
    )
    monkeypatch.setattr(
        generation_module.summaries_repo,
        "update_generation_summary_snapshot",
        update_snapshot,
    )
    monkeypatch.setattr(
        generation_module.summaries_repo, "insert_generation_revision", insert_revision
    )
    monkeypatch.setattr(
        generation_module.summaries_repo,
        "lock_and_recalculate_source_counts",
        recalculate,
    )
    monkeypatch.setattr(generation_module.summaries_repo, "upsert_generation_alias", upsert_alias)


@pytest.mark.asyncio
async def test_overview_replace_over_limit_has_no_source_or_version_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """overview 超过 24,000 字符时，文本、来源、版本和 Revision 都保持不变。"""
    old_source_id = uuid4()
    new_message_id = uuid4()
    overview = KnowledgeSummaryItem(
        item_id=uuid4(), text="旧", origin="ai", source_ids=[old_source_id]
    )
    items = [
        KnowledgeSummaryItem(
            item_id=uuid4(), text="x" * 999, origin="ai", source_ids=[old_source_id]
        )
        for _ in range(12)
    ]
    current = KnowledgeSummaryContent(
        overview=overview,
        definitions=items,
        formulas=[
            KnowledgeSummaryItem(
                item_id=uuid4(), text="x" * 999, origin="ai", source_ids=[old_source_id]
            )
            for _ in range(12)
        ],
    )
    candidate = KnowledgeCandidate.model_validate(
        {
            "scope": "math",
            "topic_group_title": "函数",
            "topic_title": "导数",
            "confidence": 0.95,
            "reusable_value": "save",
            "overview": {
                "section": "formulas",
                "text": "y" * 1000,
                "confidence": 0.95,
                "supports": [{"message_id": str(new_message_id), "quote": "y"}],
            },
            "items": [],
        }
    )
    plan = MergeSummaryPlan(
        candidate_index=0,
        target_summary_id=uuid4(),
        target_version=1,
        match_confidence=0.95,
        overview_mutation=ReplaceOverviewMutation(
            existing_overview_item_id=overview.item_id, reason="超限替换"
        ),
        item_mutations=[],
        reason="merge",
    )
    probe = _MergeSideEffectProbe()
    _install_merge_probe(monkeypatch, probe, existing_source_id=old_source_id)
    warnings: list[str] = []
    target = {
        **_merge_row(current, generation_id=uuid4()),
        "summary_id": plan.target_summary_id,
        "version": 1,
        "content": current.model_dump(mode="json"),
        "content_hash": content_hash_v1(current),
        "topic_group_title": "函数",
        "topic_title": "导数",
        "normalized_topic_group": "函数",
        "protected_sections": [],
        "review_state": "clean",
    }

    changed = await _apply_merge(
        SimpleNamespace(),
        row=_merge_row(current, generation_id=uuid4()),
        candidate=candidate,
        plan=plan,
        target=target,
        frozen=_frozen_for_message(new_message_id),
        warning_codes=warnings,
    )

    assert changed is False
    assert "SECTION_LIMIT_REACHED" in warnings
    assert current.model_dump(mode="json") == target["content"]
    assert not probe.source_rows
    assert not probe.snapshots
    assert not probe.revisions
    assert not probe.counts
    assert not probe.aliases


@pytest.mark.asyncio
async def test_item_replace_over_limit_has_no_source_or_version_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数组条目替换超过总字符上限时，不附加新来源或更新版本。"""
    old_source_id = uuid4()
    target_item = KnowledgeSummaryItem(
        item_id=uuid4(), text="旧", origin="ai", source_ids=[old_source_id]
    )
    definitions = [
        KnowledgeSummaryItem(
            item_id=uuid4(), text="x" * 999, origin="ai", source_ids=[old_source_id]
        )
        for _ in range(12)
    ]
    formulas = [target_item] + [
        KnowledgeSummaryItem(
            item_id=uuid4(), text="x" * 999, origin="ai", source_ids=[old_source_id]
        )
        for _ in range(11)
    ]
    properties = [
        KnowledgeSummaryItem(
            item_id=uuid4(), text="x" * 999, origin="ai", source_ids=[old_source_id]
        )
    ]
    current = KnowledgeSummaryContent(
        definitions=definitions, formulas=formulas, properties=properties
    )
    candidate_message_id = uuid4()
    candidate = KnowledgeCandidate.model_validate(
        {
            "scope": "math",
            "topic_group_title": "函数",
            "topic_title": "导数",
            "confidence": 0.95,
            "reusable_value": "save",
            "items": [
                {
                    "section": "formulas",
                    "text": "y" * 1000,
                    "confidence": 0.95,
                    "supports": [{"message_id": str(candidate_message_id), "quote": "y"}],
                }
            ],
        }
    )
    plan = MergeSummaryPlan(
        candidate_index=0,
        target_summary_id=uuid4(),
        target_version=1,
        match_confidence=0.95,
        overview_mutation=None,
        item_mutations=[
            ReplaceItemMutation(
                candidate_item_index=0,
                existing_item_id=target_item.item_id,
                reason="超限替换",
            )
        ],
        reason="merge",
    )
    probe = _MergeSideEffectProbe()
    _install_merge_probe(monkeypatch, probe, existing_source_id=old_source_id)
    warnings: list[str] = []
    target = {
        **_merge_row(current, generation_id=uuid4()),
        "summary_id": plan.target_summary_id,
        "version": 1,
        "content": current.model_dump(mode="json"),
        "content_hash": content_hash_v1(current),
        "topic_group_title": "函数",
        "topic_title": "导数",
        "normalized_topic_group": "函数",
        "protected_sections": [],
        "review_state": "clean",
    }

    changed = await _apply_merge(
        SimpleNamespace(),
        row=_merge_row(current, generation_id=uuid4()),
        candidate=candidate,
        plan=plan,
        target=target,
        frozen=_frozen_for_message(candidate_message_id),
        warning_codes=warnings,
    )

    assert changed is False
    assert "SECTION_LIMIT_REACHED" in warnings
    assert current.model_dump(mode="json") == target["content"]
    assert not probe.source_rows
    assert not probe.snapshots
    assert not probe.revisions
    assert not probe.counts
    assert not probe.aliases
