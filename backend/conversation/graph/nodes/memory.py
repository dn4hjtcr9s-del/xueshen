"""recall_memory 节点（方案 §5.2 / §9.3 #2 / §16；memory-rebuild §2.4 D1/D2）。

两种模式，由 `memory_prime` flag 切换，**关闭时行为与改造前逐字一致**：

- **关闭（默认）**：本轮唯一一次 Memory 读取，query seed = 当前问题 + 最近用户消息摘要。
  这是既有路径，SSE/answer 契约完全不变。
- **开启**：不再按 query 猜测该注入什么，改为
  **首轮 prime 注入（摘要 + 注册表目录）→ 之后原样复用同一份固定提示词**。
  机制检索交给 `memory.search` / `memory.read` 工具（§2.4 D3）。

**首轮判定**：`conversation_context.recent_messages` 为空即首轮。context 节点在组装时
已把**当前**用户消息排除在外（附录 A.5），所以"空"精确等于"该 thread 没有历史消息"，
不需要新增标记位（§2.4 D1）。

**pin 与每轮取回（§2.4 D2 + 2026-09-11 用户裁决 A）**：prime **每轮都重新取一次**
（一次文件读 + 一次索引查询，代价可忽略），因为 graph thread 是 `conv-turn:{turn_id}`，
checkpoint 不跨轮，而 rollout 的 `memory_prime` 记录按 §1.5「大对象放引用」只存 hash 与
条目数——**读过它也无法还原摘要正文**。若只信 pin，非首轮注入的就是空 prime（比关 flag
还差，且不会报错）。

因此 rollout 的 pin 记录只承担**审计与变更检测**：首轮写入一条；后续轮发现 summary
hash 与 pin 不一致时再写一条（说明"这个 thread 中途换了摘要"），一致则不写。提示词因此
以"每轮最新摘要"为准，代价是跨日长 thread 可能换用新摘要——用户已确认接受。

失败分类（§16.2 / 评审 P1-5）：认证/权限与 4xx 契约错误 → 抛错使 Turn 失败；
5xx/超时/网络 → unavailable 快照继续本轮对话。
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from backend.conversation.contracts.errors import MemoryUnavailableError
from backend.conversation.graph.state import ConversationRuntimeContext
from backend.conversation.rollout.recorder import record_rollout


async def recall_memory(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
) -> dict[str, Any]:
    """读取长期记忆（§16.1）。"""
    flags = runtime.flags
    if flags.get("memory_prime"):
        return await _recall_via_prime(state, runtime=runtime)

    # ---- 既有路径（flag 关闭时逐字不变）----
    if not flags.get("memory_read", True):
        return {"memory_context": {"status": "unavailable"}}
    query_seed = _build_query_seed(state)
    try:
        context = await runtime.memory_gateway.build_learning_context(
            query=query_seed,
            token_budget=None,  # 预算在快照构建后由 ContextService 固化（§9.2）
            user_id=str(state["user_id"]),
        )
    except MemoryUnavailableError as exc:
        # 第三轮必改 4：401/403 与 4xx 契约错误必须 Turn 失败，不静默降级
        # （gateway 透传 source_http_status；无状态按 5xx/降级处理）。
        source_status = exc.source_http_status
        if source_status is not None and source_status < 500:
            runtime.logger.warning("Memory 读取被拒绝（不可降级）: http=%s", source_status)
            raise
        # 5xx/超时/网络（或未知）：unavailable 快照继续本轮对话（§16.2）
        await _emit_degraded(runtime, state, "memory_unavailable")
        return {"memory_context": {"status": "unavailable"}}
    except Exception:
        # 未知异常：unavailable 继续（§16.2）
        await _emit_degraded(runtime, state, "memory_unavailable")
        return {"memory_context": {"status": "unavailable"}}
    memory_status = "degraded" if context.get("truncated") else "available"
    if memory_status == "degraded":
        await _emit_degraded(runtime, state, "memory_degraded")
    context["status"] = memory_status
    return {"memory_context": context}


# ---------------------------------------------------------------------------
# prime 模式（§2.4 D1/D2）
# ---------------------------------------------------------------------------


def is_first_turn(state: dict[str, Any]) -> bool:
    """该 conversation thread 是否还没有历史消息。

    context 节点组装 recent_messages 时已排除当前用户消息，因此"空列表"就是
    "首轮"的精确判据（§2.4 D1：不新增标记位）。
    """
    conversation_context = state.get("conversation_context") or {}
    return not (conversation_context.get("recent_messages") or [])


async def _recall_via_prime(
    state: dict[str, Any], *, runtime: ConversationRuntimeContext
) -> dict[str, Any]:
    """prime 模式：每轮取最新 prime；首轮 pin，后续轮按 hash 变化补 pin（见模块 docstring）。"""
    prime = await _build_prime(runtime, state)
    if is_first_turn(state):
        await _pin_prime(runtime, state, prime)
    else:
        await _refresh_pin_if_changed(runtime, state, prime)
    if prime.get("degraded"):
        await _emit_degraded(runtime, state, "memory_prime_degraded")
    return {
        "memory_prime": prime,
        "memory_context": {
            # 快照 status 只表达"注入的内容是否被截断"：summary 缺失（当前没有生产者）
            # 是空状态而不是内容退化，由 `memory_prime_degraded` 事件标记记录可观测性。
            "status": "degraded" if prime.get("summary_truncated") else "available",
            "prime": prime,
            "truncated": bool(prime.get("summary_truncated")),
            # prime 模式下不做 query 检索，recommendations 保持为空，
            # 由工具按需下沉（§2.4 D3）
            "recommendations": [],
        },
    }


async def _build_prime(
    runtime: ConversationRuntimeContext, state: dict[str, Any]
) -> dict[str, Any]:
    """向 memory 侧取 prime（摘要 + 注册表目录）。"""
    try:
        prime = await runtime.memory_gateway.build_memory_prime(user_id=str(state["user_id"]))
    except MemoryUnavailableError as exc:
        source_status = exc.source_http_status
        if source_status is not None and source_status < 500:
            runtime.logger.warning("Memory prime 被拒绝（不可降级）: http=%s", source_status)
            raise
        await _emit_degraded(runtime, state, "memory_prime_unavailable")
        return {"summary": "", "index_entries": [], "degraded": True}
    return dict(prime)


async def _pin_prime(
    runtime: ConversationRuntimeContext, state: dict[str, Any], prime: dict[str, Any]
) -> None:
    """把首轮 prime 固化为该 thread 的固定提示词（§2.4 D2）。

    rollout 未启用时 `record_rollout` 直接 no-op，因此这里不需要分支判断。
    """
    await record_rollout(
        runtime,
        "memory_prime",
        {
            "summary_hash": str(prime.get("summary_hash") or _summary_hash(prime)),
            "schema_version": str(prime.get("schema_version") or "v1"),
            "generated_at": prime.get("generated_at"),
            "truncated": bool(prime.get("summary_truncated")),
            "index_entry_count": len(prime.get("index_entries") or []),
        },
    )


def _summary_hash(prime: dict[str, Any]) -> str:
    """summary 无哈希时用其内容现算，保证 pin 记录有稳定的比对键。"""
    return hashlib.sha256(str(prime.get("summary") or "").encode("utf-8")).hexdigest()


async def _refresh_pin_if_changed(
    runtime: ConversationRuntimeContext, state: dict[str, Any], prime: dict[str, Any]
) -> None:
    """非首轮：pin 的 hash 与最新 prime 不一致时补一条 `memory_prime` 记录。

    pin 不是提示词来源（见模块 docstring），只用于审计"这个 thread 中途换过摘要"。
    取不到 pin（rollout 未启用、段被 retention 清理、读回失败）时**什么都不做**：
    提示词已经是最新 prime，缺一条审计记录不是用户可见的降级，不该发降级标记。
    """
    pinned = await _load_pinned_prime(runtime, state)
    if pinned is None:
        return
    current_hash = _summary_hash(prime)
    if str(pinned.get("summary_hash") or "") == current_hash:
        return
    runtime.logger.info("thread prime 摘要已更新，补记 pin 记录")
    await _pin_prime(runtime, state, prime)


async def _load_pinned_prime(
    runtime: ConversationRuntimeContext, state: dict[str, Any]
) -> dict[str, Any] | None:
    """读取该 thread **最后一条** `memory_prime` 记录（审计/变更检测用，不做提示词来源）。

    返回 None 的情形：rollout 未启用、首轮段已被 retention 清理、或读回失败。

    **已知限制（已登记 DEV-015）**：rollout 的 `memory_prime` payload 按 §1.5
    「大对象放引用」只存摘要级信息（hash + 条目数），摘要**正文**不在其中，因此这里
    只能还原"pin 的指纹"，不能还原提示词内容——这正是 prime 每轮重新取回的原因。
    """
    reader = getattr(runtime, "rollout_reader", None)
    if reader is None:
        return None
    thread_id = state.get("thread_id")
    if thread_id is None:
        return None
    try:
        records = await reader.read_thread_records(UUID(str(thread_id)))
    except Exception:
        runtime.logger.warning("rollout prime 读回失败，按无 pin 处理", exc_info=True)
        return None
    latest: dict[str, Any] | None = None
    for record in records:
        if record.type != "memory_prime":
            continue
        latest = {
            "pinned": True,
            "summary_hash": record.payload.get("summary_hash"),
            "schema_version": record.payload.get("schema_version"),
            "generated_at": record.payload.get("generated_at"),
            "summary_truncated": bool(record.payload.get("truncated")),
            "index_entry_count": int(record.payload.get("index_entry_count") or 0),
        }
    return latest


# ---------------------------------------------------------------------------
# 既有辅助
# ---------------------------------------------------------------------------


async def _emit_degraded(
    runtime: ConversationRuntimeContext,
    state: dict[str, Any],
    flag: str,
) -> None:
    """§17.4.1：turn.degraded 事件（第三轮评审 P2：发射点补齐）。

    Memory 降级（unavailable/degraded）时通知前端展示降级状态；
    与检索降级（retrieval_partial/unavailable）共用同一事件类型。
    """
    from backend.conversation.contracts.events import TurnEventWrite

    repo = runtime.conversation_repository
    if repo is None or repo.session_factory is None:
        return
    async with repo.session_factory() as session:
        async with session.begin():
            await runtime.turn_event_writer.append(
                session,
                write=TurnEventWrite(
                    turn_id=state["turn_id"],
                    event_type="turn.degraded",
                    request_id=str(state.get("request_id") or ""),
                    run_id=str(state.get("run_id") or ""),
                    payload={"flags": [flag]},
                ),
            )


def _build_query_seed(state: dict[str, Any]) -> str:
    """query seed：当前问题 + 最近用户消息摘要（§9.3 #2）。"""
    snapshot = state.get("snapshot") or {}
    current = str(
        snapshot.get("current_message")
        or state.get("conversation_context", {}).get("current_message")
        or ""
    )
    recent = state.get("conversation_context", {}).get("recent_messages") or []
    user_parts = [str(m["content"]) for m in recent if m.get("role") == "user"]
    seed = current
    if user_parts:
        seed = f"{seed} {user_parts[-1]}"
    return seed[:500] or "最近对话"
