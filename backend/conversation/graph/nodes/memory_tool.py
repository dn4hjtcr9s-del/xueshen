"""memory_tool 节点：有界、幂等、可审计的记忆工具执行（memory-rebuild §5.7）。

`generate_answer` 把模型请求的工具调用写进 `memory_pending_tool_calls`，本节点
执行它们，再把结果放回 `memory_tool_outputs` 供下一次续写使用。三条硬约束：

1. **有界**：每轮工具总调用上限 `MEMORY_TOOL_CALL_BUDGET`（=6，Phase 0 已定型的
   契约常量）。超限的调用**不执行**，回一条 `budget_exceeded` 工具结果并记
   `memory_tool_budget_exceeded` 降级标记，让模型用已有信息收尾——不抛错、不死循环。
   循环轮数另有独立硬上限（同样 6）：全部命中缓存时调用数不增长，只能靠轮数兜底。
2. **幂等**：同一 turn 内 (tool, 规范化参数) 相同即复用上一次结果，**不重复消耗预算**
   （§5.7「相同输入在同一 turn 内要有幂等/缓存策略」）。
3. **可审计**：每次调用写 rollout 的 `memory_tool_call`（参数摘要），每次结果写
   `memory_tool_result`（状态、条数、文档版本、错误码），**正文不落 rollout**
   （§1.5「大对象放引用」）。引用另存 `memory_citations` 供 finalize 回填。

裁剪：工具结果进入 Graph State 前按 §5.7 做 token/字符上限裁剪，累计预算复用
`conversation_memory_token_budget`（与旧 memory 读取同一预算族）。一旦裁剪即置
`truncated=true` 并附下一步可用的 read hint（`line_offset` 续读位置）。注意 hint 是
在裁剪**之后**追加的固定小额开销（不随正文增长），所以"预算 + 提示"才是最终上限。

错误分类（§16.2）：401/403 视为认证/权限问题 → 直接让 Turn 失败；其余错误（含
`MEMORY_NOT_FOUND` 这类模型参数错误）一律变成工具结果里的错误对象交回模型自行纠正，
并记一次 `memory_tool_degraded`，不阻塞回答。
"""

from __future__ import annotations

import json
from typing import Any

from backend.conversation.contracts.api import MemoryCitation
from backend.conversation.graph.state import ConversationRuntimeContext
from backend.conversation.rollout.recorder import record_rollout

#: 每轮记忆工具总调用上限（§5.7 工具契约；Phase 0 已在 rollout 契约中定型）。
MEMORY_TOOL_CALL_BUDGET = 6

#: `memory.search` 默认返回条数（服务端上限 50，这里取保守值）。
SEARCH_MAX_RESULTS = 10

#: `memory.read` 单次最大行数（服务端上限 500）。
READ_MAX_LINES = 200

#: 工具结果文本的硬字符上限：即使预算充足也不允许单条结果无限膨胀
#: （防止模型用极长的 read 把 Graph State／checkpoint 撑爆）。
RESULT_MAX_CHARS = 8000


