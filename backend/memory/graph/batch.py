"""批量总结分支（memory-rebuild §4.2 / §5.8 Phase 6）。

一个 ``summarize_user_memory_batch`` operation 装该用户本批积压的 N 条证据（≤50），
本分支**复用既有 summary 节点链**逐条处理，再进入 consolidation 入口：

```text
load_batch_members
  └─ loop: begin_batch_member →（既有 summary 链：load_source_refs … commit）→ record_batch_member
       ↑                                                                              │
       └────────────────────────── 还有成员 ─────────────────────────────────────────┘
  └─ enter_batch_consolidation → finalize_batch_result → normalize_result
```

三条关键语义：

1. **成员身份 = 成员自己的 operation**：``begin_batch_member`` 把 ``state["operation"]``
   临时替换成成员 operation，因此既有的抽取/计划/提交节点**一行不改**就能按成员粒度工作，
   且 ``commit_plans(operation_id=成员)`` 的 mutation 重放键、``memory_commits.operation_id``
   都自然绑定到该条证据——这正是 §5.8「通过 operation/evidence/version 绑定避免重复写
   同一事实」要求的绑定关系。
2. **重跑安全**：进入成员前先查 ``memory_commits`` 是否已有该成员 operation 的提交记录，
   有则直接跳过（记 ``skipped_already_committed``）。checkpoint 恢复不丢已处理成员；
   即使 checkpoint 被清理后整批重跑，已写入的成员也不会被重复写入。
3. **单成员失败不拖垮整批**（§5.8）：成员体每个节点外面都有一层守卫（``runner`` 里的
   ``_guard_member_node``），任一节点抛非致命异常只写入 ``batch_member_error``，条件边
   短路到 ``record_batch_member``，该成员记 ``batch_failed`` 并继续处理后续成员；
   批次整体仍能走到终态。已 commit 的成员不受影响（它们各自的 operation 绑定保证幂等）。
   致命的"环境不可用"异常（连接失效/超时等）刻意**不**隔离，直接上抛让 Worker 按既有
   retry/backoff 重试——见 ``batch.MEMBER_FATAL_EXCEPTIONS`` 的说明。
4. **fencing 归批次**（评审 C-2）：成员 operation 从未被 claim，真正持有 Lease 的是批次
   operation，因此成员的提交统一把批次 operation_id 当 ``fencing_operation_id`` 传下去
   （``commit_fencing_operation_id``），成员 id 只用于重放键与追溯。

consolidation 末段（重写 summary / 悬空链接与 aliases 治理 / 批内冲突裁决）属 Phase 7，
本 Phase 只交付**入口**：开关关闭时跳过，开关开启但实现未落地时记警告与 degraded 标记
（不静默），见 ``enter_batch_consolidation``。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langgraph.runtime import Runtime
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError

from backend.memory.contracts.errors import (
    DatabaseUnavailableError,
    OperationDeadLetterError,
    StorageUnavailableError,
)
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.graph.prompt_loader import BUILD_MUTATION_PLAN_PROMPT_VERSION
from backend.memory.graph.state import MemoryManagerState, MemoryRuntimeContext
from backend.memory.persistence import operations as ops_repo

logger = logging.getLogger("memory.graph.batch")

#: 批次内部 outcome（成员级），与 operation 状态机不同——它描述"这条证据处理得怎样"。
MEMBER_OUTCOME_SUCCEEDED = "succeeded"
MEMBER_OUTCOME_NO_CHANGE = "no_change"
MEMBER_OUTCOME_NEEDS_REVIEW = "needs_review"
MEMBER_OUTCOME_SKIPPED = "skipped_already_committed"
#: 成员体抛异常（评审 I-9 隔离）：该成员没有写入，但批次继续处理后续成员。
MEMBER_OUTCOME_FAILED = "failed"
#: payload 声明了该成员，但它已取消/已释放/已终态，本批按设计不再处理
#: （评审新发现 14：这不是"归属丢失"，不能与真·数据不一致混为一谈）。
MEMBER_OUTCOME_UNAVAILABLE = "skipped_not_processable"

#: 批次级失败信号（评审新发现 12）：本批**一条都没写**、也没有审核候选，且存在失败成员。
BATCH_ALL_MEMBERS_FAILED = "BATCH_ALL_MEMBERS_FAILED"


class BatchAllMembersFailedError(OperationDeadLetterError):
    """批次内全部成员被隔离、无任何写入（评审新发现 12）。

    单成员失败不拖垮整批（§5.8），但**全部**成员失败时批次不能报 ``succeeded``：
    operation 状态会与实际写入量不符，而"整批一条都没写进去"只藏在 warnings 里。

    继承 :class:`OperationDeadLetterError` 是为了**不改 ``manager.py`` / ``retry.py``
    就生效**：``retry.py::classify_failure`` 对"非 retryable 的 MemoryError"直接判
    ``DEAD_LETTER``，因此批次 operation 落到 ``dead_letter``（人工审）而不是
    ``succeeded``，也不会进 retry 循环。``code`` 单独取一个名字，让
    ``public_error.code`` 能一眼区分"这一批全灭"与其它死信原因。

    取舍（为什么不是只往 ``state["errors"]`` 写一个结构化 code）：``normalize_result``
    目前只识别 ``LLM_BUDGET_EXHAUSTED`` 一个 code（``manager.py`` 不在本轮改动范围），
    只写 code 不改变任何状态，等于"登记了但没生效"；而伪造
    ``LLM_BUDGET_EXHAUSTED`` 会把"预算耗尽"这个真实语义污染掉。代价是异常路径下
    ``MemoryOperationResult.result`` 为 ``None``（逐成员诊断只在图 state / checkpoint
    里），因此异常消息里带上人数与首条失败摘要，保证公开可见面仍有信息量。
    """

    code = BATCH_ALL_MEMBERS_FAILED


#: 批次级失败信号里带出的失败摘要条数上限（避免超长 public_error.message）。
MAX_BATCH_FAILURE_SUMMARY_ITEMS = 2

#: 成员失败原因（进 ``batch_failed[].reason``）与异常摘要长度上限（评审 I-9）。
MEMBER_FAILURE_REASON = "member_exception"
MAX_FAILURE_MESSAGE_CHARS = 200

#: **不允许**被成员级隔离吞掉的异常：直接上抛，交给 Worker 既有的 retry/backoff/dead_letter。
#:
#: 分类依据：这些异常描述的是"当前执行环境不可用"——数据库连接失效、事务因管理指令中止、
#: 存储后端不可达、进程资源耗尽、协程被取消。换一条成员重试同样会失败，把它们当成"成员
#: 失败"只会用一次运行把整批（≤50 条，绝大多数从未被真正尝试）全部判死；上抛让 Lease 回收
#: + 退避重试，重试时已提交的成员由 ``_member_already_committed`` 跳过，语义正确且不丢数据。
#: 其余异常（LLM 输出不合 schema、限流、单条证据解析失败、个别文档版本冲突……）都视为
#: **成员自身**的问题、对后续成员没有传染性，按成员失败隔离并继续。
MEMBER_FATAL_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncio.CancelledError,
    ConnectionError,
    TimeoutError,
    MemoryError,
    RecursionError,
    DatabaseUnavailableError,
    StorageUnavailableError,
    OperationalError,
    InterfaceError,
)


def is_member_fatal(exc: BaseException) -> bool:
    """该异常是否属于"环境不可用"（成员级隔离必须放行）。"""
    return isinstance(exc, MEMBER_FATAL_EXCEPTIONS)


def _member_error(state: MemoryManagerState) -> dict[str, Any]:
    """当前成员的失败信号（由 ``runner._guard_member_node`` 写入；正常情况下没有）。"""
    return dict(state.get("batch_member_error") or {})


def _operation(state: MemoryManagerState) -> MemoryOperation:
    return MemoryOperation.model_validate(state["operation"])


def _operation_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """DB 行 → 契约字段子集（extra="forbid"，必须剔除状态/Lease 等额外列）。"""
    return {key: row[key] for key in MemoryOperation.model_fields if key in row}


# ---------------------------------------------------------------------------
# 加载批次
# ---------------------------------------------------------------------------


async def load_batch_members(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """读取本批成员（稳定序），并把批次自身保存起来供收尾还原。

    payload ↔ DB 归属交叉校验（评审新发现 14）：`list_batch_member_operations` 只返回
    ``status='pending_batch'`` 的行（I-8 的正确过滤），因此"被用户取消 / 已被释放回池子 /
    已终态"的成员也会落进"查不到"的集合。它们与"行根本不存在"是**完全不同**的两件事，
    措辞不能都叫"归属丢失"：

    - 行还在、只是状态或归属变了 → 按设计跳过，记进 ``batch_processed``（中性措辞，
      不进 warnings——它不是异常）；
    - 行不存在 → 真·归属丢失/数据不一致，保留警告并点明需人工核查。
    """
    ctx = runtime.context
    operation = _operation(state)
    async with ctx.session_factory() as session:
        rows = await ops_repo.list_batch_member_operations(
            session, batch_operation_id=operation.operation_id
        )
        payload_ids = _declared_member_ids(operation)
        loaded_ids = {str(row["operation_id"]) for row in rows}
        declared_missing = payload_ids - loaded_ids
        # 只有"声明了却没返回"的才需要回查：一次 ANY 查询，正常批次拿空集
        existing_rows = (
            await ops_repo.list_operations_by_ids(
                session, operation_ids=[UUID(item) for item in declared_missing]
            )
            if declared_missing
            else []
        )
    by_id = {str(row["operation_id"]): row for row in existing_rows}
    members = [
        {
            "operation_id": str(row["operation_id"]),
            "user_id": str(row["user_id"]),
            "operation": _operation_from_row(row),
        }
        for row in rows
    ]
    warnings: list[str] = []
    skipped: list[dict[str, Any]] = []
    inconsistent: list[str] = []
    for member_id in sorted(declared_missing):
        row = by_id.get(member_id)
        if row is None:
            inconsistent.append(member_id)
            continue
        status = str(row.get("status") or "")
        skipped.append(
            {
                "operation_id": member_id,
                "outcome": MEMBER_OUTCOME_UNAVAILABLE,
                "reason": f"成员状态为 {status}（已取消/已释放/已终态），按设计跳过",
            }
        )
        # 不是异常，诊断价值低于 warnings：只留 debug 级痕迹
        logger.debug("批次成员 %s 状态 %s，按设计跳过", member_id, status)
    if inconsistent:
        # 真·不一致：声明了却查不到行，必须让人看见
        warnings.append(
            f"批次 payload 声明的 {len(inconsistent)} 条成员在 memory_operations 中不存在"
            "（归属丢失/数据不一致，需人工核查）"
        )
    if skipped:
        logger.info(
            "批次 %s：%d 条声明成员已取消/已释放，按设计跳过",
            operation.operation_id,
            len(skipped),
        )
    if not members:
        warnings.append("批次没有任何成员，按 no_change 处理")
    return {
        "batch_active": True,
        "batch_operation": dict(state["operation"]),
        "batch_prompt_version": BUILD_MUTATION_PLAN_PROMPT_VERSION,
        "batch_members": members,
        "batch_index": 0,
        "batch_selected": {},
        # 跳过的成员直接进 processed：批次结果要能回答"声明的那几条去哪了"
        "batch_processed": skipped,
        "batch_failed": [],
        "batch_warnings": warnings,
        # 成员循环里每条证据都要重新计数，批次总量单独累计
        "batch_llm_call_count": 0,
    }


def _declared_member_ids(operation: MemoryOperation) -> set[str]:
    """payload 里声明的成员 operation_id（用于与 DB 归属做交叉校验）。"""
    declared = getattr(operation.payload, "member_operation_ids", None)
    if not declared:
        return set()
    return {str(item) for item in declared}


# ---------------------------------------------------------------------------
# 成员循环
# ---------------------------------------------------------------------------


async def begin_batch_member(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """选中下一条成员并投影成 ``state["operation"]``；已提交过的成员直接跳过。

    返回的 ``batch_selected`` 为空表示"没有可处理的成员了"，由条件边决定是继续跳过
    （指针已推进）还是进入 consolidation。
    """
    members = state.get("batch_members") or []
    index = int(state.get("batch_index") or 0)
    processed = list(state.get("batch_processed") or [])
    warnings = list(state.get("batch_warnings") or [])

    while index < len(members):
        member = members[index]
        index += 1
        if await _member_already_committed(runtime, member["operation_id"]):
            processed.append(
                {
                    "operation_id": member["operation_id"],
                    "outcome": MEMBER_OUTCOME_SKIPPED,
                    "reason": "该成员已有提交记录，重跑时跳过以避免重复写入",
                }
            )
            continue
        # 投影成成员 operation：既有 summary 链一行不改地按成员粒度工作（见模块 docstring）
        return {
            "operation": member["operation"],
            "batch_index": index,
            "batch_selected": member,
            "batch_processed": processed,
            "batch_warnings": warnings,
            # 评审 C-2：真正持有 Lease 的是**批次** operation（成员行从未被 claim），
            # 提交时的 CAS 必须打在批次行上；成员 id 仍只用于重放键与可追溯。
            # 批次 operation 缺失（异常装配）时留空，提交节点会退回成员 id。
            "commit_fencing_operation_id": str(
                (state.get("batch_operation") or {}).get("operation_id") or ""
            ),
            # 上一条成员的失败信号绝不能漏进这一条（守卫只在异常时写它）
            "batch_member_error": {},
            # 每条成员重置处理态，避免上一条的候选/证据/结果串到下一条
            "source_bundle": {},
            "candidates": [],
            "candidate_graph_nodes": {},
            "existing_memories": [],
            "mutation_plan_drafts": [],
            "commit_mutation_plans": [],
            "commit_result": {},
            "review_candidates": [],
            "errors": [],
            "warnings": [],
            "replan_count": 0,
            # **每条成员重置 LLM 预算**：`LLMCallBudget` 的上限是"每 operation 4 次"
            # （policies.LLM_MAX_CALLS_PER_OPERATION），而批量的每个成员本身就是一条
            # operation——累计计数会让第 3 条成员起全部被判"预算耗尽"
            # （实测：3 条成员只写出 2 条，且批次仍报 succeeded）。
            # 批次级总量单独记在 `batch_llm_call_count`（审计用，不参与预算判定）。
            "llm_call_count": 0,
        }
    return {
        "batch_index": index,
        "batch_selected": {},
        "batch_processed": processed,
        "batch_warnings": warnings,
    }


async def _member_already_committed(
    runtime: Runtime[MemoryRuntimeContext], member_operation_id: str
) -> bool:
    """该成员是否已有提交记录（``memory_commits.operation_id``，走 ix_memory_commits_operation）。

    只认"写过东西"的成员：零提交的成员（无候选 / 全是审核候选）重跑代价只是再抽取一次，
    而误判为"已处理"会**永久丢掉**这条证据的信息，因此这里宁可重做也不误跳过。
    """
    ctx = runtime.context
    async with ctx.session_factory() as session:
        result = await session.execute(
            text("SELECT 1 FROM memory_commits WHERE operation_id = :operation_id LIMIT 1"),
            {"operation_id": member_operation_id},
        )
        return result.first() is not None


async def record_batch_member(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """记录刚处理完的成员 outcome，并累计批次级计数。

    两条入口：成员体正常走完（按 commit_result / review 候选判定 outcome），或成员体被
    守卫短路（``batch_member_error`` 非空，评审 I-9）——后者记一条 ``failed`` 成员并附
    稳定 reason 与截断到 200 字符的异常摘要，然后清空信号，让循环继续下一条成员。
    """
    selected = state.get("batch_selected") or {}
    if not selected:
        return {"batch_member_error": {}}
    member_error = _member_error(state)
    commit = state.get("commit_result") or {}
    mutations = commit.get("mutations") or []
    review_ids = [str(item.get("candidate_id")) for item in state.get("review_candidates") or []]
    errors = list(state.get("errors") or [])
    if member_error:
        outcome = MEMBER_OUTCOME_FAILED
    elif review_ids and not mutations:
        outcome = MEMBER_OUTCOME_NEEDS_REVIEW
    elif mutations:
        outcome = MEMBER_OUTCOME_SUCCEEDED
    else:
        outcome = MEMBER_OUTCOME_NO_CHANGE
    entry = {
        "operation_id": selected.get("operation_id"),
        "outcome": outcome,
        "mutation_count": len(mutations),
        "review_candidate_count": len(review_ids),
        "replayed": bool(commit.get("replayed")),
        # 明细随 entry 一起留在批次结果里：批次 operation 的结果是唯一能回查
        # "这批到底写了什么"的地方（成员 operation 状态只有 succeeded/dead_letter）。
        "mutations": list(mutations),
        "review_candidate_ids": list(review_ids),
    }
    if errors:
        entry["error_codes"] = [str(item.get("code") or "") for item in errors]
    if member_error:
        # 稳定 reason + 异常摘要（截断 200 字符，避免把堆栈/SQL 灌进批次结果）
        entry["reason"] = str(member_error.get("reason") or MEMBER_FAILURE_REASON)
        entry["error_type"] = str(member_error.get("error_type") or "")
        entry["error_message"] = str(member_error.get("message") or "")[:MAX_FAILURE_MESSAGE_CHARS]
        entry["failed_node"] = str(member_error.get("node") or "")
    elif outcome == MEMBER_OUTCOME_NEEDS_REVIEW:
        entry["reason"] = "needs_review"
    processed = list(state.get("batch_processed") or [])
    processed.append(entry)
    failed = list(state.get("batch_failed") or [])
    if outcome in (MEMBER_OUTCOME_NEEDS_REVIEW, MEMBER_OUTCOME_FAILED) or errors:
        # "未直接写入的成员"：需人工审核或出错。命名沿用 §5.8 的"失败成员"，
        # 但语义包含 needs_review——它们同样不会在批次结果里出现 mutation。
        failed.append({key: value for key, value in entry.items() if key != "mutations"})
    warnings = list(state.get("batch_warnings") or [])
    warnings.extend(str(item) for item in (state.get("warnings") or []))
    # 成员失败的可见警告统一由 `finalize_batch_result` 从 `batch_failed` 生成，
    # 这里不重复追加（否则公开 result 里会出现两条语义相同、措辞不同的警告）。
    return {
        "batch_selected": {},
        "batch_member_error": {},
        "batch_processed": processed,
        "batch_failed": failed,
        "batch_warnings": warnings,
        "batch_llm_call_count": int(state.get("batch_llm_call_count") or 0)
        + int(state.get("llm_call_count") or 0),
    }


# ---------------------------------------------------------------------------
# consolidation 入口（Phase 7 实现末段）
# ---------------------------------------------------------------------------


async def enter_batch_consolidation(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """consolidation 末段入口（§4.2 / §5.9）。

    Phase 6 只交付入口，末段三件事（重写 ``memory_summary.md``、悬空链接/aliases/近义主题
    治理、批内冲突裁决）属 Phase 7。开关关闭时记 ``disabled``；开关开启但实现未落地时
    **记警告 + degraded 标记**，绝不静默假装做过。
    """
    settings = runtime.context.settings
    enabled = bool(getattr(settings, "memory_consolidation_enabled", False))
    if not enabled:
        return {"batch_consolidation": {"status": "disabled"}}
    # 末段实现见 graph/consolidation.py（§5.9①）：summary 重写 + keywords/aliases 治理 +
    # 悬空链接两级制 + KG 双路。失败只告警，不影响循环段已提交的成员。
    from backend.memory.graph import consolidation

    return await consolidation.consolidate_user_memory(state, runtime)


# ---------------------------------------------------------------------------
# 收尾
# ---------------------------------------------------------------------------


async def finalize_batch_result(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """把逐成员结果聚合成批次结果，并还原 ``state["operation"]`` 为批次自身。

    聚合规则：

    - ``mutations`` / ``review_candidate_ids`` 按成员顺序拼接（附 ``member_operation_id``，
      便于从批次结果回查到具体证据）；
    - 只要有成员真正写入，批次就是 ``succeeded``（交给 ``normalize_result`` 判定），
      因此**清空** ``errors``；一条都没写时才把成员错误上抛，让批次进入
      dead_letter / needs_review 判定；
    - ``warnings`` 还原为批次级累计（成员循环里它被逐条重置过）。

    **批次级失败信号**（评审新发现 12）：``mutations`` 与 ``review_candidate_ids`` 都为空、
    而 ``batch_failed`` 非空时，整批成员一个都没写进去——此时抛
    :class:`BatchAllMembersFailedError`（``retryable=False`` 的域错误，经
    ``classify_failure`` 落 ``dead_letter``）。单成员失败照旧不拖垮整批（§5.8），只有
    "全灭"才升级为批次级失败；审核候选（``needs_review``）与"无成员/无变化"不受影响。
    """
    processed = list(state.get("batch_processed") or [])
    failed = list(state.get("batch_failed") or [])
    mutations: list[dict[str, Any]] = []
    review_ids: list[str] = []
    for entry in processed:
        mutations.extend(entry.get("mutations") or [])
        review_ids.extend(entry.get("review_candidate_ids") or [])
    consolidation = state.get("batch_consolidation") or {}
    degraded_flags = list(consolidation.get("degraded_flags") or [])
    commit_result = {
        "mutations": mutations,
        "review_candidate_ids": review_ids,
        "replayed": bool(processed) and all(bool(e.get("replayed")) for e in processed),
        "batch": {
            "member_count": len(state.get("batch_members") or []),
            "processed": processed,
            "failed": failed,
            "llm_call_count": int(state.get("batch_llm_call_count") or 0),
            "prompt_version": str(state.get("batch_prompt_version") or ""),
            "consolidation": consolidation,
        },
    }
    # 被成员级隔离捕获、实际没写成功的成员必须**释放回证据池**（review I-9 的重试出口）：
    # 批次 succeeded 时 settle_batch_members 会把归属成员一律置 succeeded，包括这些失败项，
    # 它们就永远不会再被处理。释放（清 batch_operation_id）后它们不再被那条 UPDATE 命中。
    release = await _release_failed_members(runtime, failed)
    errors = [] if mutations else list(state.get("errors") or [])
    warnings = _cap_warnings(list(state.get("batch_warnings") or []))
    if release.released:
        warnings.append(f"{release.released} 条失败成员已释放回证据池，等待下一次批量重试")
    if release.dead_lettered:
        # 评审新发现 13：这些成员已达到自己的 max_attempts，不再回池子（否则是无终点的
        # 每晚重试），转 dead_letter 交人工审核。
        warnings.append(
            f"{release.dead_lettered} 条失败成员已达 max_attempts，转 dead_letter 交人工审核"
        )
    if failed:
        # 部分成员没写进去时批次本身仍可成功（§5.8：单成员失败不拖垮整批），
        # 但绝不能让"少写了几条"只藏在诊断字段里——`MemoryOperationResult`
        # （extra="forbid"）**没有**自由诊断字段，公开 result 只有 warnings 可见。
        warnings.append(f"本批 {len(failed)} 条成员未直接写入（见批次诊断与成员状态）")
    for entry in failed:
        if entry.get("outcome") != MEMBER_OUTCOME_FAILED:
            continue
        # I-9 要求"记录 reason"：reason + 节点 + 异常摘要都进公开可见的 warnings，
        # 否则成员失败在 operation result 里完全不可观测（结构化明细只存在图 state）。
        warnings.append(
            f"成员 {entry.get('operation_id')} 处理失败已隔离: "
            f"{entry.get('reason')} @{entry.get('failed_node')} "
            f"({entry.get('error_type')}: {entry.get('error_message')})"
        )
    if degraded_flags:
        # 结构化标记进不了 MemoryOperationResult（公开契约无自由字段），
        # 因此额外写一条可读警告，保证"末段没做"这件事在运维侧可见。
        warnings.append("批次降级标记: " + ", ".join(sorted(set(degraded_flags))))
    if not mutations and not review_ids and failed:
        # 整批全灭：不能报 succeeded（见本函数 docstring 与新发现 12）。
        raise BatchAllMembersFailedError(
            f"批次内 {len(failed)}/{len(state.get('batch_members') or [])} 条成员全部未能写入"
            f"（0 条 mutation、0 条审核候选，已释放 {release.released} 条、"
            f"转死信 {release.dead_lettered} 条）。诊断: " + _failure_digest(failed)
        )
    return {
        "batch_active": False,
        "operation": state.get("batch_operation") or state["operation"],
        "commit_result": commit_result,
        "warnings": warnings,
        "errors": errors,
    }


def _failure_digest(failed: list[dict[str, Any]]) -> str:
    """把失败成员摘要压成一行（进 ``public_error.message``，那里只保留 500 字符）。"""
    parts: list[str] = []
    for entry in failed[:MAX_BATCH_FAILURE_SUMMARY_ITEMS]:
        parts.append(
            f"{entry.get('operation_id')}={entry.get('reason')}"
            f"@{entry.get('failed_node')}({entry.get('error_type')}: {entry.get('error_message')})"
        )
    if len(failed) > MAX_BATCH_FAILURE_SUMMARY_ITEMS:
        parts.append(f"等共 {len(failed)} 条")
    return "; ".join(parts) or "无明细"


@dataclass
class _ReleaseSummary:
    """失败成员释放结果汇总（评审新发现 13：释放计数 + 达上限转死信计数）。"""

    released: int = 0
    dead_lettered: int = 0


async def _release_failed_members(
    runtime: Runtime[MemoryRuntimeContext], failed: list[dict[str, Any]]
) -> _ReleaseSummary:
    """把"成员自身问题"导致的失败成员释放回证据池；环境类异常导致的失败不释放。"""
    summary = _ReleaseSummary()
    ctx = runtime.context
    for entry in failed:
        if entry.get("reason") != MEMBER_FAILURE_REASON:
            continue
        operation_id = entry.get("operation_id")
        if not operation_id:
            continue
        try:
            async with ctx.session_factory() as session:
                async with session.begin():
                    outcome = await ops_repo.release_batch_member(
                        session, operation_id=UUID(str(operation_id))
                    )
            if outcome.released:
                summary.released += 1
            elif outcome.status == ops_repo.BATCH_MEMBER_DEAD_LETTERED:
                summary.dead_lettered += 1
        except Exception:  # 释放失败不能拖垮批次收尾
            logger.warning("失败成员释放回证据池失败: %s", operation_id, exc_info=True)
    return summary


#: 批次警告条数上限：超过即折叠成一行摘要（避免 50 条证据的警告把结果行撑大）。
MAX_BATCH_WARNINGS = 50


def _cap_warnings(warnings: list[str]) -> list[str]:
    """去重并截断批次警告，超限只保留前 N 条 + 一行汇总。"""
    unique: list[str] = []
    for warning in warnings:
        if warning not in unique:
            unique.append(warning)
    if len(unique) <= MAX_BATCH_WARNINGS:
        return unique
    kept = unique[:MAX_BATCH_WARNINGS]
    kept.append(f"其余 {len(unique) - MAX_BATCH_WARNINGS} 条警告已省略")
    return kept


# ---------------------------------------------------------------------------
# 条件边路由（供 runner 装配）
# ---------------------------------------------------------------------------


def route_after_begin_member(state: MemoryManagerState) -> str:
    """选成员后去哪：交给 summary 链 / 继续跳过下一条 / 进入 consolidation。"""
    if state.get("batch_selected"):
        return "member"
    if int(state.get("batch_index") or 0) < len(state.get("batch_members") or []):
        return "next"
    return "consolidate"


def route_after_record_member(state: MemoryManagerState) -> str:
    """记完一条成员：还有成员就继续循环，否则进入 consolidation 入口。"""
    if int(state.get("batch_index") or 0) < len(state.get("batch_members") or []):
        return "next"
    return "consolidate"
