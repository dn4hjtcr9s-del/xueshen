"""generate_answer / validate_answer_and_citations 节点（方案 §15）。

- Answer 节点使用与 Rewrite 相同的快照与最终证据集（§5.3 #1/#6）；
- 流式输出（§15.4/§17.4）：模型只调用一次，deltas 经 AnswerDeltaAggregator
  聚合（64 字符/100ms，§7.4）后逐批写入持久化 answer.delta 事件；
  最终 payload 由流式聚合的完整回答 + 服务端证据集 Citation 构造，
  不二次调用模型（评审 C2）；
- 引用规则（§15.3/评审 C3）：payload.citations 一律替换为服务端证据集
  Citation（含确定性 snippet），模型输出的 citation 字段被丢弃；
  正文引用 ID 校验正则匹配服务端生成的十六进制 citation_id。
"""

from __future__ import annotations

import re
from typing import Any

from backend.conversation.contracts.events import AnswerDeltaAggregator, TurnEventWrite
from backend.conversation.graph.state import ConversationRuntimeContext

# 服务端生成的 citation_id 为 C+12 位十六进制（evidence.py _to_merged）
_CITATION_ID_RE = re.compile(r"\bC[0-9a-f]{12}\b")


async def generate_answer(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
    context_service: Any,
) -> dict[str, Any]:
    """生成回答（单次模型调用；deltas 持久化 + 服务端 citation 构造）。"""
    snapshot = state.get("snapshot") or {}
    evidence_set = state.get("evidence_set") or {}
    items = evidence_set.get("items") or []
    evidence_summary = "\n".join(_evidence_line(item) for item in items)
    degraded_flags = list(state.get("degraded_flags") or [])
    snapshot_obj = _snapshot_obj(snapshot)
    view = context_service.build_answer_view(
        snapshot=snapshot_obj,
        standalone_question=str(state.get("rewrite_plan", {}).get("standalone_question") or ""),
        evidence_summary=evidence_summary[:8000] or "（无证据）",
        evidence_refs=_evidence_refs(items),
        degraded_flags=degraded_flags,
    )
    request_id = str(state.get("request_id") or "")
    run_id = str(state.get("run_id") or "")
    turn_id = state["turn_id"]

    # 单次模型调用（§15.4）：deltas 是结构化 JSON 片段，payload 是解析后的
    # AnswerPayload（第三轮必改 1：持久化的 answer 必须取 payload.answer，
    # 不能直接 join 原始 JSON 片段）。
    deltas, stream_payload = await runtime.openai_gateway.stream_answer(answer_context=view)

    # 聚合持久化 answer.delta 事件（§7.4：64 字符或 100ms flush）
    aggregator = AnswerDeltaAggregator(
        batch_chars=runtime.settings.conversation_sse_delta_batch_chars,
        batch_ms=runtime.settings.conversation_sse_delta_batch_ms,
    )
    repo = runtime.conversation_repository
    for delta in deltas:
        chunk = aggregator.append(delta)
        if chunk is not None:
            await _write_event(
                runtime,
                repo,
                turn_id,
                request_id,
                run_id,
                "answer.delta",
                {"text_delta": chunk},
            )
    tail = aggregator.flush()
    if tail is not None:
        await _write_event(
            runtime,
            repo,
            turn_id,
            request_id,
            run_id,
            "answer.delta",
            {"text_delta": tail},
        )

    answer_text = str((stream_payload or {}).get("answer") or "")
    # C3：citations 一律来自服务端证据集（模型输出的 citation 字段被丢弃）
    server_citations = _server_citations(items)
    payload: dict[str, Any] = {
        "answer": answer_text,
        "citations": server_citations,
        "followups": (stream_payload or {}).get("followups") or [],
    }
    return {"answer_payload": payload, "answer_buffer": answer_text}


async def _write_event(
    runtime: ConversationRuntimeContext,
    repo: Any,
    turn_id: Any,
    request_id: str,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """持久化 Turn 事件（§17.4；单测无 repo 时跳过）。"""
    if repo is None or repo.session_factory is None:
        return
    async with repo.session_factory() as session:
        async with session.begin():
            await runtime.turn_event_writer.append(
                session,
                write=TurnEventWrite(
                    turn_id=turn_id,
                    event_type=event_type,  # type: ignore[arg-type]
                    request_id=request_id,
                    run_id=run_id,
                    payload=payload,
                ),
            )


async def validate_answer_and_citations(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
) -> dict[str, Any]:
    """引用验证（§15.3/评审 C3）：正文只允许证据集 ID，十六进制匹配。"""
    payload = state.get("answer_payload") or {}
    evidence_set = state.get("evidence_set") or {}
    items = evidence_set.get("items") or []
    valid_ids = set(_evidence_refs(items))
    answer = str(payload.get("answer") or "")
    cited = set(_CITATION_ID_RE.findall(answer))
    invalid = cited - valid_ids
    degraded_flags = list(state.get("degraded_flags") or [])
    if invalid:
        cleaned = answer
        for token in sorted(invalid, key=len, reverse=True):
            cleaned = cleaned.replace(token, "")
        payload = dict(payload)
        payload["answer"] = cleaned
        degraded_flags.append("citation_degraded")
        payload["degraded_flags"] = degraded_flags
    return {"answer_payload": payload, "degraded_flags": degraded_flags}


def _snapshot_obj(snapshot: dict[str, Any]) -> Any:
    from backend.conversation.graph.state import snapshot_from_dict

    return snapshot_from_dict(snapshot)


def _evidence_line(item: dict[str, Any]) -> str:
    """证据行：兼容 dataclass 与 dict 两种形态（checkpoint 恢复后）。"""
    citation = item.get("citation") or {}
    if hasattr(citation, "citation_id"):
        citation_id = citation.citation_id
    else:
        citation_id = citation.get("citation_id", "")
    content = str(item.get("content_text") or "")
    return f"[{citation_id}]\n{content}"


def _evidence_refs(items: list[dict[str, Any]]) -> list[str]:
    """证据 Citation ID 列表：兼容 dataclass 与 dict。"""
    refs: list[str] = []
    for item in items:
        citation = item.get("citation") or {}
        if hasattr(citation, "citation_id"):
            refs.append(citation.citation_id)
        else:
            refs.append(str(citation.get("citation_id", "")))
    return refs


def _server_citations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """服务端证据集 Citation（§13.3）：前端只能渲染这些真实引用。"""
    import dataclasses

    citations: list[dict[str, Any]] = []
    for item in items:
        citation = item.get("citation") or {}
        if isinstance(citation, dict):
            citations.append(dict(citation))
        elif hasattr(citation, "model_dump"):
            citations.append(citation.model_dump(mode="json"))
        elif dataclasses.is_dataclass(citation) and not isinstance(citation, type):
            citations.append(dataclasses.asdict(citation))
    return citations
