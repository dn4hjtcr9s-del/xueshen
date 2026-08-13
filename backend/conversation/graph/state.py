"""ConversationGraph State 与 Runtime Context（方案 §10）。

State 只保存可序列化数据并按 §10.3 的 Reducer 规则合并：
worker_results 以 worker_key 为键的 Map（覆盖而非追加、不依赖完成顺序、
恢复后一致、聚合前按确定性 Key 排序）；运行时对象（Gateway/Repository/
Writer/Clock/取消令牌/flag 快照）通过 ConversationRuntimeContext 注入，不进入 Checkpoint。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Protocol, TypedDict
from uuid import UUID

from backend.conversation.contracts.graph import (
    RewritePlan,
    TurnContextSnapshot,
)


class ConversationGraphState(TypedDict, total=False):
    """§10.2 Graph State：全部字段均可序列化，运行时对象不进入。"""

    # Identity（§10.1）
    user_id: UUID
    thread_id: UUID
    turn_id: UUID
    request_id: str
    run_id: str
    expected_thread_version: int

    # Context（§10.2）
    conversation_context: dict[str, Any]
    snapshot: dict[str, Any]
    snapshot_hash: str

    # Planning
    rewrite_plan: dict[str, Any]
    plan_revision: int
    # §10.3 Reducer：去重合并（第二轮改写禁止重复旧查询）
    executed_query_fingerprints: Annotated[list[str], merge_executed_query_fingerprints]

    # Retrieval
    embedded_queries: dict[str, list[float]]
    # §10.3 Map Reducer：相同 Worker Key 覆盖而非重复追加
    worker_results: Annotated[dict[str, dict[str, Any]], merge_worker_results]
    # 聚合中间结果（§13.1：evidence_hits + matched_subquery_ids 供 rerank 消费）
    evidence_hits: list[dict[str, Any]]
    matched_subquery_ids: dict[str, list[str]]
    evidence_set: dict[str, Any]

    # Evaluation
    evidence_assessment: dict[str, Any]
    retrieval_iteration: int
    # C6（评审）：补检索预算上限（builder 注入，§14.3）
    _max_retrieval_iterations: int
    # P2（第三轮评审）：citation.available 只发一次（补检索重跑时不重复）
    _citations_emitted: bool

    # Answer
    answer_buffer: str
    answer_payload: dict[str, Any]
    citation_validation: dict[str, Any]

    # Control（§10.2）
    deadline: datetime | None
    cancel_requested: bool
    # 降级标记去重合并（§17.4.1）
    degraded_flags: Annotated[list[str], merge_degraded_flags]
    errors: list[dict[str, Any]]

    # Persistence（§10.2）
    assistant_message_id: str | None
    source_checkpoint_id: str | None
    outbox_event_id: str | None


# ---------------------------------------------------------------------------
# 确定性 Worker Key（§10.3）
# ---------------------------------------------------------------------------


def worker_key(plan_revision: int, subquery_id: str) -> str:
    return f"{plan_revision}:{subquery_id}"


def merge_worker_results(
    current: dict[str, dict[str, Any]], update: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Map Reducer：相同 Worker Key 覆盖而非重复追加（§10.3）。

    旧 plan_revision 结果保留用于查询去重，但不自动混入新一轮最终证据
    （证据集在 aggregate 时按当前 revision 过滤）。
    """
    merged = dict(current)
    merged.update(update)
    return merged


def merge_executed_query_fingerprints(current: list[str], update: list[str]) -> list[str]:
    """去重合并已执行查询指纹（第二轮改写禁止重复旧查询，§11.2 #8）。"""
    seen = set(current)
    merged = list(current)
    for item in update:
        if item not in seen:
            seen.add(item)
            merged.append(item)
    return merged


def merge_degraded_flags(current: list[str], update: list[str]) -> list[str]:
    """去重合并降级标记，保持稳定顺序。"""
    merged = list(current)
    for item in update:
        if item not in merged:
            merged.append(item)
    return merged


# ---------------------------------------------------------------------------
# 输入校验辅助（§11.1 规范化）
# ---------------------------------------------------------------------------


def normalize_plan_mode(plan: RewritePlan, *, max_subqueries: int) -> RewritePlan:
    """服务端规范化（§11.1）：need_retrieval=true ↔ answer_mode=rag 强制一致。"""
    need_retrieval = plan.need_retrieval
    answer_mode = plan.answer_mode
    if need_retrieval:
        answer_mode = "rag"
    elif answer_mode == "rag":
        need_retrieval = True
    if not need_retrieval and answer_mode == "direct":
        answer_mode = "memory_assisted" if plan.memory_trigger != "none" else "direct"
    subqueries = plan.subqueries[:max_subqueries]
    return RewritePlan(
        schema_version=plan.schema_version,
        plan_revision=plan.plan_revision,
        standalone_question=plan.standalone_question,
        answer_mode=answer_mode,
        need_retrieval=need_retrieval,
        memory_trigger=plan.memory_trigger,
        topic_hints=plan.topic_hints,
        subqueries=subqueries,
        reason_codes=plan.reason_codes,
    )


# ---------------------------------------------------------------------------
# Runtime Context（§10.4：不进入 Checkpoint）
# ---------------------------------------------------------------------------


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        from datetime import UTC

        return datetime.now(UTC)


class IdGenerator(Protocol):
    def new_uuid(self) -> UUID: ...


class SystemIdGenerator:
    def new_uuid(self) -> UUID:
        from uuid import uuid4

        return uuid4()


class ConversationRuntimeContext:
    """依赖注入容器（§10.4）。字段在 composition root 装配。"""

    def __init__(
        self,
        *,
        openai_gateway: Any,
        memory_gateway: Any,
        embedding_gateway: Any,
        retriever_gateway: Any,
        conversation_repository: Any,
        turn_event_writer: Any,
        clock: Clock,
        id_generator: IdGenerator,
        logger: logging.Logger,
        flags: dict[str, bool] | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.openai_gateway = openai_gateway
        self.memory_gateway = memory_gateway
        self.embedding_gateway = embedding_gateway
        self.retriever_gateway = retriever_gateway
        self.conversation_repository = conversation_repository
        self.turn_event_writer = turn_event_writer
        self.clock = clock
        self.id_generator = id_generator
        self.logger = logger
        self.flags = flags or {}
        self.worker_id = worker_id
        # 可空辅助对象：production 由 composition root 装配；单测可直注
        self.context_service: Any = None
        self.settings: Any = None
        self.token_counter: Any = None


def serialize_snapshot(snapshot: TurnContextSnapshot) -> dict[str, Any]:
    """快照序列化（进入 Checkpoint 的为可序列化 dict）。"""
    import dataclasses

    return dataclasses.asdict(snapshot)


def snapshot_from_dict(data: dict[str, Any]) -> TurnContextSnapshot:
    """从 Checkpoint 反序列化恢复快照（§9.3：禁止重建 Memory 读取）。"""

    from backend.conversation.contracts.graph import (
        SnapshotBudgets,
        SnapshotMemory,
        SnapshotMessage,
    )

    fields = dict(data)
    fields["memory"] = SnapshotMemory(**fields.get("memory", {}))
    fields["budgets"] = SnapshotBudgets(**fields.get("budgets", {}))
    fields["recent_messages"] = [SnapshotMessage(**m) for m in fields.get("recent_messages", [])]
    return TurnContextSnapshot(**fields)
