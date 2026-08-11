"""确定性策略阈值（§9.3 / §16.3）。

所有阈值集中在 settings/policy，写入指标，后续通过评测调整，不散落在 Prompt 中。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from backend.settings import Settings

# LLM 调用预算（§9.1）
LLM_MAX_CALLS_PER_ATTEMPT = 2
LLM_MAX_CALLS_PER_OPERATION = 4
EXTRACT_MAX_OUTPUT_TOKENS = 3000
PLAN_MAX_OUTPUT_TOKENS = 4000

CandidateDisposition = Literal["auto_save", "review", "discard"]
TopicSimilarityDisposition = Literal["auto_merge", "conflict", "distinct"]


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
