"""用户记忆命令分支（§6.2 / §6.3）：确定性转换，不调用 OpenAI。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from langgraph.runtime import Runtime

from backend.memory.contracts.commands import (
    CommitMutationPlan,
    CorrectMemoryCommand,
    ForgetMemoryCommand,
    LearnerReplacement,
    MasteryPatch,
    MasteryReplacement,
    OverrideLearnerProfileCommand,
    RestoreMemoryCommand,
    ReviewCandidateCommand,
)
from backend.memory.contracts.common import topic_key_from_title
from backend.memory.contracts.errors import (
    CandidateNotFoundError,
    MemoryNotFoundError,
)
from backend.memory.contracts.operations import MemoryOperation, MutationResult
from backend.memory.graph.state import MemoryManagerState, MemoryRuntimeContext
from backend.memory.persistence import review_candidates as candidates_repo


def _operation(state: MemoryManagerState) -> MemoryOperation:
    return MemoryOperation.model_validate(state["operation"])


async def run_memory_command(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    operation = _operation(state)
    payload = operation.payload
    if isinstance(payload, CorrectMemoryCommand | OverrideLearnerProfileCommand):
        outcome = await _run_replace_command(state, runtime, operation)
    elif isinstance(payload, ForgetMemoryCommand):
        outcome = await _run_forget(runtime, operation, payload)
    elif isinstance(payload, RestoreMemoryCommand):
        outcome = await _run_restore(runtime, operation, payload)
    elif isinstance(payload, ReviewCandidateCommand):
        outcome = await _run_review(runtime, operation, payload)
    else:  # pragma: no cover - 路由保证不会到达
        raise ValueError(f"非命令分支 payload: {type(payload).__name__}")
    return {"commit_result": outcome}


async def _run_replace_command(
    state: MemoryManagerState,
    runtime: Runtime[MemoryRuntimeContext],
    operation: MemoryOperation,
) -> dict[str, Any]:
    """correct_memory / override_learner_profile → 确定性 replace 计划（§6.2）。"""
    ctx = runtime.context
    payload = operation.payload
    if isinstance(payload, OverrideLearnerProfileCommand):
        # None 字段保持当前内容：读取当前档案后构造完整 replacement
        current = await ctx.memory_service.get_learner(user_id=operation.user_id)
        current_prefs = current.preferences if current else []
        current_goals = current.goals if current else []
        current_plans = current.plans if current else []
        replacement = LearnerReplacement(
            preferences=(payload.preferences if payload.preferences is not None else current_prefs),
            goals=payload.goals if payload.goals is not None else current_goals,
            plans=payload.plans if payload.plans is not None else current_plans,
        )
        plan = CommitMutationPlan(
            mutation_id=ctx.id_generator.new_uuid(),
            memory_id="learner",
            target_memory_type="learner",
            action="create" if payload.expected_version is None else "replace",
            expected_version=payload.expected_version,
            replacement=replacement,
            reason=payload.reason,
        )
    else:
        assert isinstance(payload, CorrectMemoryCommand)
        target_type = "learner" if payload.memory_id == "learner" else "mastery"
        plan = CommitMutationPlan(
            mutation_id=ctx.id_generator.new_uuid(),
            memory_id=payload.memory_id,
            target_memory_type=target_type,  # type: ignore[arg-type]
            action="replace",
            expected_version=payload.expected_version,
            replacement=payload.replacement,
            reason=payload.reason,
        )
    outcome = await ctx.memory_service.commit_plans(
        operation_id=operation.operation_id,
        user_id=operation.user_id,
        actor_type=operation.actor_type,
        plans=[plan],
    )
    return {
        "mutations": [m.model_dump(mode="json") for m in outcome.mutations],
        "warnings": outcome.warnings,
        "replayed": outcome.replayed,
    }


async def _run_forget(
    runtime: Runtime[MemoryRuntimeContext],
    operation: MemoryOperation,
    payload: ForgetMemoryCommand,
) -> dict[str, Any]:
    result = await runtime.context.memory_service.forget(
        operation_id=operation.operation_id,
        user_id=operation.user_id,
        actor_type=operation.actor_type,
        mutation_id=runtime.context.id_generator.new_uuid(),
        memory_id=payload.memory_id,
        expected_version=payload.expected_version,
        reason=payload.reason,
    )
    return {"mutations": [result.model_dump(mode="json")], "replayed": False}


async def _run_restore(
    runtime: Runtime[MemoryRuntimeContext],
    operation: MemoryOperation,
    payload: RestoreMemoryCommand,
) -> dict[str, Any]:
    result = await runtime.context.memory_service.restore(
        operation_id=operation.operation_id,
        user_id=operation.user_id,
        actor_type=operation.actor_type,
        mutation_id=runtime.context.id_generator.new_uuid(),
        memory_id=payload.memory_id,
        deleted_version=payload.deleted_version,
    )
    return {"mutations": [result.model_dump(mode="json")], "replayed": False}


# ---------------------------------------------------------------------------
# 候选审核（§6.3）
# ---------------------------------------------------------------------------


async def _run_review(
    runtime: Runtime[MemoryRuntimeContext],
    operation: MemoryOperation,
    payload: ReviewCandidateCommand,
) -> dict[str, Any]:
    ctx = runtime.context
    now = ctx.clock.now()
    async with ctx.session_factory() as session:
        candidate = await candidates_repo.get_candidate(session, candidate_id=payload.candidate_id)
    if candidate is None or candidate["user_id"] != operation.user_id:
        raise CandidateNotFoundError(str(payload.candidate_id))
    if candidate["status"] != "pending":
        # 已决议候选重复提交：幂等返回原决议（§23.3 可审计重放）
        return {
            "mutations": [],
            "review_resolution": {
                "candidate_id": str(payload.candidate_id),
                "status": candidate["status"],
                "replayed": True,
            },
        }

    mutations: list[MutationResult] = []
    warnings: list[str] = []
    status = {"accept": "accepted", "correct": "corrected", "reject": "rejected"}[payload.decision]
    resolution_target = payload.resolution_target
    target_memory_id = payload.target_memory_id

    if payload.decision == "reject":
        tombstone_until = now + timedelta(days=ctx.settings.memory_tombstone_days)
    else:
        tombstone_until = None
        plan = await _build_review_commit_plan(runtime, operation, payload, candidate, warnings)
        if plan is None:
            # 版本漂移无法安全重放：生成 version_conflict 候选，不提交旧计划（§6.3）
            async with ctx.session_factory() as session:
                async with session.begin():
                    await candidates_repo.insert_candidate(
                        session,
                        candidate_id=ctx.id_generator.new_uuid(),
                        operation_id=operation.operation_id,
                        user_id=operation.user_id,
                        candidate_type="version_conflict",
                        base_memory_id=candidate["base_memory_id"],
                        base_version=candidate["base_version"],
                        topic_key=candidate["topic_key"],
                        normalized_match_key=candidate["normalized_match_key"],
                        candidate_payload=candidate["candidate_payload"],
                        evidence_refs=list(candidate["evidence_refs"]),
                        confidence=float(candidate["confidence"]),
                    )
            warnings.append("候选基础版本已漂移，已生成 version_conflict 候选")
        else:
            outcome = await ctx.memory_service.commit_plans(
                operation_id=operation.operation_id,
                user_id=operation.user_id,
                actor_type=operation.actor_type,
                plans=[plan],
            )
            mutations.extend(outcome.mutations)
            target_memory_id = target_memory_id or plan.memory_id

    async with ctx.session_factory() as session:
        async with session.begin():
            await candidates_repo.resolve_candidate(
                session,
                candidate_id=payload.candidate_id,
                status=status,
                reviewed_by=operation.user_id,
                reviewed_at=now,
                resolution_target=resolution_target,
                target_memory_id=target_memory_id,
                resolved_operation_id=operation.operation_id,
                tombstone_until=tombstone_until,
            )
    return {
        "mutations": [m.model_dump(mode="json") for m in mutations],
        "warnings": warnings,
        "review_resolution": {
            "candidate_id": str(payload.candidate_id),
            "status": status,
            "replayed": False,
        },
    }


async def _build_review_commit_plan(
    runtime: Runtime[MemoryRuntimeContext],
    operation: MemoryOperation,
    payload: ReviewCandidateCommand,
    candidate: dict[str, Any],
    warnings: list[str],
) -> CommitMutationPlan | None:
    """accept/correct → 读取当前活动版本生成新 commit plan（不复用旧 mutation，§6.3）。

    版本漂移无法安全重放时返回 None。
    """
    ctx = runtime.context
    content = candidate["candidate_payload"]
    is_topic_conflict = candidate["candidate_type"] == "topic_conflict"

    if is_topic_conflict and payload.decision != "reject":
        # topic_conflict 必须显式 resolution_target，后端不猜测（§6.3）
        if payload.resolution_target == "merge_existing":
            memory_id = payload.target_memory_id
        elif payload.resolution_target == "create_new_topic":
            memory_id = None
        else:
            raise ValueError("topic_conflict 候选必须提供 resolution_target")
    else:
        memory_id = candidate["base_memory_id"]

    if payload.decision == "correct":
        assert payload.corrected_content is not None
        replacement = payload.corrected_content
        if memory_id is None:
            topic_title = (
                replacement.topic_title
                if isinstance(replacement, MasteryReplacement)
                else content.get("topic_title")
            )
            memory_id = _memory_id_for(content, topic_title)
        target_type = "learner" if memory_id == "learner" else "mastery"
        expected = await _current_version(ctx, operation, memory_id)
        if expected is None and candidate["base_memory_id"] is not None:
            return None  # 目标已删除，无法安全重放
        return CommitMutationPlan(
            mutation_id=ctx.id_generator.new_uuid(),
            memory_id=memory_id,
            target_memory_type=target_type,  # type: ignore[arg-type]
            action="create" if expected is None else "replace",
            expected_version=expected,
            replacement=replacement,
            reason=payload.reason,
        )

    # accept：候选内容 → 增量 patch
    if memory_id is None:
        memory_id = _memory_id_for(content, content.get("topic_title"))
    if memory_id == "learner":
        from backend.memory.contracts.commands import LearnerPatch

        plan_kwargs: dict[str, Any] = {
            "target_memory_type": "learner",
            "learner_patch": LearnerPatch(
                preferences_to_add=list(content.get("preferences", [])),
                goals_to_add=list(content.get("goals", [])),
                plans_to_add=list(content.get("plans", [])),
            ),
        }
    else:
        plan_kwargs = {
            "target_memory_type": "mastery",
            "topic_title": content.get("topic_title"),
            "mastery_patch": MasteryPatch(
                overview=content.get("overview"),
                understood_to_add=list(content.get("understood", [])),
                difficulties_to_add=list(content.get("difficulties", [])),
                review_advice_to_add=list(content.get("review_advice", [])),
            ),
        }
    expected = await _current_version(ctx, operation, memory_id)
    if expected is None and candidate["base_memory_id"] is not None:
        return None
    return CommitMutationPlan(
        mutation_id=ctx.id_generator.new_uuid(),
        memory_id=memory_id,
        action="create" if expected is None else "merge",
        expected_version=expected,
        reason=payload.reason,
        **plan_kwargs,
    )


def _memory_id_for(content: dict[str, Any], topic_title: str | None) -> str:
    if content.get("memory_type") == "learner":
        return "learner"
    title = topic_title or content.get("topic_key") or "未命名主题"
    return f"mastery:{topic_key_from_title(title)}"


async def _current_version(
    ctx: MemoryRuntimeContext, operation: MemoryOperation, memory_id: str
) -> int | None:
    if memory_id == "learner":
        learner = await ctx.memory_service.get_learner(user_id=operation.user_id)
        return learner.version if learner else None
    if memory_id.startswith("mastery:"):
        mastery = await ctx.memory_service.get_mastery(
            user_id=operation.user_id, topic_key=memory_id.removeprefix("mastery:")
        )
        return mastery.version if mastery else None
    raise MemoryNotFoundError(memory_id)
