"""确定性策略阈值（§9.3 / §16.3）。

所有阈值集中在 settings/policy，写入指标，后续通过评测调整，不散落在 Prompt 中。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from backend.settings import Settings

# LLM 调用预算（§9.1）
LLM_MAX_CALLS_PER_ATTEMPT = 2
LLM_MAX_CALLS_PER_OPERATION = 4
EXTRACT_MAX_OUTPUT_TOKENS = 3000
PLAN_MAX_OUTPUT_TOKENS = 4000

CandidateDisposition = Literal["auto_save", "review", "discard"]
TopicSimilarityDisposition = Literal["auto_merge", "conflict", "distinct"]


def candidate_evidence_issues(
    candidate: dict[str, Any], source_items: list[dict[str, Any]]
) -> list[str]:
    """执行记忆证据硬校验，避免把写入安全边界交给 Prompt。

    返回值为空表示证据结构和来源角色满足门槛；返回值非空时，候选最多进入审核，
    缺少可审计来源的候选直接丢弃。该校验只针对总结记忆候选，不影响用户显式命令。
    """
    evidence = list(candidate.get("evidence") or [])
    source_roles_by_ref: dict[str, set[str]] = {}
    for item in source_items:
        source_roles_by_ref.setdefault(str(item.get("source_ref")), set()).add(
            str(item.get("role") or "")
        )
    if not evidence:
        return ["MEMORY_EVIDENCE_REQUIRED"]

    issues: list[str] = []
    memory_type = str(candidate.get("memory_type") or "")
    category = str(candidate.get("category") or "")
    if memory_type == "learner" and category not in {"preference", "goal", "plan"}:
        issues.append("LEARNER_CATEGORY_MISMATCH")
    if memory_type == "mastery" and category not in {
        "understanding",
        "difficulty",
        "misconception",
        "review_advice",
    }:
        issues.append("MASTERY_CATEGORY_MISMATCH")
    if memory_type == "mastery" and not str(candidate.get("topic_title") or "").strip():
        issues.append("MASTERY_TOPIC_REQUIRED")
    valid_refs = 0
    valid_learning_roles = 0
    for item in evidence:
        evidence_ref = str(item.get("evidence_ref") or "")
        roles = source_roles_by_ref.get(evidence_ref)
        if roles is None:
            issues.append("MEMORY_EVIDENCE_REF_NOT_FOUND")
            continue
        if len(roles) != 1:
            issues.append("MEMORY_EVIDENCE_REF_AMBIGUOUS")
            continue
        role = next(iter(roles))
        valid_refs += 1
        evidence_type = str(item.get("evidence_type") or "")
        if role in {"assistant", "tool"}:
            issues.append("ASSISTANT_TOOL_EVIDENCE_NOT_SUPPORTING_MEMORY")
        if _evidence_type_requires_user(evidence_type) and role != "user":
            issues.append("MEMORY_USER_EVIDENCE_REQUIRED")
        elif role in {"user", "activity"}:
            valid_learning_roles += 1

    expected_type = {
        "preference": "preference_statement",
        "goal": "goal_statement",
        "plan": "plan_statement",
    }.get(category)
    if expected_type and not any(
        str(item.get("evidence_type") or "") in {expected_type, "explicit_user_statement"}
        and _single_source_role(
            source_roles_by_ref,
            str(item.get("evidence_ref") or ""),
        )
        == "user"
        for item in evidence
    ):
        issues.append("LEARNER_CATEGORY_REQUIRES_EXPLICIT_USER_STATEMENT")

    if valid_refs == 0:
        issues.append("MEMORY_EVIDENCE_NOT_AUDITABLE")

    if memory_type == "learner" and valid_learning_roles == 0:
        issues.append("LEARNER_MEMORY_REQUIRES_USER_EVIDENCE")
    if memory_type == "mastery" and valid_learning_roles == 0:
        issues.append("MASTERY_MEMORY_REQUIRES_LEARNING_EVIDENCE")
    if memory_type == "mastery" and not any(
        str(item.get("evidence_type") or "")
        in {"user_solution", "exercise_result", "repeated_error", "learning_activity"}
        and _single_source_role(
            source_roles_by_ref,
            str(item.get("evidence_ref") or ""),
        )
        in {"user", "activity"}
        for item in evidence
    ):
        issues.append("MASTERY_SELF_REPORT_REQUIRES_REVIEW")
    if category in {"understanding", "difficulty", "misconception", "review_advice"}:
        if not any(
            _single_source_role(source_roles_by_ref, str(item.get("evidence_ref") or ""))
            in {"user", "activity"}
            for item in evidence
        ):
            issues.append("MASTERY_MEMORY_REQUIRES_USER_OR_ACTIVITY")
    return list(dict.fromkeys(issues))


def _single_source_role(source_roles_by_ref: dict[str, set[str]], evidence_ref: str) -> str | None:
    """返回唯一来源角色；同一 ref 对应多个角色时视为不可审计。"""
    roles = source_roles_by_ref.get(evidence_ref)
    if roles is None or len(roles) != 1:
        return None
    return next(iter(roles))


def evidence_gate_disposition(
    *,
    candidate: dict[str, Any],
    source_items: list[dict[str, Any]],
    disposition: CandidateDisposition,
) -> CandidateDisposition:
    """把证据校验结果转换为写入分流结果。"""
    issues = candidate_evidence_issues(candidate, source_items)
    if not issues:
        return disposition
    if "MEMORY_EVIDENCE_NOT_AUDITABLE" in issues or "MEMORY_EVIDENCE_REQUIRED" in issues:
        return "discard"
    return "review" if disposition != "discard" else disposition


def validate_commit_evidence_refs(*, evidence_refs: list[str], allowed_refs: set[str]) -> None:
    """提交前再次确认计划只携带本次来源中的证据引用。"""
    if not evidence_refs:
        raise ValueError("记忆变更计划缺少证据引用")
    unknown = sorted(set(evidence_refs) - allowed_refs)
    if unknown:
        raise ValueError(f"记忆变更计划包含未授权证据引用: {unknown[:3]}")


def _evidence_type_requires_user(evidence_type: str) -> bool:
    return evidence_type in {
        "explicit_user_statement",
        "user_solution",
        "preference_statement",
        "goal_statement",
        "plan_statement",
    }


def classify_candidate(
    *, long_term_value: str, confidence: float, settings: Settings
) -> CandidateDisposition:
    """长期价值与置信度分类（§9.3）。

    - 自动写入：confidence >= MEMORY_AUTO_WRITE_CONFIDENCE 且 long_term_value=save
    - 进入候选审核：0.55 <= confidence < 0.80 或 long_term_value=review
    - 丢弃：confidence < 0.55 或 long_term_value=ignore
    """
    if long_term_value == "ignore" or confidence < settings.memory_review_min_confidence:
        return "discard"
    if long_term_value == "save" and confidence >= settings.memory_auto_write_confidence:
        return "auto_save"
    return "review"


def classify_topic_similarity(
    *, similarity: float, settings: Settings
) -> TopicSimilarityDisposition:
    """主题相似度分类（§9.3）：>=0.72 自动合并；0.55–0.72 冲突需审核；否则独立主题。"""
    if similarity >= settings.memory_auto_merge_trgm:
        return "auto_merge"
    if similarity >= settings.memory_topic_conflict_trgm_min:
        return "conflict"
    return "distinct"


def mapping_candidate_accepted(
    *, best_score: float, second_score: float | None, settings: Settings
) -> bool:
    """图谱模型候选映射校验（§9.3）：best >= 0.92 且与第二名差值 >= 0.15。"""
    if best_score < settings.graph_projection_mapping_min:
        return False
    if second_score is None:
        return True
    return (best_score - second_score) >= settings.graph_projection_mapping_margin


@dataclass
class LLMCallBudget:
    """LLM 调用预算（§9.1）：2 次/operation attempt，4 次/任务生命周期。

    超过生命周期上限后不再调用模型：有可用候选则进入 needs_review，
    否则进入 dead_letter 并记录可重试性（由 Graph/Worker 层执行）。
    """

    attempt_calls: int = 0
    operation_calls: int = 0

    def can_call(self) -> bool:
        return (
            self.attempt_calls < LLM_MAX_CALLS_PER_ATTEMPT
            and self.operation_calls < LLM_MAX_CALLS_PER_OPERATION
        )

    @property
    def operation_exhausted(self) -> bool:
        return self.operation_calls >= LLM_MAX_CALLS_PER_OPERATION

    def consume(self) -> None:
        if not self.can_call():
            raise LLMBudgetExceededError(
                f"LLM 调用预算耗尽: attempt={self.attempt_calls}/"
                f"{LLM_MAX_CALLS_PER_ATTEMPT}, operation={self.operation_calls}/"
                f"{LLM_MAX_CALLS_PER_OPERATION}"
            )
        self.attempt_calls += 1
        self.operation_calls += 1

    def reset_attempt(self) -> None:
        """新 attempt 开始时重置单次预算，保留生命周期计数。"""
        self.attempt_calls = 0


class LLMBudgetExceededError(Exception):
    """LLM 调用预算耗尽（§9.1）。"""
