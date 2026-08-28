"""ContextService：TurnContextSnapshot 构造与 Context View（方案 §9 / §9.4 / 附录 A.5）。

- 每轮只构造一次不可变快照；Rewrite 与 Answer 使用同一实例（§9.1/#3）；
- 历史截断顺序固定（附录 A.5）：
  1. 当前用户消息永远完整保留，不占 20 条计数、不被截断；
  2. 最近消息从新到旧逐条取完整消息，直到触及 20 条或 6000 tokens 任一上限；
  3. 历史摘要：recent messages 装入后仍有剩余空间时保留完整摘要；
     空间不足整体丢弃（降级为有界最近消息）；
  4. 极端超预算不裁剪当前消息，仅记 context_over_budget 指标。
- 同一快照构建两个只读视图：RewriteContextView / AnswerContextView（§9.4）。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from backend.conversation.contracts.graph import (
    SnapshotBudgets,
    SnapshotMemory,
    SnapshotMessage,
    TurnContextSnapshot,
)
from backend.conversation.contracts.retrieval import ActiveCorpusVocabulary
from backend.conversation.services.token_counter import TokenCounter
from backend.settings import Settings


class ContextService:
    """快照与 View 构造（无状态；只依赖注入的 TokenCounter 与 Settings 预算）。"""

    def __init__(
        self,
        *,
        settings: Settings,
        token_counter: TokenCounter,
    ) -> None:
        self._settings = settings
        self._token_counter = token_counter

    def build_snapshot(
        self,
        *,
        user_id: UUID,
        thread_id: UUID,
        turn_id: UUID,
        current_message: str,
        recent_messages: list[dict[str, Any]],
        conversation_summary: str | None,
        memory: dict[str, Any] | None,
        memory_status: str = "unavailable",
    ) -> TurnContextSnapshot:
        """构造不可变快照（§9.2/§9.3）。"""
        history_budget = self._settings.conversation_context_token_budget
        max_messages = self._settings.conversation_context_max_messages

        # 附录 A.5：最近消息从新到旧完整装入，直到触及 20 条或 budget 任一上限
        budget_remaining = history_budget
        kept: list[SnapshotMessage] = []
        for row in sorted(recent_messages, key=lambda r: int(r["sequence"]), reverse=True):
            if len(kept) >= max_messages:
                break
            content = str(row["content"])
            tokens = self._token_counter.count(content)
            if tokens > budget_remaining and kept:
                break
            budget_remaining -= tokens
            kept.append(
                SnapshotMessage(
                    message_id=row["message_id"],
                    role=str(row["role"]),
                    sequence=int(row["sequence"]),
                    content=content,
                )
            )
        kept.reverse()  # 恢复正序

        # 附录 A.5：摘要只在仍有剩余空间时保留完整摘要
        final_summary: str | None = None
        if conversation_summary:
            summary_tokens = self._token_counter.count(conversation_summary)
            if summary_tokens <= budget_remaining:
                final_summary = conversation_summary

        budgets = SnapshotBudgets(
            history_tokens=history_budget,
            memory_tokens=self._settings.conversation_memory_token_budget,
            retrieval_tokens=self._settings.conversation_evidence_token_budget,
            answer_tokens=self._settings.conversation_answer_token_budget,
        )
        snapshot_memory = self._memory_from_context(memory, memory_status)

        snapshot_id = str(uuid4())
        snapshot = TurnContextSnapshot(
            snapshot_id=snapshot_id,
            created_at=datetime.now(UTC),
            user_id=user_id,
            thread_id=thread_id,
            turn_id=turn_id,
            current_message=current_message,
            recent_messages=kept,
            conversation_summary=final_summary,
            memory=snapshot_memory,
            budgets=budgets,
        )
        return snapshot.with_context_hash(self._context_hash(snapshot))

    def _memory_from_context(
        self, memory: dict[str, Any] | None, memory_status: str
    ) -> SnapshotMemory:
        if memory is None:
            return SnapshotMemory(status="unavailable")
        from typing import Literal

        valid: Literal["available", "degraded", "unavailable"]
        if memory_status in ("available", "degraded", "unavailable"):
            valid = memory_status  # type: ignore[assignment]
        else:
            valid = "unavailable"
        return SnapshotMemory(
            status=valid,
            learner=dict(memory.get("learner") or {}),
            mastery=list(memory.get("mastery") or []),
            graph_states=list(memory.get("graph_states") or []),
            recommendations=list(memory.get("recommendations") or []),
            truncated=bool(memory.get("truncated")),
            fetched_at=datetime.now(UTC),
        )

    def _context_hash(self, snapshot: TurnContextSnapshot) -> str:
        """上下文一致性哈希（§9.2：Rewrite 与 Answer 校验同一快照）。"""
        payload = {
            "current_message": snapshot.current_message,
            "recent": [
                {"id": str(m.message_id), "role": m.role, "content": m.content}
                for m in snapshot.recent_messages
            ],
            "summary": snapshot.conversation_summary,
            "memory_status": snapshot.memory.status,
            "memory_truncated": snapshot.memory.truncated,
        }
        canonical = str(sorted(payload.items(), key=lambda kv: kv[0]))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def build_rewrite_view(
        self,
        *,
        snapshot: TurnContextSnapshot,
        vocabulary: ActiveCorpusVocabulary,
        executed_queries: list[str] | None = None,
        missing_aspects: list[str] | None = None,
    ) -> dict[str, Any]:
        """RewriteContextView（§9.4）：含 filter_vocabulary 与已执行查询。"""
        return {
            "current_user_request": snapshot.current_message,
            "conversation_context": {
                "summary": snapshot.conversation_summary,
                "recent_messages": [
                    {"role": m.role, "content": m.content} for m in snapshot.recent_messages
                ],
            },
            "long_term_memory": {
                "status": snapshot.memory.status,
                "learner": snapshot.memory.learner,
                "mastery": snapshot.memory.mastery,
                "truncated": snapshot.memory.truncated,
            },
            "executed_queries": executed_queries or [],
            "missing_aspects": missing_aspects or [],
            "filter_vocabulary": {
                "version": vocabulary.version,
                "allowed_book_ids": list(vocabulary.allowed_book_ids),
                "allowed_grade_levels": list(vocabulary.allowed_grade_levels),
                "allowed_sections": list(vocabulary.allowed_sections),
                "allowed_content_roles": list(vocabulary.allowed_content_roles),
                "allowed_chapter_prefixes": list(vocabulary.allowed_chapter_prefixes),
            },
        }

    def build_answer_view(
        self,
        *,
        snapshot: TurnContextSnapshot,
        standalone_question: str,
        evidence_summary: str,
        evidence_refs: list[str],
        degraded_flags: list[str],
        answer_contract: dict[str, Any] | None = None,
        evidence_assessment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """AnswerContextView（§9.4）：注入回答合同和局部证据状态。"""
        return {
            "current_user_request": snapshot.current_message,
            "conversation_context": {
                "summary": snapshot.conversation_summary,
                "recent_messages": [
                    {"role": m.role, "content": m.content} for m in snapshot.recent_messages
                ],
            },
            "long_term_memory": {
                "status": snapshot.memory.status,
                "learner": snapshot.memory.learner,
            },
            "standalone_question": standalone_question,
            "evidence": evidence_summary,
            "evidence_refs": evidence_refs,
            "answer_contract": answer_contract or {},
            "evidence_assessment": evidence_assessment or {},
            "degraded_flags": degraded_flags,
            "answer_rules": {
                "max_followups": 3,
                "citation_format": "C1...Cn，仅引用提供的证据",
            },
        }
