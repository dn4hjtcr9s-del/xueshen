"""读路径双源协调单元测试（memory-rebuild §3.5② / §5.9；决议 D 组）。

覆盖：
- 两路一致 → 无冲突标记；
- 记忆更新鲜 → 取记忆值；
- KG 更新鲜 → 取 KG 值；
- 并列/同刻 → 两者都出现在输出里（``memory_version`` / ``kg_projected_version``
  与两路时间戳）且带 ``conflict: true``；
- 单侧缺失 → 覆盖缺口不算冲突（既有行为不变）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from backend.memory.contracts.context import LearningContextGraphState
from backend.memory.services.context_service import (
    GraphStateTask,
    align_graph_states,
    assemble_context,
    reconcile_graph_states,
)
from backend.memory.storage.markdown_schema import LearnerDocument, MasteryDocument

USER_ID = uuid4()
NOW = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=6)


def _mastery(topic_key: str, **overrides: Any) -> MasteryDocument:
    base: dict[str, Any] = {
        "user_id": USER_ID,
        "topic_key": topic_key,
        "topic_title": f"主题{topic_key}",
        "version": 2,
        "updated_at": NOW,
        "overview": "整体掌握良好。",
        "understood": ["定义"],
        "difficulties": [],
        "review_advice": [],
        "evidence_refs": ["conv:t1:m1"],
    }
    base.update(overrides)
    return MasteryDocument(**base)  # type: ignore[arg-type]


def _learner() -> LearnerDocument:
    return LearnerDocument(
        user_id=USER_ID,
        version=1,
        updated_at=NOW,
        preferences=["例题驱动"],
        goals=["期末 90 分"],
        plans=["每天一节"],
        evidence_refs=["conv:t1:m1"],
    )


def _base_state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "node_id": "n007",
        "title": "椭圆",
        "kg_status": "proficient",
        "kg_projected_version": 3,
        "kg_updated_at": NOW,
        "memory_version": 3,
        "memory_updated_at": NOW,
        "memory_status": None,
        "kg_conflict_signal": False,
        "kg_reason_codes": ["SUMMARY_MEMORY_SIGNAL"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 纯函数裁决
# ---------------------------------------------------------------------------


def test_aligned_sources_have_no_conflict_marker() -> None:
    state = reconcile_graph_states(**_base_state())
    assert state.conflict is False
    assert state.conflict_reason is None
    assert state.status == "proficient"
    assert state.state_source == "memory"
    assert state.memory_version == 3
    assert state.kg_projected_version == 3
    assert "DUAL_SOURCE_CONFLICT" not in state.reason_codes


def test_memory_newer_wins_and_flags_conflict() -> None:
    state = reconcile_graph_states(
        **_base_state(
            kg_status="learning",
            kg_projected_version=2,
            memory_version=4,
            memory_status="proficient",
        )
    )
    assert state.status == "proficient"
    assert state.state_source == "memory"
    assert state.conflict is True
    assert state.conflict_reason == "memory_newer_version:4>2"
    assert "DUAL_SOURCE_CONFLICT" in state.reason_codes


def test_kg_newer_wins_and_flags_conflict() -> None:
    state = reconcile_graph_states(
        **_base_state(
            kg_status="expert",
            kg_projected_version=5,
            memory_version=3,
            memory_status="learning",
        )
    )
    assert state.status == "expert"
    assert state.state_source == "kg"
    assert state.conflict is True
    assert state.conflict_reason == "kg_newer_version:5>3"
    assert "DUAL_SOURCE_CONFLICT" in state.reason_codes


def test_version_tie_keeps_both_sides_and_marks_conflict() -> None:
    state = reconcile_graph_states(
        **_base_state(
            kg_status="proficient",
            kg_projected_version=3,
            memory_version=3,
            memory_status="learning",
            memory_updated_at=LATER,
        )
    )
    # 并列（同版本）时长期记忆侧优先，两路取值都在返回结构里
    assert state.status == "learning"
    assert state.state_source == "memory"
    assert state.conflict is True
    assert state.conflict_reason == "version_tie:memory=learning,kg=proficient,memory_preferred"
    # 两路信息都在输出里，不静默丢差异
    assert state.kg_projected_version == 3 and state.memory_version == 3
    assert state.memory_updated_at == LATER
    assert state.kg_updated_at == NOW


def test_same_time_tie_prefers_memory_and_marks_conflict() -> None:
    state = reconcile_graph_states(
        **_base_state(
            kg_projected_version=None,
            memory_version=None,
            kg_updated_at=NOW,
            memory_updated_at=NOW,
            kg_status="proficient",
            memory_status="learning",
        )
    )
    assert state.conflict is True
    assert state.conflict_reason == "tie_same_time:memory_preferred"
    assert state.status == "learning"
    assert state.state_source == "memory"


def test_memory_newer_by_time_wins() -> None:
    state = reconcile_graph_states(
        **_base_state(
            kg_projected_version=None,
            memory_version=None,
            kg_updated_at=NOW,
            memory_updated_at=LATER,
            kg_status="proficient",
            memory_status="learning",
        )
    )
    assert state.status == "learning"
    assert state.state_source == "memory"
    assert state.conflict is True
    assert state.conflict_reason == "memory_newer_time"


def test_kg_newer_by_time_wins() -> None:
    state = reconcile_graph_states(
        **_base_state(
            kg_projected_version=None,
            memory_version=None,
            kg_updated_at=LATER,
            memory_updated_at=NOW,
            kg_status="expert",
            memory_status="learning",
        )
    )
    assert state.status == "expert"
    assert state.state_source == "kg"
    assert state.conflict is True
    assert state.conflict_reason == "kg_newer_time"


def test_single_side_present_is_not_a_conflict() -> None:
    only_kg = reconcile_graph_states(
        **_base_state(memory_version=None, memory_updated_at=None, memory_status=None)
    )
    assert only_kg.conflict is False
    assert only_kg.state_source == "kg"
    assert only_kg.status == "proficient"

    only_memory = reconcile_graph_states(
        **_base_state(
            kg_status=None,
            kg_projected_version=None,
            kg_updated_at=None,
            memory_status="learning",
        )
    )
    assert only_memory.conflict is False
    assert only_memory.state_source == "memory"
    assert only_memory.status == "learning"


def test_kg_unresolved_conflict_signal_is_forwarded() -> None:
    state = reconcile_graph_states(
        **_base_state(
            kg_status="learning",
            memory_version=3,
            memory_status="learning",
            kg_conflict_signal=True,
        )
    )
    assert "REVIEW_AFTER_CONFLICT" in state.reason_codes


def test_projected_memory_mismatch_is_flagged() -> None:
    state = reconcile_graph_states(
        **_base_state(
            memory_id="mastery:椭圆",
            kg_projected_memory_id="mastery:双曲线",
            memory_version=3,
            kg_projected_version=3,
        )
    )
    assert state.conflict is True
    assert state.conflict_reason == ("source_mismatch:kg=mastery:双曲线,memory=mastery:椭圆")


def test_align_passthrough_without_tasks() -> None:
    state = LearningContextGraphState(
        node_id="n007", title="椭圆", status="learning", reason_codes=["CONTINUE_LEARNING"]
    )
    assert align_graph_states([state], tasks=None) == [state]
    # 既有键的序列化形状不变（协调未启用时新键保持缺省）
    assert state.model_dump() == {
        "node_id": "n007",
        "title": "椭圆",
        "status": "learning",
        "reason_codes": ["CONTINUE_LEARNING"],
        "state_source": None,
        "conflict": False,
        "conflict_reason": None,
        "memory_version": None,
        "kg_projected_version": None,
        "memory_updated_at": None,
        "kg_updated_at": None,
    }


def test_align_applies_tasks_per_node() -> None:
    states = [
        LearningContextGraphState(
            node_id="n007", title="椭圆", status="proficient", reason_codes=[]
        ),
        LearningContextGraphState(node_id="n009", title="双曲线", status=None, reason_codes=[]),
    ]
    tasks = {
        "n007": GraphStateTask(
            memory_id="mastery:椭圆",
            memory_version=4,
            memory_updated_at=LATER,
            memory_status="learning",
            projected_version=2,
            projected_memory_id="mastery:椭圆",
            current_status="proficient",
            kg_updated_at=NOW,
            conflict_signal=False,
        )
    }
    aligned = align_graph_states(states, tasks=tasks)
    assert aligned[0].conflict is True
    assert aligned[0].status == "learning"
    assert aligned[1].node_id == "n009" and aligned[1].conflict is False


# ---------------------------------------------------------------------------
# 组装阶段的端到端行为（assemble_context 带 tasks）
# ---------------------------------------------------------------------------


def _assemble(tasks: dict[str, GraphStateTask] | None) -> Any:
    return assemble_context(
        user_id=USER_ID,
        query="椭圆",
        budget=3000,
        learner=_learner(),
        exact_mastery=[_mastery("椭圆")],
        weak_mastery=[],
        graph_states=[
            LearningContextGraphState(
                node_id="n007", title="椭圆", status="proficient", reason_codes=[]
            )
        ],
        recommendations=[],
        graph_state_tasks=tasks,
    )


def test_assemble_context_without_tasks_keeps_legacy_shape() -> None:
    context = _assemble(None)
    assert context.graph_states[0].status == "proficient"
    assert context.graph_states[0].conflict is False
    assert context.truncated is False


def test_assemble_context_surfaces_conflict_marker() -> None:
    context = _assemble(
        {
            "n007": GraphStateTask(
                memory_id="mastery:椭圆",
                memory_version=5,
                memory_updated_at=LATER,
                memory_status="learning",
                projected_version=3,
                projected_memory_id="mastery:椭圆",
                current_status="proficient",
                kg_updated_at=NOW,
                conflict_signal=False,
            )
        }
    )
    state = context.graph_states[0]
    assert state.status == "learning"
    assert state.conflict is True
    assert state.state_source == "memory"
    assert "DUAL_SOURCE_CONFLICT" in state.reason_codes
    # 两路都在输出里
    assert state.memory_version == 5 and state.kg_projected_version == 3
