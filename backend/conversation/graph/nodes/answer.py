"""generate_answer / validate_answer_and_citations 节点（方案 §15）。

- Answer 节点使用与 Rewrite 相同的快照与最终证据集（§5.3 #1/#6）；
- 稳健输出（§15.4/§17.4）：默认非流式——模型只调用一次并先完成结构化校验，
  再由网关把 answer 正文切为应用层 deltas；AnswerDeltaAggregator 聚合后写入
  answer.delta；启用 CONVERSATION_ANSWER_STREAMING 后走真实 token 级流式，
  失败时按已发长度决定回退非流式或保留中断内容；
- 最终 payload 由已校验的完整回答 + 服务端证据集 Citation 构造，不二次调用模型；
- 记忆工具循环（memory-rebuild §5.7）：`memory_tools` flag 开启时向网关下发
  `memory.search` / `memory.read` 定义；模型请求工具就结束本轮，由 `memory_tool`
  节点执行后再以 `previous_response_id` + `tool_outputs` 续写同一回答，最多 6 轮。
  **不变量：已经作为 answer.delta 发出的正文一定出现在最终正文里**——工具轮的
  preamble 正文既照常流出，也通过 `answer_buffer` 带到下一轮拼接（`carried`）。
- 引用规则（§15.3/评审 C3）：payload.citations 一律替换为服务端证据集
  Citation（含确定性 snippet），模型输出的 citation 字段被丢弃；
  正文引用 ID 校验正则匹配服务端生成的十六进制 citation_id（流式模式下正文
  引用为"先展示、流结束后校验"，非法引用在 validate 节点清理并标记降级）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from backend.conversation.contracts.errors import ModelUnavailableError
from backend.conversation.contracts.events import AnswerDeltaAggregator, TurnEventWrite
from backend.conversation.graph.state import ConversationRuntimeContext
from backend.conversation.services.answer_context import build_answer_contract
from backend.conversation.services.token_counter import TokenCounter

# 服务端生成的 citation_id 为 C+12 位十六进制（evidence.py _to_merged）
_CITATION_ID_RE = re.compile(r"\bC[0-9a-f]{12}\b")


@dataclass(frozen=True)
class _AnswerAttempt:
    """一次回答生成的全部输入（两种传输模式共用，便于回退时原样重放）。

    工具参数（tools/previous_response_id/tool_outputs）在**同一轮 turn 内**跨续写
    保持不变：回退到非流式时仍然是同一个工具轮，不能丢参数。
    """

    state: dict[str, Any]
    runtime: ConversationRuntimeContext
    view: dict[str, Any]
    items: list[dict[str, Any]]
    evidence_refs: list[str]
    request_id: str
    run_id: str
    turn_id: Any
    tools: list[dict[str, Any]] | None = None
    previous_response_id: str | None = None
    tool_outputs: list[dict[str, Any]] = field(default_factory=list)
    #: 之前几轮已经流出并进入最终正文的前缀（工具轮 preamble）。
    carried: str = ""


async def generate_answer(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
    context_service: Any,
) -> dict[str, Any]:
    """生成回答（单次模型调用；deltas 持久化 + 服务端 citation 构造）。

    记忆工具开启时本节点可被重复进入：每次进入生成"一轮"输出，模型请求工具时把
    调用交给 `memory_tool` 节点，执行完再回到本节点续写（§5.7 tool-call loop）。
    """
    snapshot = state.get("snapshot") or {}
    evidence_set = state.get("evidence_set") or {}
    items = evidence_set.get("items") or []
    degraded_flags = list(state.get("degraded_flags") or [])
    snapshot_obj = _snapshot_obj(snapshot)
    rewrite_plan = state.get("rewrite_plan") or {}
    answer_contract, evidence_summary, evidence_refs = build_answer_contract(
        current_question=snapshot_obj.current_message,
        standalone_question=str(rewrite_plan.get("standalone_question") or ""),
        rewrite_plan=rewrite_plan,
        snapshot=snapshot_obj,
        evidence_items=items,
        evidence_assessment=state.get("evidence_assessment"),
        token_counter=runtime.token_counter or TokenCounter(),
        total_budget=snapshot_obj.budgets.retrieval_tokens,
    )
    view = context_service.build_answer_view(
        snapshot=snapshot_obj,
        standalone_question=str(rewrite_plan.get("standalone_question") or ""),
        evidence_summary=evidence_summary,
        evidence_refs=evidence_refs,
        degraded_flags=degraded_flags,
        answer_contract=answer_contract.model_dump(mode="json"),
        evidence_assessment=state.get("evidence_assessment"),
        retrieval_decision=rewrite_plan.get("retrieval_decision") or {},
    )

    attempt = _AnswerAttempt(
        state=state,
        runtime=runtime,
        view=view,
        items=items,
        evidence_refs=evidence_refs,
        request_id=str(state.get("request_id") or ""),
        run_id=str(state.get("run_id") or ""),
        turn_id=state["turn_id"],
        tools=_answer_tools(runtime, state),
        previous_response_id=_optional_text(state.get("memory_response_id")),
        tool_outputs=[item for item in (state.get("memory_tool_outputs") or []) if item],
        carried=str(state.get("answer_buffer") or ""),
    )

    if _streaming_answer_supported(runtime):
        return await _generate_answer_streaming(attempt)
    return await _generate_answer_structured(attempt)


def _answer_tools(
    runtime: ConversationRuntimeContext, state: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """本轮下发给模型的记忆工具定义；flag 关闭时返回 None（调用参数逐字不变）。

    优先用网关自己提供的定义（Fake 网关可在单测里给一份最小 spec），否则用 OpenAI
    网关的线上 JSON Schema。
    """
    if not state.get("_memory_tools_enabled"):
        return None
    provider = getattr(runtime.openai_gateway, "memory_tool_specs", None)
    if callable(provider):
        specs = provider()
        return list(specs) if specs else None
    from backend.conversation.gateways.openai import memory_tool_specs

    return memory_tool_specs()


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _streaming_answer_supported(runtime: ConversationRuntimeContext) -> bool:
    """网关是否启用真实流式（无该能力的 Fake/兼容网关一律走非流式）。"""
    gateway = runtime.openai_gateway
    if not hasattr(gateway, "supports_answer_streaming"):
        return False
    return bool(gateway.supports_answer_streaming())


async def _generate_answer_structured(attempt: _AnswerAttempt) -> dict[str, Any]:
    """非流式路径（§15.4）：完整校验后切片，行为与历史一致。

    开启工具时网关返回 `AnswerToolTurn`：工具轮的 preamble 正文同样按应用层切片
    流出（保证"已发出的 delta 必然出现在最终正文里"），工具调用写入 state 交给
    `memory_tool` 节点执行。
    """
    kwargs: dict[str, Any] = {"answer_context": attempt.view}
    if attempt.tools:
        kwargs["tools"] = attempt.tools
        kwargs["previous_response_id"] = attempt.previous_response_id
        kwargs["tool_outputs"] = attempt.tool_outputs or None
    raw = await attempt.runtime.openai_gateway.stream_answer(**kwargs)
    deltas, stream_payload, pending, response_id = _split_answer_result(raw)

    aggregator = _aggregator(attempt.runtime)
    for delta in deltas:
        await _flush_delta(
            attempt.runtime, attempt.turn_id, attempt.request_id, attempt.run_id, delta, aggregator
        )
    await _flush_tail(
        attempt.runtime, attempt.turn_id, attempt.request_id, attempt.run_id, aggregator
    )

    answer_text = attempt.carried + str(stream_payload.get("answer") or "")
    payload = _answer_payload(
        attempt.items,
        attempt.evidence_refs,
        {**stream_payload, "answer": answer_text},
    )
    return {
        "answer_payload": payload,
        "answer_buffer": answer_text,
        **_tool_loop_updates(pending, response_id, attempt),
    }


async def _generate_answer_streaming(attempt: _AnswerAttempt) -> dict[str, Any]:
    """真实流式路径：token 级转发 answer.delta；失败/空输出按已发长度决定回退/保留。

    - 零正文（模型不可用、连接失败、refusal 或自然结束空输出）→ 回退非流式路径；
    - 已发出部分正文后失败 → 保留已发内容，标记 answer_stream_interrupted；
    - incomplete 截断 → 标记 answer_stream_truncated；refusal（部分正文后）→
      标记 answer_stream_refused；引用校验照常进行（validate 节点负责）；
    - 工具轮（§5.7）没有正文是正常形态，**不触发非流式回退**。
    """
    runtime = attempt.runtime
    gateway = runtime.openai_gateway
    try:
        stream = _open_answer_stream(gateway, attempt)
    except (ModelUnavailableError, ValueError) as exc:
        runtime.logger.warning("流式回答不可用，回退非流式: %s", str(exc)[:200])
        return await _generate_answer_structured(attempt)

    aggregator = _aggregator(runtime)
    collected: list[str] = []
    interrupted = False
    try:
        async for delta in stream:
            collected.append(delta)
            await _flush_delta(
                runtime, attempt.turn_id, attempt.request_id, attempt.run_id, delta, aggregator
            )
    except (ModelUnavailableError, TimeoutError) as exc:
        if not collected:
            runtime.logger.warning("流式回答失败且无正文，回退非流式: %s", str(exc)[:200])
            return await _generate_answer_structured(attempt)
        interrupted = True
        runtime.logger.warning("流式回答中断，保留已生成正文: %s", str(exc)[:200])

    pending = list(getattr(stream, "pending_tool_calls", None) or [])
    response_id = _optional_text(getattr(stream, "response_id", None))

    if not collected and not pending:
        # 流自然结束但零正文（refusal / 模型空输出）：与非流式"空结构化输出"同样拒绝。
        runtime.logger.warning("流式回答无正文（refusal/空输出），回退非流式")
        return await _generate_answer_structured(attempt)

    await _flush_tail(runtime, attempt.turn_id, attempt.request_id, attempt.run_id, aggregator)

    degraded_flags = list(attempt.state.get("degraded_flags") or [])
    if interrupted:
        degraded_flags.append("answer_stream_interrupted")
    if getattr(stream, "truncated", False):
        degraded_flags.append("answer_stream_truncated")
    if getattr(stream, "refused", False):
        degraded_flags.append("answer_stream_refused")
    answer_text = attempt.carried + "".join(collected)
    payload = _answer_payload(
        attempt.items,
        attempt.evidence_refs,
        {
            "answer": answer_text,
            "followups": stream.followups,
            "citations": [],
        },
    )
    return {
        "answer_payload": payload,
        "answer_buffer": answer_text,
        "degraded_flags": degraded_flags,
        **_tool_loop_updates(pending, response_id, attempt),
    }


def _open_answer_stream(gateway: Any, attempt: _AnswerAttempt) -> Any:
    """按是否启用工具选择调用参数：关闭时与既有调用**逐字相同**（既有 Fake 兼容）。"""
    if not attempt.tools:
        return gateway.open_answer_stream(answer_context=attempt.view)
    return gateway.open_answer_stream(
        answer_context=attempt.view,
        tools=attempt.tools,
        previous_response_id=attempt.previous_response_id,
        tool_outputs=attempt.tool_outputs or None,
        tool_rounds=int(attempt.state.get("memory_tool_rounds") or 0),
    )


def _split_answer_result(
    raw: Any,
) -> tuple[list[str], dict[str, Any], list[dict[str, Any]], str | None]:
    """兼容两种非流式返回：既有 `(deltas, payload)` 元组与工具轮的 `AnswerToolTurn`。"""
    if isinstance(raw, tuple) and len(raw) == 2:
        deltas, payload = raw
        return list(deltas), dict(payload or {}), [], None
    deltas = list(getattr(raw, "deltas", None) or [])
    payload = dict(getattr(raw, "payload", None) or {})
    pending = [dict(call) for call in (getattr(raw, "pending_tool_calls", None) or [])]
    return deltas, payload, pending, _optional_text(getattr(raw, "response_id", None))


def _tool_loop_updates(
    pending: list[dict[str, Any]], response_id: str | None, attempt: _AnswerAttempt
) -> dict[str, Any]:
    """把本轮的工具请求写进 State（§5.7）；没有工具请求时不写任何字段。"""
    if not pending and response_id is None:
        return {}
    return {
        "memory_pending_tool_calls": pending,
        "memory_response_id": response_id,
        "memory_tool_outputs": [],
    }


def _aggregator(runtime: ConversationRuntimeContext) -> AnswerDeltaAggregator:
    """answer.delta 小窗口聚合（§7.4：默认 64 字符或 100ms 任一条件即 flush）。"""
    return AnswerDeltaAggregator(
        batch_chars=runtime.settings.conversation_sse_delta_batch_chars,
        batch_ms=runtime.settings.conversation_sse_delta_batch_ms,
    )


async def _flush_delta(
    runtime: ConversationRuntimeContext,
    turn_id: Any,
    request_id: str,
    run_id: str,
    text: str,
    aggregator: AnswerDeltaAggregator,
) -> None:
    """单段正文进聚合器；按窗口条件写出 answer.delta 事件。"""
    chunk = aggregator.append(text)
    if chunk is not None:
        await _write_event(
            runtime,
            runtime.conversation_repository,
            turn_id,
            request_id,
            run_id,
            "answer.delta",
            {"text_delta": chunk},
        )


async def _flush_tail(
    runtime: ConversationRuntimeContext,
    turn_id: Any,
    request_id: str,
    run_id: str,
    aggregator: AnswerDeltaAggregator,
) -> None:
    """流结束后 flush 聚合器剩余文本。"""
    tail = aggregator.flush()
    if tail is not None:
        await _write_event(
            runtime,
            runtime.conversation_repository,
            turn_id,
            request_id,
            run_id,
            "answer.delta",
            {"text_delta": tail},
        )


def _answer_payload(
    items: list[dict[str, Any]],
    evidence_refs: list[str],
    stream_payload: dict[str, Any],
) -> dict[str, Any]:
    """由完整回答 + 服务端证据集 Citation 构造最终 payload。"""
    answer_text = str(stream_payload.get("answer") or "")
    # C3：模型生成契约不含 citations，最终引用一律来自服务端证据集。
    server_citations = _server_citations(items, allowed_ids=set(evidence_refs))
    return {
        "answer": answer_text,
        "citations": server_citations,
        "followups": stream_payload.get("followups") or [],
    }


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
    valid_ids = {
        str(citation.get("citation_id") or "")
        for citation in payload.get("citations") or []
        if isinstance(citation, dict)
    }
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


def _server_citations(
    items: list[dict[str, Any]], *, allowed_ids: set[str]
) -> list[dict[str, Any]]:
    """只注入回答预算实际保留的服务端 Citation。"""
    import dataclasses

    citations: list[dict[str, Any]] = []
    for item in items:
        citation = item.get("citation") or {}
        if isinstance(citation, dict):
            serialized = dict(citation)
        elif hasattr(citation, "model_dump"):
            serialized = citation.model_dump(mode="json")
        elif dataclasses.is_dataclass(citation) and not isinstance(citation, type):
            serialized = dataclasses.asdict(citation)
        else:
            continue
        if str(serialized.get("citation_id") or "") in allowed_ids:
            citations.append(serialized)
    return citations