async def run_memory_tools(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
) -> dict[str, Any]:
    """执行 `memory_pending_tool_calls`，返回状态增量（§5.7）。"""
    pending = list(state.get("memory_pending_tool_calls") or [])
    if not pending:
        return {"memory_pending_tool_calls": [], "memory_tool_outputs": []}

    records = list(state.get("memory_tool_records") or [])
    citations = list(state.get("memory_citations") or [])
    executed = int(state.get("memory_tool_calls") or 0)
    rounds = int(state.get("memory_tool_rounds") or 0) + 1
    user_id = str(state["user_id"])
    token_counter = runtime.token_counter
    budget_tokens = int(getattr(runtime.settings, "conversation_memory_token_budget", 3000) or 0)
    used_tokens = _used_tokens(records)

    outputs: list[dict[str, Any]] = []
    degraded_flags: list[str] = []
    truncated_any = False
    budget_exceeded = False

    for call in pending:
        tool = str(call.get("name") or "")
        call_id = str(call.get("call_id") or "")
        if call.get("executable") is False:
            # 网关判定这条调用不可执行（arguments 不是合法 JSON 对象 / 缺 call_id 或 name）：
            # 不猜参数、不打网关，直接把可读错误交回模型让它重写调用。
            outputs.append(
                _error_output(call_id, tool, "MEMORY_TOOL_INVALID_ARGUMENT", "工具调用格式不合法")
            )
            records.append(
                _result_record(
                    call_id,
                    tool,
                    _cache_key(tool, {}),
                    status="error",
                    error_code="MEMORY_TOOL_INVALID_ARGUMENT",
                )
            )
            if call_id and tool in _SUPPORTED_TOOLS:
                await _record_result(
                    runtime,
                    call_id,
                    tool,
                    status="error",
                    result_count=0,
                    truncated=False,
                    document_versions=[],
                    error_code="MEMORY_TOOL_INVALID_ARGUMENT",
                )
            degraded_flags.append("memory_tool_degraded")
            continue
        arguments = _normalize_arguments(tool, call.get("arguments"))
        cache_key = _cache_key(tool, arguments)

        cached = _find_cached(records, cache_key)
        if cached is not None:
            # 幂等命中：直接回放上次结果，不消耗预算、不写重复记录。
            outputs.append({"call_id": call_id, "name": tool, "output": cached["output"]})
            continue

        if tool not in _SUPPORTED_TOOLS:
            outputs.append(_error_output(call_id, tool, "MEMORY_TOOL_UNKNOWN", "未知的记忆工具"))
            records.append(_result_record(call_id, tool, cache_key, status="error", output=None))
            continue

        if executed >= MEMORY_TOOL_CALL_BUDGET:
            # 超限：不执行、不推进 call_index（契约 call_index ≤ 6），只回错误结果。
            budget_exceeded = True
            outputs.append(
                _error_output(
                    call_id,
                    tool,
                    "MEMORY_TOOL_BUDGET_EXCEEDED",
                    f"本轮记忆工具调用已达上限 {MEMORY_TOOL_CALL_BUDGET} 次",
                )
            )
            await _record_call(runtime, call_id, tool, call_index=None, arguments=arguments)
            await _record_result(
                runtime,
                call_id,
                tool,
                status="budget_exceeded",
                result_count=0,
                truncated=False,
                document_versions=[],
                error_code="MEMORY_TOOL_BUDGET_EXCEEDED",
            )
            records.append(_result_record(call_id, tool, cache_key, status="budget_exceeded"))
            continue

        executed += 1
        call_index = executed
        await _record_call(runtime, call_id, tool, call_index=call_index, arguments=arguments)

        try:
            payload, result_count, versions, citation = await _invoke(
                runtime, tool=tool, arguments=arguments, user_id=user_id
            )
        except Exception as exc:  # 工具失败一律转成模型可读结果
            if _is_turn_fatal(exc):
                # 认证/权限问题按 §16.2 让 Turn 失败，不做静默降级。
                raise
            code, message = _classify_error(exc)
            runtime.logger.warning("记忆工具调用失败: tool=%s code=%s", tool, code)
            degraded_flags.append("memory_tool_degraded")
            outputs.append(_error_output(call_id, tool, code, message))
            await _record_result(
                runtime,
                call_id,
                tool,
                status="error",
                result_count=0,
                truncated=False,
                document_versions=[],
                error_code=code,
            )
            records.append(
                _result_record(
                    call_id, tool, cache_key, status="error", error_code=code, output=None
                )
            )
            continue

        rendered, truncated, delivered_lines = _render_bounded(
            payload,
            tool=tool,
            used_tokens=used_tokens,
            budget_tokens=budget_tokens,
            token_counter=token_counter,
        )
        if truncated:
            truncated_any = True
            rendered = _attach_read_hint(
                rendered,
                tool=tool,
                arguments=arguments,
                payload=payload,
                delivered_lines=delivered_lines,
            )
        used_tokens += _count_tokens(rendered, token_counter)

        if citation is not None:
            citations.append(citation)

        await _record_result(
            runtime,
            call_id,
            tool,
            status="ok",
            result_count=result_count,
            truncated=truncated,
            document_versions=versions,
            error_code=None,
        )
        records.append(
            _result_record(
                call_id,
                tool,
                cache_key,
                status="ok",
                output=rendered,
                truncated=truncated,
                output_tokens=_count_tokens(rendered, token_counter),
            )
        )
        outputs.append({"call_id": call_id, "name": tool, "output": rendered})

    if budget_exceeded:
        degraded_flags.append("memory_tool_budget_exceeded")
    if truncated_any:
        degraded_flags.append("memory_tool_truncated")

    return {
        "memory_pending_tool_calls": [],
        "memory_tool_outputs": outputs,
        "memory_tool_calls": executed,
        "memory_tool_rounds": rounds,
        "memory_tool_records": records,
        "memory_citations": citations,
        "memory_truncated": bool(state.get("memory_truncated")) or truncated_any,
        "degraded_flags": degraded_flags,
    }


