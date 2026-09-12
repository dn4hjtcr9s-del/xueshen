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
import json
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
        # prime 模式（§2.4 D1）：摘要 + 注册表目录是**固定提示词**，随快照进入
        # answer 视图；非 prime 模式取不到该键，保持空 dict。
        # review I-5：快照是 prime 的**唯一构造点**，在这里固化"注入内容有界"的不变量
        # （节点侧已按同一预算裁剪，这里兜住任何未经裁剪的生产者）。
        prime, prime_budget_truncated = trim_prime_to_budget(
            dict(memory.get("prime") or {}),
            budget_tokens=self._settings.conversation_memory_token_budget,
            token_counter=self._token_counter,
        )
        return SnapshotMemory(
            status=valid,
            learner=dict(memory.get("learner") or {}),
            mastery=list(memory.get("mastery") or []),
            graph_states=list(memory.get("graph_states") or []),
            recommendations=list(memory.get("recommendations") or []),
            truncated=bool(memory.get("truncated")) or prime_budget_truncated,
            fetched_at=datetime.now(UTC),
            prime=prime,
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
                "graph_states": snapshot.memory.graph_states,
                "recommendations": snapshot.memory.recommendations,
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
        retrieval_decision: dict[str, Any] | None = None,
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
            "long_term_memory": _long_term_memory_view(
                snapshot.memory,
                budget_tokens=self._settings.conversation_memory_token_budget,
                token_counter=self._token_counter,
            ),
            "standalone_question": standalone_question,
            "evidence": evidence_summary,
            "evidence_refs": evidence_refs,
            "answer_contract": answer_contract or {},
            "evidence_assessment": evidence_assessment or {},
            "retrieval_decision": retrieval_decision or {},
            "degraded_flags": degraded_flags,
            "answer_rules": {
                "max_followups": 3,
                "citation_format": "C1...Cn，仅引用提供的证据",
            },
        }


def _long_term_memory_view(
    memory: SnapshotMemory,
    *,
    budget_tokens: int,
    token_counter: TokenCounter | None,
) -> dict[str, Any]:
    """长期记忆在回答视图里的投影（§9.4）。

    prime 模式（§2.4 D1）额外挂 `prime` 键（摘要 + 注册表目录，原样复用不重检索）；
    prime 为空时**不加这个键**，保证 flag 关闭时下发给模型的 JSON 逐字不变。

    review I-5：这里再按同一预算裁一次，是为了兜住**旧 checkpoint 里的快照**（修复前
    写入的 prime 可能未裁剪，`snapshot_from_dict` 会原样还原），保证"下发给模型的 prime
    一定有界"这条不变量在视图出口也成立。已裁剪过的 prime 走到这里是 no-op。
    """
    view: dict[str, Any] = {"status": memory.status, "learner": memory.learner}
    if memory.prime:
        prime, _truncated = trim_prime_to_budget(
            memory.prime, budget_tokens=budget_tokens, token_counter=token_counter
        )
        view["prime"] = prime
    return view


def trim_prime_to_budget(
    prime: dict[str, Any],
    *,
    budget_tokens: int,
    token_counter: TokenCounter | None,
) -> tuple[dict[str, Any], bool]:
    """按 token 预算裁剪 prime 的注册表目录（review I-5），返回 (prime, 是否裁剪)。

    预算复用 ``conversation_memory_token_budget``（与旧 memory 读取、memory 工具结果
    同一预算族）：prime 与它们是**同一类"长期记忆进提示词"的内容**，共用一份预算才能
    让快照里申报的 `budgets.memory_tokens` 与实际注入量一致；另开一个 setting 会让两者
    之和超出快照声明的预算。

    - 保留 summary（服务端已单独限长）与目录的**前缀**：条目按服务端固定序
      （memory_id 升序）逐条计入，计入后超预算的那条连同其后的条目一起不注入；
    - 一旦发生裁剪必须置 `index_entries_truncated=true`——**不静默丢内容**，
      调用方据此发 `memory_prime_degraded` 降级标记（服务端的条数上限用同一字段）；
    - `budget_tokens <= 0`（未配置预算）或没有 token 计数器时不裁剪，与
      `memory_tool` 的 `_apply_token_budget` 保持同一语义。
    """
    entries = prime.get("index_entries")
    if not isinstance(entries, list) or not entries:
        return prime, False
    if budget_tokens <= 0 or token_counter is None:
        return prime, False
    used = int(token_counter.count(str(prime.get("summary") or "")))
    kept: list[Any] = []
    for entry in entries:
        cost = int(
            token_counter.count(json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str))
        )
        if used + cost > budget_tokens:
            break
        used += cost
        kept.append(entry)
    if len(kept) == len(entries):
        return prime, False
    trimmed = dict(prime)
    trimmed["index_entries"] = kept
    trimmed["index_entries_truncated"] = True
    return trimmed, True
