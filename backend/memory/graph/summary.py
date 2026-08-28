"""SummaryMemoryGraph 节点链（§10.4）。

load_source_refs → sanitize_and_bound_source → extract_candidates
→ apply_scope_and_value_policy → route_candidates
→ persist_review_candidates → resolve_existing_memories
→ resolve_graph_candidates → build_mutation_plan_drafts
→ prepare_commit_mutation_plans → commit_summary_memories
→ finalize_summary_result
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.contracts.commands import (
    CommitMutationPlan,
    LearnerPatch,
    MasteryPatch,
)
from backend.memory.contracts.common import (
    candidate_match_key,
    canonical_json,
    evidence_ref_hash,
    normalize_topic_title,
    topic_key_from_title,
    topic_key_with_conflict_suffix,
)
from backend.memory.contracts.evidence import (
    ActivityEvidence,
    ConversationEvidence,
    SourceBundle,
)
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.graph.llm_schemas import MutationPlanResult
from backend.memory.graph.policies import (
    LLMBudgetExceededError,
    LLMCallBudget,
    candidate_evidence_issues,
    classify_candidate,
    classify_topic_similarity,
    evidence_gate_disposition,
    validate_commit_evidence_refs,
)
from backend.memory.graph.prompt_loader import BUILD_MUTATION_PLAN_PROMPT_VERSION
from backend.memory.graph.state import MemoryManagerState, MemoryRuntimeContext
from backend.memory.persistence import review_candidates as candidates_repo

LEARNER_CATEGORIES = {"preference", "goal", "plan"}


def _operation(state: MemoryManagerState) -> MemoryOperation:
    return MemoryOperation.model_validate(state["operation"])


def _budget(state: MemoryManagerState) -> LLMCallBudget:
    return LLMCallBudget(operation_calls=state.get("llm_call_count", 0))


def _warnings(state: MemoryManagerState) -> list[str]:
    return list(state.get("warnings", []))


def _candidates(state: MemoryManagerState) -> list[dict[str, Any]]:
    return list(state.get("candidates", []))


# ---------------------------------------------------------------------------
# 来源加载与裁剪
# ---------------------------------------------------------------------------


async def load_source_refs(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """通过 Reader 边界读取来源（§10.4：Reader，无副作用）。"""
    ctx = runtime.context
    operation = _operation(state)
    payload = operation.payload
    if isinstance(payload, ConversationEvidence):
        bundle = await ctx.conversation_reader.read(
            user_id=operation.user_id,
            thread_id=payload.thread_id,
            checkpoint_id=payload.checkpoint_id,
            message_ids=payload.message_ids,
        )
    elif isinstance(payload, ActivityEvidence):
        bundle = await ctx.activity_reader.read(
            user_id=operation.user_id,
            activity_type=payload.activity_type,
            activity_ids=payload.activity_ids,
            content_ref=payload.content_ref,
        )
    else:  # pragma: no cover - 路由保证不会到达
        raise ValueError(f"非证据 payload: {type(payload).__name__}")
    return {"source_bundle": bundle.model_dump(mode="json")}


async def sanitize_and_bound_source(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """重新校验 80KB 上限与条目结构；空来源直接走 no_change。"""
    bundle = SourceBundle.model_validate(state["source_bundle"])
    warnings = _warnings(state)
    if not bundle.items:
        warnings.append("来源为空，按 no_change 处理")
    return {"warnings": warnings}


def _source_payload(state: MemoryManagerState) -> str:
    """LLM 输入：只含 ref/role/content/occurred_at，不含 metadata。"""
    operation = _operation(state)
    bundle = SourceBundle.model_validate(state["source_bundle"])
    hints: dict[str, Any] = {}
    payload = operation.payload
    if isinstance(payload, ConversationEvidence | ActivityEvidence):
        hints = {
            "topic_hints": payload.topic_hints,
            "graph_node_hints": payload.graph_node_hints,
        }
    return canonical_json(
        {
            "hints": hints,
            "items": [
                {
                    "source_ref": item.source_ref,
                    "role": item.role,
                    "content": item.content,
                    "occurred_at": item.occurred_at.isoformat(),
                }
                for item in bundle.items
            ],
        }
    )


# ---------------------------------------------------------------------------
# 候选提取与策略
# ---------------------------------------------------------------------------


async def extract_candidates(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """第 1 次 LLM 调用（§10.4）；预算耗尽时不调用模型（§9.1）。"""
    ctx = runtime.context
    bundle = SourceBundle.model_validate(state["source_bundle"])
    if not bundle.items:
        return {"candidates": []}
    budget = _budget(state)
    try:
        result, _record = await ctx.openai_client.extract_candidates(
            source_payload=_source_payload(state), budget=budget
        )
    except LLMBudgetExceededError:
        return {
            "candidates": [],
            "llm_call_count": budget.operation_calls,
            "errors": [
                *state.get("errors", []),
                {"code": "LLM_BUDGET_EXHAUSTED", "stage": "extract_candidates"},
            ],
            "warnings": [*_warnings(state), "LLM 调用预算耗尽，无法提取候选"],
        }
    candidates = [{**c.model_dump(mode="json"), "_disposition": None} for c in result.candidates]
    warnings = _warnings(state)
    warnings.extend(f"ignored:{code}" for code in result.ignored_reason_codes)
    return {
        "candidates": candidates,
        "llm_call_count": budget.operation_calls,
        "warnings": warnings,
    }


async def apply_scope_and_value_policy(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """确定性长期价值、置信度和证据门禁分类（§9.3），不调用模型。"""
    ctx = runtime.context
    candidates = _candidates(state)
    bundle = SourceBundle.model_validate(state["source_bundle"])
    source_items = [item.model_dump(mode="json") for item in bundle.items]
    warnings = _warnings(state)
    for entry in candidates:
        disposition = classify_candidate(
            long_term_value=entry["long_term_value"],
            confidence=float(entry["confidence"]),
            settings=ctx.settings,
        )
        issues = candidate_evidence_issues(entry, source_items)
        entry["_disposition"] = evidence_gate_disposition(
            candidate=entry,
            source_items=source_items,
            disposition=disposition,
        )
        if issues:
            entry["_policy_reason_codes"] = issues
            warnings.append(f"候选证据门禁调整为 {entry['_disposition']}: {','.join(issues)}")
    kept = [c for c in candidates if c["_disposition"] != "discard"]
    discarded = len(candidates) - len(kept)
    if discarded:
        warnings.append(f"{discarded} 条候选因低置信/无长期价值被丢弃")
    return {"candidates": kept, "warnings": warnings}


async def route_candidates(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """确定性分流：无候选 → finalize；否则先持久化审核候选再处理可写入候选。"""
    candidates = _candidates(state)
    if not candidates:
        return {"route": "summary_finalize"}
    return {"route": "summary_process"}


async def persist_review_candidates(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """低置信候选写入 memory_review_candidates，不写活动 Markdown（§10.4）。"""
    ctx = runtime.context
    operation = _operation(state)
    stored: list[dict[str, Any]] = list(state.get("review_candidates", []))
    async with ctx.session_factory() as session:
        async with session.begin():
            for entry in _candidates(state):
                if entry["_disposition"] != "review":
                    continue
                candidate_id = ctx.id_generator.new_uuid()
                match_key = candidate_match_key(
                    entry["memory_type"],
                    normalize_topic_title(entry.get("topic_title") or entry["category"]),
                    entry["summary"],
                )
                if await candidates_repo.has_recent_rejected_match(
                    session, user_id=operation.user_id, match_key=match_key, now=ctx.clock.now()
                ):
                    continue  # §8.8：30 天内相同匹配键不重复生成
                await candidates_repo.insert_candidate(
                    session,
                    candidate_id=candidate_id,
                    operation_id=operation.operation_id,
                    user_id=operation.user_id,
                    candidate_type=entry["memory_type"],
                    topic_key=(
                        topic_key_from_title(entry["topic_title"])
                        if entry.get("topic_title")
                        else None
                    ),
                    normalized_match_key=match_key,
                    candidate_payload=_candidate_content(entry),
                    evidence_refs=[e["evidence_ref"] for e in entry["evidence"]],
                    confidence=float(entry["confidence"]),
                )
                stored.append({"candidate_id": str(candidate_id), "match_key": match_key})
    return {"review_candidates": stored}


def _candidate_content(entry: dict[str, Any]) -> dict[str, Any]:
    """候选受控内容视图（§6.3 CandidateContentView 同构）。"""
    content: dict[str, Any] = {
        "memory_type": entry["memory_type"],
        "topic_title": entry.get("topic_title"),
        "summary": entry["summary"],
        "category": entry["category"],
    }
    if entry["memory_type"] == "learner":
        key = {"preference": "preferences", "goal": "goals", "plan": "plans"}.get(entry["category"])
        if key:
            content[key] = [entry["summary"]]
    else:
        key = {
            "understanding": "understood",
            "difficulty": "difficulties",
            "misconception": "difficulties",
            "review_advice": "review_advice",
        }.get(entry["category"])
        if key:
            content[key] = [entry["summary"]]
        content["overview"] = entry["summary"] if entry["category"] == "understanding" else None
    return content


# ---------------------------------------------------------------------------
# 已有记忆与图谱解析
# ---------------------------------------------------------------------------


async def resolve_existing_memories(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """解析每个可写入候选的目标文档：create / merge / topic_conflict review（§9.3）。

    同时执行：拒绝匹配键抑制（§8.8）与删除证据抑制（§8.7 防旧证据复活）。
    """
    ctx = runtime.context
    operation = _operation(state)
    resolutions: list[dict[str, Any]] = list(state.get("existing_memories", []))
    extra_reviews: list[dict[str, Any]] = []
    warnings = _warnings(state)
    async with ctx.session_factory() as session:
        for index, entry in enumerate(_candidates(state)):
            if entry["_disposition"] != "auto_save":
                continue
            match_key = candidate_match_key(
                entry["memory_type"],
                normalize_topic_title(entry.get("topic_title") or entry["category"]),
                entry["summary"],
            )
            if await candidates_repo.has_recent_rejected_match(
                session, user_id=operation.user_id, match_key=match_key, now=ctx.clock.now()
            ):
                warnings.append(f"候选 {index} 命中 30 天拒绝抑制，丢弃")
                continue

            if entry["memory_type"] == "learner" or entry["category"] in LEARNER_CATEGORIES:
                resolutions.append(
                    {
                        "candidate_index": index,
                        "action": "merge",
                        "memory_id": "learner",
                        "topic_key": None,
                        "topic_title": None,
                    }
                )
                continue

            topic_title = entry.get("topic_title")
            if not topic_title:
                warnings.append(f"候选 {index} 缺少 mastery 主题，转审核")
                extra_reviews.append(entry)
                continue
            topic_key = topic_key_from_title(topic_title)
            memory_id = f"mastery:{topic_key}"

            existing = await session.execute(
                text(
                    "SELECT memory_id, active_version FROM memory_documents "
                    "WHERE user_id = :u AND memory_id = :m AND deleted_at IS NULL"
                ),
                {"u": operation.user_id, "m": memory_id},
            )
            if existing.first() is not None:
                action = "merge"
            else:
                similar = await session.execute(
                    text(
                        "SELECT memory_id, title, similarity(title, :t) AS score "
                        "FROM memory_index_entries "
                        "WHERE user_id = :u AND memory_type = 'mastery' "
                        "AND similarity(title, :t) >= :min_sim "
                        "ORDER BY score DESC LIMIT 3"
                    ),
                    {
                        "u": operation.user_id,
                        "t": topic_title,
                        "min_sim": ctx.settings.memory_topic_conflict_trgm_min,
                    },
                )
                matches = [dict(r) for r in similar.mappings().all()]
                if matches:
                    disposition = classify_topic_similarity(
                        similarity=float(matches[0]["score"]), settings=ctx.settings
                    )
                    if disposition == "auto_merge":
                        memory_id = str(matches[0]["memory_id"])
                        action = "merge"
                    elif disposition == "conflict":
                        await _persist_topic_conflict(
                            session, ctx, operation, index, entry, matches
                        )
                        warnings.append(f"候选 {index} 主题冲突，转审核")
                        continue
                    else:
                        action = "create"
                else:
                    action = "create"
                if action == "create":
                    taken = await session.execute(
                        text(
                            "SELECT 1 FROM memory_documents WHERE user_id = :u AND memory_id = :m"
                        ),
                        {"u": operation.user_id, "m": memory_id},
                    )
                    if taken.first() is not None:
                        memory_id = (
                            f"mastery:{topic_key_with_conflict_suffix(topic_key, topic_title)}"
                        )

            # 删除证据抑制（§8.7）：旧 evidence ref 不得复活同一记忆
            suppressed = False
            for ev in entry["evidence"]:
                row = await session.execute(
                    text(
                        "SELECT 1 FROM memory_deleted_evidence_suppressions "
                        "WHERE user_id = :u AND memory_id = :m "
                        "AND evidence_ref_hash = :h LIMIT 1"
                    ),
                    {
                        "u": operation.user_id,
                        "m": memory_id,
                        "h": evidence_ref_hash(ctx.settings.privacy_hmac_key, ev["evidence_ref"]),
                    },
                )
                if row.first() is not None:
                    suppressed = True
                    break
            if suppressed:
                warnings.append(f"候选 {index} 命中删除证据抑制，转审核")
                extra_reviews.append(entry)
                continue

            resolutions.append(
                {
                    "candidate_index": index,
                    "action": action,
                    "memory_id": memory_id,
                    "topic_key": memory_id.removeprefix("mastery:"),
                    "topic_title": topic_title,
                }
            )

        # 删除抑制/缺主题转入审核的候选也落库（status=pending）
        stored = list(state.get("review_candidates", []))
        for entry in extra_reviews:
            candidate_id = ctx.id_generator.new_uuid()
            await candidates_repo.insert_candidate(
                session,
                candidate_id=candidate_id,
                operation_id=operation.operation_id,
                user_id=operation.user_id,
                candidate_type=entry["memory_type"],
                topic_key=(
                    topic_key_from_title(entry["topic_title"]) if entry.get("topic_title") else None
                ),
                normalized_match_key=candidate_match_key(
                    entry["memory_type"],
                    normalize_topic_title(entry.get("topic_title") or entry["category"]),
                    entry["summary"],
                ),
                candidate_payload=_candidate_content(entry),
                evidence_refs=[e["evidence_ref"] for e in entry["evidence"]],
                confidence=float(entry["confidence"]),
            )
            stored.append({"candidate_id": str(candidate_id)})
        await session.commit()
    result: dict[str, Any] = {"existing_memories": resolutions, "warnings": warnings}
    if extra_reviews:
        result["review_candidates"] = stored
    return result


async def _persist_topic_conflict(
    session: AsyncSession,
    ctx: MemoryRuntimeContext,
    operation: MemoryOperation,
    index: int,
    entry: dict[str, Any],
    matches: list[dict[str, Any]],
) -> None:
    await candidates_repo.insert_candidate(
        session,
        candidate_id=ctx.id_generator.new_uuid(),
        operation_id=operation.operation_id,
        user_id=operation.user_id,
        candidate_type="topic_conflict",
        base_memory_id=str(matches[0]["memory_id"]),
        topic_key=topic_key_from_title(entry["topic_title"]),
        normalized_match_key=candidate_match_key(
            "topic_conflict", normalize_topic_title(entry["topic_title"]), entry["summary"]
        ),
        candidate_payload={
            **_candidate_content(entry),
            "conflict_with": [str(m["memory_id"]) for m in matches],
        },
        evidence_refs=[e["evidence_ref"] for e in entry["evidence"]],
        confidence=float(entry["confidence"]),
    )


async def resolve_graph_candidates(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """按 §16.4 优先级解析 mastery 候选的图谱节点映射（代码计算置信度）。"""
    from backend.memory.knowledge_graph.resolver import resolve_node_mapping

    ctx = runtime.context
    operation = _operation(state)
    payload = operation.payload
    hints = (
        payload.graph_node_hints
        if isinstance(payload, ConversationEvidence | ActivityEvidence)
        else []
    )
    mapping: dict[str, list[dict[str, Any]]] = {}
    candidates = _candidates(state)
    async with ctx.session_factory() as session:
        registry = ctx.graph_registry_factory(session)
        for resolution in state.get("existing_memories", []):
            index = resolution["candidate_index"]
            entry = candidates[index]
            node_mapping = await resolve_node_mapping(
                registry,
                ctx.settings,
                topic_title=resolution.get("topic_title"),
                graph_node_hints=hints,
                model_candidate_node_ids=list(entry.get("graph_node_candidates", [])),
            )
            if node_mapping is not None:
                mapping[str(index)] = [
                    {
                        "node_id": node_mapping.node_id,
                        "method": node_mapping.method,
                        "confidence": node_mapping.confidence,
                    }
                ]
    # 总结记忆没有图谱节点时照常提交（§23.2）：mapping 为空不阻断
    return {"candidate_graph_nodes": mapping}


# ---------------------------------------------------------------------------
# MutationPlan 生成与提交
# ---------------------------------------------------------------------------


async def build_mutation_plan_drafts(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """第 2 次 LLM 调用；预算耗尽时可写入候选降级为审核（§9.1）。"""
    ctx = runtime.context
    resolutions = state.get("existing_memories", [])
    if not resolutions:
        return {"mutation_plan_drafts": []}
    candidates = _candidates(state)
    docs_payload: list[dict[str, Any]] = []
    for resolution in resolutions:
        doc_view = await _current_doc_view(ctx, _operation(state), resolution["memory_id"])
        docs_payload.append({**resolution, "current_content": doc_view})
    plan_payload = canonical_json(
        {
            "candidates": [
                {
                    "index": r["candidate_index"],
                    **{
                        k: v
                        for k, v in candidates[r["candidate_index"]].items()
                        if not k.startswith("_")
                    },
                }
                for r in resolutions
            ],
            "targets": docs_payload,
        }
    )
    budget = _budget(state)
    try:
        result, _record = await ctx.openai_client.build_mutation_plan(
            plan_payload=plan_payload, budget=budget
        )
    except LLMBudgetExceededError:
        return {
            "mutation_plan_drafts": [],
            "llm_call_count": budget.operation_calls,
            "errors": [
                *state.get("errors", []),
                {"code": "LLM_BUDGET_EXHAUSTED", "stage": "build_mutation_plan"},
            ],
            "warnings": [
                *_warnings(state),
                "LLM 调用预算耗尽，可写入候选降级为 needs_review",
            ],
        }
    assert isinstance(result, MutationPlanResult)
    return {
        "mutation_plan_drafts": [d.model_dump(mode="json") for d in result.plans],
        "llm_call_count": budget.operation_calls,
    }


async def _current_doc_view(
    ctx: MemoryRuntimeContext, operation: MemoryOperation, memory_id: str
) -> dict[str, Any] | None:
    from dataclasses import asdict

    if memory_id == "learner":
        learner = await ctx.memory_service.get_learner(user_id=operation.user_id)
        return asdict(learner) if learner else None
    mastery = await ctx.memory_service.get_mastery(
        user_id=operation.user_id, topic_key=memory_id.removeprefix("mastery:")
    )
    return asdict(mastery) if mastery else None


async def prepare_commit_mutation_plans(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """草稿 → 确定计划：代码注入 mutation_id/memory_id/expected_version（§9.2）。

    非法 candidate_indexes、类型不一致或目标漂移的草稿被拒绝并记录警告。
    """
    ctx = runtime.context
    operation = _operation(state)
    candidates = _candidates(state)
    resolutions = {r["candidate_index"]: r for r in state.get("existing_memories", [])}
    graph_nodes = state.get("candidate_graph_nodes", {})
    warnings = _warnings(state)
    plans: list[dict[str, Any]] = []
    async with ctx.session_factory() as session:
        for draft in state.get("mutation_plan_drafts", []):
            if draft["action"] == "no_change":
                continue  # no_change 草稿不转换（§9.2 规则 3）
            indexes = draft.get("candidate_indexes", [])
            if not indexes or any(i >= len(candidates) or i < 0 for i in indexes):
                warnings.append("草稿包含非法 candidate_indexes，已拒绝")
                continue
            if len(set(indexes)) != len(indexes):
                warnings.append("草稿包含重复 candidate_indexes，已拒绝")
                continue
            primary = resolutions.get(indexes[0])
            if primary is None:
                warnings.append("草稿引用了不可写入候选，已拒绝")
                continue
            memory_id = primary["memory_id"]
            target_type = "learner" if memory_id == "learner" else "mastery"
            if draft["target_memory_type"] != target_type:
                warnings.append("草稿 target_memory_type 与解析目标不一致，已拒绝")
                continue
            if any(candidates[index]["memory_type"] != target_type for index in indexes):
                warnings.append("草稿 candidate 类型与目标记忆类型不一致，已拒绝")
                continue
            if any(resolutions.get(index, {}).get("memory_id") != memory_id for index in indexes):
                warnings.append("草稿跨目标合并 candidate，已拒绝")
                continue
            evidence_refs = [e["evidence_ref"] for i in indexes for e in candidates[i]["evidence"]]
            allowed_refs = {
                item.source_ref
                for item in SourceBundle.model_validate(state["source_bundle"]).items
            }
            patch_refs = list((draft.get("mastery_patch") or {}).get("evidence_refs_to_add") or [])
            try:
                validate_commit_evidence_refs(
                    evidence_refs=evidence_refs,
                    allowed_refs=allowed_refs,
                )
                if set(patch_refs) - allowed_refs:
                    raise ValueError("mastery_patch 包含未授权证据引用")
            except ValueError as exc:
                warnings.append(f"草稿证据校验失败，已拒绝: {exc}")
                continue
            expected = await _active_version(session, operation, memory_id)
            if draft["action"] == "create":
                if expected is not None:
                    action: str = "merge"
                    warnings.append(f"{memory_id} 已存在，create 降级为 merge")
                else:
                    action = "create"
            else:
                action = draft["action"]
                if expected is None:
                    action = "create"
                    warnings.append(f"{memory_id} 不存在，{draft['action']} 降级为 create")
            plan = CommitMutationPlan(
                mutation_id=ctx.id_generator.new_uuid(),
                memory_id=memory_id,
                target_memory_type=target_type,  # type: ignore[arg-type]
                topic_title=primary.get("topic_title") or draft.get("topic_title"),
                action=action,  # type: ignore[arg-type]
                expected_version=None if action == "create" else expected,
                learner_patch=(
                    LearnerPatch.model_validate(draft["learner_patch"])
                    if draft.get("learner_patch")
                    else None
                ),
                mastery_patch=(
                    MasteryPatch.model_validate(draft["mastery_patch"])
                    if draft.get("mastery_patch")
                    else None
                ),
                candidate_indexes=indexes,
            )
            node_entries = graph_nodes.get(str(indexes[0]), [])
            plans.append(
                {
                    "plan": plan.model_dump(mode="json"),
                    "evidence_refs": evidence_refs,
                    "graph_nodes": node_entries,
                }
            )
    return {"commit_mutation_plans": plans, "warnings": warnings}


async def _active_version(
    session: AsyncSession, operation: MemoryOperation, memory_id: str
) -> int | None:
    result = await session.execute(
        text(
            "SELECT active_version FROM memory_documents "
            "WHERE user_id = :u AND memory_id = :m AND deleted_at IS NULL"
        ),
        {"u": operation.user_id, "m": memory_id},
    )
    row = result.mappings().first()
    return int(row["active_version"]) if row and row["active_version"] is not None else None


async def commit_summary_memories(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """唯一总结写入节点：mutation_id 重放由 MemoryService 保证（§10.4）。

    LLM 预算耗尽导致无计划时，可写入候选降级为审核候选（§9.1 needs_review）。
    """
    ctx = runtime.context
    operation = _operation(state)
    entries = state.get("commit_mutation_plans", [])
    errors = state.get("errors", [])
    budget_exhausted = any(e.get("code") == "LLM_BUDGET_EXHAUSTED" for e in errors)
    if not entries and budget_exhausted and state.get("existing_memories"):
        candidates = _candidates(state)
        stored = list(state.get("review_candidates", []))
        async with ctx.session_factory() as session:
            async with session.begin():
                for resolution in state["existing_memories"]:
                    entry = candidates[resolution["candidate_index"]]
                    candidate_id = ctx.id_generator.new_uuid()
                    await candidates_repo.insert_candidate(
                        session,
                        candidate_id=candidate_id,
                        operation_id=operation.operation_id,
                        user_id=operation.user_id,
                        candidate_type=entry["memory_type"],
                        topic_key=resolution.get("topic_key"),
                        normalized_match_key=candidate_match_key(
                            entry["memory_type"],
                            normalize_topic_title(entry.get("topic_title") or entry["category"]),
                            entry["summary"],
                        ),
                        candidate_payload=_candidate_content(entry),
                        evidence_refs=[e["evidence_ref"] for e in entry["evidence"]],
                        confidence=float(entry["confidence"]),
                    )
                    stored.append({"candidate_id": str(candidate_id)})
        return {
            "commit_result": {"mutations": [], "replayed": False},
            "review_candidates": stored,
        }
    if not entries:
        return {"commit_result": {"mutations": [], "replayed": False}}
    allowed_refs = {
        item.source_ref for item in SourceBundle.model_validate(state["source_bundle"]).items
    }
    valid_entries: list[dict[str, Any]] = []
    warnings = _warnings(state)
    candidates = _candidates(state)
    resolutions = {r["candidate_index"]: r for r in state.get("existing_memories", [])}
    for entry in entries:
        try:
            plan = CommitMutationPlan.model_validate(entry["plan"])
            indexes = plan.candidate_indexes
            if not indexes or len(set(indexes)) != len(indexes):
                raise ValueError("candidate_indexes 为空或包含重复项")
            if any(index < 0 or index >= len(candidates) for index in indexes):
                raise ValueError("candidate_indexes 越界")
            if any(candidates[index].get("_disposition") != "auto_save" for index in indexes):
                raise ValueError("candidate_indexes 引用了不可自动写入候选")
            if any(
                candidates[index].get("memory_type") != plan.target_memory_type for index in indexes
            ):
                raise ValueError("candidate 类型与目标记忆类型不一致")
            if any(
                resolutions.get(index, {}).get("memory_id") != plan.memory_id for index in indexes
            ):
                raise ValueError("candidate 解析目标与计划 memory_id 不一致")
            expected_refs = {
                str(evidence["evidence_ref"])
                for index in indexes
                for evidence in candidates[index]["evidence"]
            }
            if set(entry.get("evidence_refs") or []) != expected_refs:
                raise ValueError("计划证据引用与 candidate 不一致")
            validate_commit_evidence_refs(
                evidence_refs=list(entry.get("evidence_refs") or []),
                allowed_refs=allowed_refs,
            )
            patch_refs = set(plan.mastery_patch.evidence_refs_to_add if plan.mastery_patch else [])
            if patch_refs - expected_refs:
                raise ValueError("mastery_patch 包含 candidate 之外的证据引用")
        except ValueError as exc:
            warnings.append(f"提交前证据校验失败，计划已跳过: {exc}")
            continue
        valid_entries.append(entry)
    if not valid_entries:
        return {
            "commit_result": {"mutations": [], "replayed": False},
            "warnings": warnings,
        }
    entries = valid_entries
    # 评审二轮 #3：Lease fencing token 传入 commit 入口做 CAS
    fencing = state.get("fencing") or {}
    outcome = await ctx.memory_service.commit_plans(
        operation_id=operation.operation_id,
        user_id=operation.user_id,
        actor_type=operation.actor_type,
        plans=[CommitMutationPlan.model_validate(e["plan"]) for e in entries],
        evidence_refs_by_plan=[list(dict.fromkeys(e["evidence_refs"])) for e in entries],
        prompt_version=BUILD_MUTATION_PLAN_PROMPT_VERSION,
        model_name=getattr(ctx.openai_client, "model_name", None),
        graph_node_ids_by_plan=[[n["node_id"] for n in e["graph_nodes"]] for e in entries],
        mapping_methods_by_plan=[
            (e["graph_nodes"][0]["method"] if e["graph_nodes"] else None) for e in entries
        ],
        mapping_confidences_by_plan=[
            (float(e["graph_nodes"][0]["confidence"]) if e["graph_nodes"] else None)
            for e in entries
        ],
        expected_worker=fencing.get("worker_id"),
        expected_generation=fencing.get("generation"),
    )
    return {
        "commit_result": {
            "mutations": [m.model_dump(mode="json") for m in outcome.mutations],
            "warnings": outcome.warnings,
            "replayed": outcome.replayed,
        },
        "warnings": warnings,
    }


async def finalize_summary_result(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """汇总稳定结果（§10.4）：mutations / review_candidate_ids / warnings。"""
    commit = state.get("commit_result", {})
    review_ids = [r["candidate_id"] for r in state.get("review_candidates", [])]
    result = {
        "mutations": commit.get("mutations", []),
        "review_candidate_ids": review_ids,
        "replayed": commit.get("replayed", False),
    }
    warnings = _warnings(state)
    warnings.extend(commit.get("warnings", []))
    return {"commit_result": result, "warnings": warnings}