def should_continue_memory_tools(state: dict[str, Any]) -> bool:
    """回答后是否进入/继续工具循环（§5.7：有界，两个上限都不允许突破）。"""
    pending = state.get("memory_pending_tool_calls") or []
    if not pending:
        return False
    if int(state.get("memory_tool_rounds") or 0) >= MEMORY_TOOL_CALL_BUDGET:
        return False
    return int(state.get("memory_tool_calls") or 0) < MEMORY_TOOL_CALL_BUDGET


# ---------------------------------------------------------------------------
# 工具调用
# ---------------------------------------------------------------------------

_SUPPORTED_TOOLS = frozenset({"memory.search", "memory.read"})


class _TurnFatalToolError(Exception):
    """认证/权限类工具失败（§16.2：必须让 Turn 失败，不得降级）。"""


def _is_turn_fatal(exc: Exception) -> bool:
    """401/403（认证/权限）必须让 Turn 失败；其余错误降级为可读的工具结果。"""
    from backend.conversation.contracts.errors import MemoryUnavailableError

    if isinstance(exc, _TurnFatalToolError):
        return True
    if isinstance(exc, MemoryUnavailableError):
        return exc.source_http_status in (401, 403)
    return False


async def _invoke(
    runtime: ConversationRuntimeContext,
    *,
    tool: str,
    arguments: dict[str, Any],
    user_id: str,
) -> tuple[dict[str, Any], int, list[str], dict[str, Any] | None]:
    """调用一个记忆工具，返回 (结果体, 结果条数, 文档版本列表, citation 或 None)。"""
    gateway = runtime.memory_gateway
    if tool == "memory.search":
        result = await gateway.search_memories(
            queries=list(arguments.get("queries") or []),
            match_mode=str(arguments.get("match_mode") or "any"),
            max_results=int(arguments.get("max_results") or SEARCH_MAX_RESULTS),
            user_id=user_id,
        )
        items = list(result.get("items") or [])
        versions = [str(item.get("version")) for item in items if item.get("version") is not None]
        return result, len(items), versions, None

    result = await gateway.read_memory(
        memory_id=str(arguments.get("memory_id") or ""),
        line_offset=int(arguments.get("line_offset") or 0),
        max_lines=int(arguments.get("max_lines") or READ_MAX_LINES),
        user_id=user_id,
    )
    citation = _build_citation(result)
    version = str(result.get("version"))
    return result, 1, [version], citation


