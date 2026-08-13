"""retrieve_subquery Worker 节点（方案 §5.2 / §12）。

每个子问题一个独立 Worker：embed（批量在上游节点做）→ retriever 检索 → 结果。
Worker 只处理自己的局部输入输出，不读取其他 Worker 结果（§12.3）。
"""

from __future__ import annotations

from typing import Any

from backend.conversation.graph.state import ConversationRuntimeContext, worker_key


async def retrieve_subquery(
    worker_input: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
) -> dict[str, Any]:
    """单个检索 Worker（§12.3）。

    worker_input: {plan_revision, subquery_id, query_text, query_vector,
                   validated_filters, limit, deadline}
    """
    plan_revision = int(worker_input["plan_revision"])
    subquery_id = str(worker_input["subquery_id"])
    result = await runtime.retriever_gateway.retrieve(
        query_text=str(worker_input["query_text"]),
        query_vector=worker_input.get("query_vector"),
        filters=worker_input.get("validated_filters") or None,
        limit=int(worker_input.get("limit") or 20),
        deadline=worker_input.get("deadline"),
    )
    key = worker_key(plan_revision, subquery_id)
    result["worker_key"] = key
    result["subquery_id"] = subquery_id
    # hits 序列化（dataclass → dict；slots dataclass 无 __dict__，用 asdict；
    # Fake gateway 可能直接返回 dict，原样保留）
    import dataclasses

    hits = result.get("hits") or ()
    if hits and dataclasses.is_dataclass(hits[0]):
        result["hits"] = [dataclasses.asdict(hit) for hit in hits]
    return {"worker_results": {key: result}}


async def embed_subqueries(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
) -> dict[str, Any]:
    """批量 Embedding（§12.1 #4：批量减少外部请求，按子问题关联结果）。"""
    plan = state.get("rewrite_plan") or {}
    subqueries = plan.get("subqueries") or []
    if not subqueries:
        return {"embedded_queries": {}}
    texts = [str(s["query_text"]) for s in subqueries]
    vectors = await runtime.embedding_gateway.embed(texts=texts)
    return {
        "embedded_queries": {str(s["subquery_id"]): vectors[i] for i, s in enumerate(subqueries)}
    }