def _optional_str(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _build_citation(result: dict[str, Any]) -> dict[str, Any] | None:
    """read 结果 → MemoryCitation（§5.7：可回查到 version/checksum/行范围）。"""
    try:
        return MemoryCitation(
            memory_id=str(result.get("memory_id") or ""),
            # read 响应契约没有 name 字段（只有 memory_id/version/checksum/正文），
            # 因此名称缺省为 None，不伪造一个看起来像名字的 memory_id。
            name=_optional_str(result.get("name")),
            version=int(result.get("version") or 0),
            checksum=str(result.get("checksum") or ""),
            line_offset=int(result.get("line_offset") or 0),
            total_lines=int(result.get("total_lines") or 0),
            truncated=bool(result.get("truncated")),
        ).model_dump(mode="json")
    except Exception:
        # 契约不符时不登记 citation，但读取本身仍可用（citation 缺失不阻塞回答）。
        return None


def _classify_error(exc: Exception) -> tuple[str, str]:
    """工具异常 → (稳定错误码, 给模型看的简短说明)。

    `MEMORY_NOT_FOUND` 单独成码：模型引用一个不存在/已删除的 memory_id 是最常见的
    正常纠错场景，与"参数不合法"要能区分（401/403 已在上游按 Turn 失败处理）。
    """
    from backend.conversation.contracts.errors import MemoryUnavailableError

    if isinstance(exc, MemoryUnavailableError):
        status = exc.source_http_status
        if status == 404:
            return "MEMORY_TOOL_NOT_FOUND", "记忆不存在或已被删除"
        if status is not None and 400 <= status < 500:
            return "MEMORY_TOOL_INVALID_ARGUMENT", "工具参数不合法，请修正后重试"
        return "MEMORY_TOOL_UNAVAILABLE", "记忆服务暂时不可用"
    return "MEMORY_TOOL_FAILED", "记忆工具调用失败"


# ---------------------------------------------------------------------------
# 参数规范化 / 缓存键
# ---------------------------------------------------------------------------


def _normalize_arguments(tool: str, raw: Any) -> dict[str, Any]:
    """把模型给的参数收敛成固定形状：只保留该工具认识的键，并做边界裁剪。"""
    if isinstance(raw, str):
        raw = _parse_json_object(raw)
    if not isinstance(raw, dict):
        raw = {}
    if tool == "memory.search":
        queries = [str(q).strip() for q in (raw.get("queries") or []) if str(q).strip()]
        match_mode = str(raw.get("match_mode") or "any")
        if match_mode not in ("any", "all"):
            match_mode = "any"
        max_results = _clamp_int(raw.get("max_results"), default=SEARCH_MAX_RESULTS, low=1, high=50)
        return {"queries": queries, "match_mode": match_mode, "max_results": max_results}
    if tool == "memory.read":
        return {
            "memory_id": str(raw.get("memory_id") or ""),
            "line_offset": _clamp_int(raw.get("line_offset"), default=0, low=0, high=10_000_000),
            "max_lines": _clamp_int(raw.get("max_lines"), default=READ_MAX_LINES, low=1, high=500),
        }
    # 未知工具：保留原始形状用于缓存键，调用时会直接失败。
    return {"raw": raw}


def _parse_json_object(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _clamp_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _cache_key(tool: str, arguments: dict[str, Any]) -> str:
    """幂等键：工具名 + 规范化参数的稳定序列化。"""
    return f"{tool}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"


def _find_cached(records: list[dict[str, Any]], cache_key: str) -> dict[str, Any] | None:
    for record in records:
        if record.get("cache_key") == cache_key and record.get("status") == "ok":
            return record
    return None


# ---------------------------------------------------------------------------
# 结果渲染与裁剪
# ---------------------------------------------------------------------------


def _render_result(payload: dict[str, Any], *, result_count: int) -> tuple[str, bool]:
    """工具结果 → 模型可读 JSON 文本；返回 (文本, 服务端已标记的截断)。"""
    truncated = bool(payload.get("truncated")) or bool(payload.get("summary_truncated"))
    return json.dumps(payload, ensure_ascii=False, sort_keys=True), truncated


def _render_bounded(
    payload: dict[str, Any],
    *,
    tool: str,
    used_tokens: int,
    budget_tokens: int,
    token_counter: Any,
) -> tuple[str, bool, int | None]:
    """渲染 + 有界裁剪，返回 (文本, 是否被截断, 已投递给模型的正文行数)。

    **read 结果按行裁剪**（review I-4）：`memory.read` 的后续读取位置由 `line_offset`
    决定，若先按字符/预算把 JSON 截断再"按完整 content 算行数"，提示会指向
    "服务端取到的末行 +1"，而那些行**从未投递给模型** → 中间整段永远读不到。
    因此这里先二分出"能放进预算的最大正文行数"，用裁剪后的 content 重新渲染，
    提示再按**已投递行数**给下一步偏移。非 read 工具保持原有的字符/预算裁剪。
    """
    if tool == "memory.read":
        lines = str(payload.get("content") or "").splitlines()
        total = len(lines)
        delivered = _max_deliverable_lines(
            payload,
            lines=lines,
            used_tokens=used_tokens,
            budget_tokens=budget_tokens,
            token_counter=token_counter,
        )
        if delivered < total:
            if delivered == 0 and lines:
                # 预算连一行都放不下：回最小结果而不是"0 行的 read"——后者会让模型以为
                # 该主题是空的、或反复用同一个 line_offset 重试。
                return _budget_exhausted_stub(), True, 0
            bounded = dict(payload)
            bounded["content"] = "\n".join(lines[:delivered])
            bounded["truncated"] = True
            rendered = json.dumps(bounded, ensure_ascii=False, sort_keys=True)
            return rendered, True, delivered
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return rendered, bool(payload.get("truncated")), total

    rendered, truncated = _render_result(payload, result_count=0)
    rendered, char_truncated = _apply_char_cap(rendered)
    rendered, token_truncated = _apply_token_budget(
        rendered,
        used_tokens=used_tokens,
        budget_tokens=budget_tokens,
        token_counter=token_counter,
    )
    return rendered, truncated or char_truncated or token_truncated, None


def _max_deliverable_lines(
    payload: dict[str, Any],
    *,
    lines: list[str],
    used_tokens: int,
    budget_tokens: int,
    token_counter: Any,
) -> int:
    """二分出"渲染后同时满足字符上限与剩余 token 预算"的最大正文行数。"""
    remaining_tokens = budget_tokens - used_tokens if budget_tokens > 0 else None
    low, high = 0, len(lines)
    best = 0
    while low <= high:
        mid = (low + high) // 2
        candidate = dict(payload)
        candidate["content"] = "\n".join(lines[:mid])
        rendered = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
        fits_chars = len(rendered) <= RESULT_MAX_CHARS
        fits_tokens = remaining_tokens is None or _count_tokens(rendered, token_counter) <= (
            remaining_tokens
        )
        if fits_chars and fits_tokens:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def _apply_char_cap(rendered: str) -> tuple[str, bool]:
    """单条结果硬字符上限（防止一次 read 把 checkpoint 撑爆）。"""
    if len(rendered) <= RESULT_MAX_CHARS:
        return rendered, False
    return rendered[:RESULT_MAX_CHARS], True


def _apply_token_budget(
    rendered: str,
    *,
    used_tokens: int,
    budget_tokens: int,
    token_counter: Any,
) -> tuple[str, bool]:
    """累计预算裁剪：预算耗尽时把结果压到最小可用形态（不返回空串）。"""
    if budget_tokens <= 0:
        return rendered, False
    remaining = budget_tokens - used_tokens
    if remaining <= 0:
        return _budget_exhausted_stub(), True
    if _count_tokens(rendered, token_counter) <= remaining:
        return rendered, False
    # 粗粒度二分：按字符比例估算，再逐次收敛，避免按 token 精确切分中文时的歧义。
    low, high = 0, len(rendered)
    while low < high:
        mid = (low + high + 1) // 2
        if _count_tokens(rendered[:mid], token_counter) <= remaining:
            low = mid
        else:
            high = mid - 1
    return rendered[:low], True


def _budget_exhausted_stub() -> str:
    """预算耗尽时的最小结果：保留可解析的 JSON 外壳与提示。

    刻意**不带**原结果参数：这个 stub 是"压到最小形态"，任何来自原结果的内容都会
    把"有界"这条不变量重新打开（也避免留一个永远不用的形参）。
    """
    return json.dumps(
        {
            "truncated": True,
            "reason": "memory_tool_budget_exhausted",
            "hint": "本轮记忆工具内容预算已用尽，请基于已有信息回答",
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _attach_read_hint(
    rendered: str,
    *,
    tool: str,
    arguments: dict[str, Any],
    payload: dict[str, Any],
    delivered_lines: int | None = None,
) -> str:
    """裁剪后附下一步可用的 read hint（§5.7：超限必须给出下一步提示）。"""
    hinted = _parse_json_object(rendered)
    if not isinstance(hinted, dict) or not hinted:
        hinted = {"truncated": True, "excerpt": rendered}
    hint: dict[str, Any] = {"truncated": True}
    if tool == "memory.read":
        consumed = int(payload.get("line_offset") or arguments.get("line_offset") or 0)
        # **已投递**的行数：`_render_bounded` 按行裁剪后回传，绝不能用完整 content 的行数
        # （那会把"从未投递的行"算成已读，模型永远跳过它们）。
        if delivered_lines is not None:
            returned = delivered_lines
        else:
            returned = len(str(payload.get("content") or "").splitlines())
        memory_id = str(payload.get("memory_id") or arguments.get("memory_id") or "")
        if delivered_lines == 0:
            # review-2 新发现 11：预算连一行都放不下时 `_render_bounded` 回最小 stub，
            # 此时 `consumed + 0` 恰好等于**请求的同一 offset**——提示会变成"再读一次同一
            # 位置"，模型照着做只会命中幂等缓存、白白消耗剩余轮数。因此这里不给可执行的
            # offset，改为明确的"本轮预算耗尽、勿重试"语义（仍是合法 JSON + truncated=true）。
            hinted["reason"] = "memory_tool_budget_exhausted"
            hint["hint"] = {
                "tool": "memory.read",
                "memory_id": memory_id,
                "retryable": False,
                "note": ("本轮记忆工具内容预算已耗尽，请勿重复读取同一位置；请基于已有信息作答"),
            }
        else:
            hint["hint"] = {
                "tool": "memory.read",
                "memory_id": memory_id,
                "line_offset": consumed + returned,
                "max_lines": int(arguments.get("max_lines") or READ_MAX_LINES),
            }
    else:
        hint["hint"] = {
            "tool": "memory.read",
            "note": "搜索结果只含注册表条目；需要正文请用 memory.read",
        }
    hinted.update(hint)
    return json.dumps(hinted, ensure_ascii=False, sort_keys=True)


def _count_tokens(text: str, token_counter: Any) -> int:
    if token_counter is None:
        return 0
    return int(token_counter.count(text))


def _used_tokens(records: list[dict[str, Any]]) -> int:
    """已消耗的预算 = 各条已回传给模型的结果 token 之和（按记录里的快照计）。"""
    return sum(int(record.get("output_tokens") or 0) for record in records)


def _error_output(call_id: str, tool: str, code: str, message: str) -> dict[str, Any]:
    """错误也是**工具结果**：交回模型自行纠正，不抛错、不阻塞回答。"""
    return {
        "call_id": call_id,
        "name": tool,
        "output": json.dumps(
            {"error": {"code": code, "message": message}}, ensure_ascii=False, sort_keys=True
        ),
    }


def _result_record(
    call_id: str,
    tool: str,
    cache_key: str,
    *,
    status: str,
    output: str | None = None,
    truncated: bool = False,
    error_code: str | None = None,
    output_tokens: int = 0,
) -> dict[str, Any]:
    return {
        "call_id": call_id,
        "tool": tool,
        "cache_key": cache_key,
        "status": status,
        "output": output,
        "truncated": truncated,
        "error_code": error_code,
        "output_tokens": output_tokens,
    }


# ---------------------------------------------------------------------------
# rollout 记录（摘要/引用，不落正文）
# ---------------------------------------------------------------------------


async def _record_call(
    runtime: ConversationRuntimeContext,
    call_id: str,
    tool: str,
    *,
    call_index: int | None,
    arguments: dict[str, Any],
) -> None:
    if call_index is None or not call_id:
        # 超预算的调用没有合法 call_index（契约 call_index ∈ [1, 6]），
        # 缺 call_id 的调用也不满足契约（min_length=1）；两种情况都只在
        # result 记录里留痕，不伪造 call 记录。
        return
    if tool not in _SUPPORTED_TOOLS:
        return
    await record_rollout(
        runtime,
        "memory_tool_call",
        {
            "call_id": call_id,
            "tool": tool,
            "call_index": call_index,
            "arguments": arguments,
        },
    )


async def _record_result(
    runtime: ConversationRuntimeContext,
    call_id: str,
    tool: str,
    *,
    status: str,
    result_count: int,
    truncated: bool,
    document_versions: list[str],
    error_code: str | None,
) -> None:
    if tool not in _SUPPORTED_TOOLS or not call_id:
        return
    await record_rollout(
        runtime,
        "memory_tool_result",
        {
            "call_id": call_id,
            "tool": tool,
            "status": status,
            "result_count": result_count,
            "truncated": truncated,
            "document_versions": document_versions[:100],
            "error_code": error_code,
        },
    )
