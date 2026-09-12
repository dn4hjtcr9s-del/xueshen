"""consolidation 末段（memory-rebuild §5.9①）。

批次循环全部结束后执行**一次**面向用户全局文档的整理。与循环段的区别是视角：
循环段只看"本条证据 + 目标文档"，末段要通读该用户的 learner、全部 mastery、index 与
本批变更，因此它才做得了四件全局的事：

1. **重写 ``memory_summary.md``**（§2.3 的 v1 格式，schema 见 ``storage/summary_file.py``）；
2. **keywords 生产**（§2.3 index 注册表的判别性检索词；Phase 5 裁决 A 把它放在这里——
   只有通读全部文档才知道哪些词值得当检索词），经 ``frontmatter_patch`` 走正常
   不可变版本写入；
3. **aliases 归并**（§5.9③）：只把近义主题的名字**并进 canonical 文档的 aliases**，
   绝不删除用户原有文档或命名——旧名字仍能作为 ``[[link]]`` 的解析键命中 canonical；
4. **悬空链接两级制**（决议 E 组）：1 次只记为候选，≥2 批才正式建档。

外加两类记录：**并列冲突**（决议 D 组：更新鲜者优先 + 并列标注，不静默覆盖）与
**KG 双路更新**（§5.9「KG 双路更新」，失败只告警不影响长期记忆）。

幂等：末段的凭证是 ``memory_summary.meta.json`` 里的 ``batch_operation_id``——同一批次
重跑时直接跳过（§5.9 验收"末段重试不重复写同一 summary 版本"）。keywords/aliases 的
写入本身就是成员级不可变版本提交，重跑最多是多一个版本，不会重复写同一版本。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from langgraph.runtime import Runtime

from backend.memory.contracts.commands import CommitMutationPlan, FrontMatterPatch
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.graph.llm_schemas import ConsolidationResult
from backend.memory.graph.policies import LLMCallBudget
from backend.memory.graph.prompt_loader import SUMMARY_CONSOLIDATE_PROMPT_VERSION
from backend.memory.graph.state import MemoryManagerState, MemoryRuntimeContext
from backend.memory.persistence import documents as docs_repo
from backend.memory.services.memory_service import projected_title
from backend.memory.storage import summary_file

logger = logging.getLogger("memory.graph.consolidation")

#: 单次 consolidation 最多通读的主题数（防止超大户把末段拖成无限任务）。
MAX_CONSOLIDATION_TOPICS = 200

#: 单次 consolidation 最多应用的 keywords / alias 补丁数（治理动作必须有界）。
MAX_KEYWORD_PATCHES = 200
MAX_ALIAS_MERGES = 20

#: 悬空链接正式建档的门槛（决议 E 组：≥2 批）。
DANGLING_PROMOTE_MIN_BATCHES = 2

#: summary 的固定小节标题（§2.3；渲染与降级保留都按它们定位）。
PROFILE_HEADING = "## 用户画像"
PREFERENCES_HEADING = "## 稳定偏好"
ROUTES_HEADING = "## 主题路由"


def _operation(state: MemoryManagerState) -> MemoryOperation:
    return MemoryOperation.model_validate(state["operation"])


def _batch_operation(state: MemoryManagerState) -> MemoryOperation:
    """批次 operation：优先取 `batch_operation`（load_batch_members 保存的那份）。

    只有在单条路径（没有批次上下文）时才退回 `operation`。
    """
    saved = state.get("batch_operation")
    return MemoryOperation.model_validate(saved if saved else state["operation"])


async def consolidate_user_memory(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """批次末段入口（由 ``batch.enter_batch_consolidation`` 调用）。"""
    ctx = runtime.context
    settings = ctx.settings
    # **必须用批次自身的 operation**（review I-2）：`begin_batch_member` 会把
    # `state["operation"]` 投影成"当前成员"，还原发生在 `finalize_batch_result`，而本节点
    # 在它**之前**执行。若读 `state["operation"]`，末段所有产物的 batch_operation_id
    # （summary meta 的幂等凭证、悬空链接的幂等键、KG 审计锚）都会错记成最后一个成员。
    operation = _batch_operation(state)
    user_id = operation.user_id
    batch_operation_id = operation.operation_id

    processed = [
        entry
        for entry in (state.get("batch_processed") or [])
        if entry.get("outcome") != "skipped_already_committed"
    ]
    if not processed:
        return {
            "batch_consolidation": {
                "status": "skipped",
                "reason": "no_processed_members",
            }
        }

    root = settings.memory_storage_root
    meta = await summary_file.read_summary_meta(root, user_id)
    if meta is not None and meta.batch_operation_id == str(batch_operation_id):
        # §5.9 验收：末段重试不重复写同一 summary 版本
        return {
            "batch_consolidation": {
                "status": "skipped",
                "reason": "already_consolidated",
                "summary_version": meta.version,
            }
        }

    documents = await _load_user_documents(ctx, user_id=user_id)
    existing_summary = await summary_file.read_summary(root, user_id)
    payload, degraded = _build_payload(
        documents=documents,
        existing_summary=existing_summary,
        batch_diff=_batch_diff(state),
        max_chars=settings.memory_consolidation_input_max_chars,
    )
    # consolidation 是**批次容器自己的**工作，不与成员共享 LLM 预算（成员各自 4 次上限）
    budget = LLMCallBudget()
    try:
        result, record = await ctx.openai_client.consolidate_memory(
            consolidation_payload=payload, budget=budget
        )
    except Exception as exc:  # 末段失败不能让已提交的成员回滚
        logger.warning("consolidation LLM 调用失败: %s", type(exc).__name__)
        return {
            "batch_consolidation": {
                "status": "failed",
                "reason": "llm_failed",
                "error": type(exc).__name__,
            },
            "batch_warnings": [
                *(state.get("batch_warnings") or []),
                "consolidation 末段调用失败，已提交的成员不受影响，将在下一批重试",
            ],
        }

    summary_meta = await _write_summary(
        root=root,
        user_id=user_id,
        batch_operation_id=batch_operation_id,
        result=result,
        existing_summary=existing_summary,
        degraded=degraded,
    )
    keywords, aliases = await _apply_frontmatter_governance(
        ctx, state, user_id=user_id, documents=documents, result=result, operation=operation
    )
    dangling = await _govern_dangling_links(
        ctx, state, user_id=user_id, documents=documents, operation=operation
    )
    # 治理提交（keywords/aliases/建档）会把文档升版，因此 KG 双写前必须**重读**
    # 活动版本与 checksum：否则 `_load_active_links(version=旧值)` 取不到（link 已跟着
    # 新版本走），双路更新会一直空转（review I-3 附带的 DEV-028 订正）。
    refreshed = await _refresh_document_versions(ctx, user_id=user_id, documents=documents)
    kg = await _dual_write_kg(
        ctx,
        state,
        user_id=user_id,
        batch_operation_id=batch_operation_id,
        documents=refreshed,
        result=result,
    )

    conflicts = [
        {"memory_ids": list(item.memory_ids), "description": item.description}
        for item in result.conflicts[:20]
    ]
    warnings = list(state.get("batch_warnings") or [])
    if degraded:
        warnings.append("consolidation 输入超预算：只重写了主题路由段，画像与偏好沿用旧版")
    if kg.get("status") == "failed":
        warnings.append("KG overlay 更新失败（长期记忆已提交，按最终一致 + 告警处理）")

    logger.info(
        "consolidation 完成 user=%s batch=%s summary=v%s keywords=%d aliases=%d dangling=%d kg=%s",
        user_id,
        batch_operation_id,
        summary_meta.version,
        keywords.get("applied", 0),
        aliases.get("applied", 0),
        dangling.get("candidates", 0),
        kg.get("status"),
    )
    return {
        "batch_consolidation": {
            "status": "completed",
            "summary_version": summary_meta.version,
            "summary_checksum": summary_meta.checksum,
            "degraded": degraded,
            "prompt_version": SUMMARY_CONSOLIDATE_PROMPT_VERSION,
            "model_name": getattr(ctx.openai_client, "model_name", None),
            "keywords": keywords,
            "aliases": aliases,
            "dangling_links": dangling,
            "kg_dual_write": kg,
            "conflicts": conflicts,
            "topic_count": len(documents),
            "llm_prompt_version": getattr(record, "prompt_version", None),
        },
        "batch_warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 输入装载与预算
# ---------------------------------------------------------------------------


async def _load_user_documents(ctx: MemoryRuntimeContext, *, user_id: UUID) -> list[dict[str, Any]]:
    """读该用户的 learner + 全部 mastery（解析后的对象），按 memory_id 稳定排序。"""
    async with ctx.session_factory() as session:
        rows = await docs_repo.list_active_documents(session, user_id=user_id)
    checksums = {str(row["memory_id"]): str(row.get("active_checksum") or "") for row in rows}
    topics = sorted(
        str(row["topic_key"])
        for row in rows
        if row["memory_type"] == "mastery" and row["topic_key"]
    )[:MAX_CONSOLIDATION_TOPICS]
    documents: list[dict[str, Any]] = []
    learner = await ctx.memory_service.get_learner(user_id=user_id)
    if learner is not None:
        documents.append(_learner_view(learner))
    for topic_key in topics:
        mastery = await ctx.memory_service.get_mastery(user_id=user_id, topic_key=topic_key)
        if mastery is not None:
            view = _mastery_view(mastery)
            view["checksum"] = checksums.get(str(view["memory_id"]), "")
            documents.append(view)
    return documents


def _learner_view(learner: Any) -> dict[str, Any]:
    return {
        "memory_id": "learner",
        "memory_type": "learner",
        "version": int(learner.version),
        # 评审新发现 7 同类：视图里的标题也必须走 projected_title（v2 取 frontmatter
        # `name`）。learner 与 mastery 两个视图保持同一组键，`_govern_dangling_links`
        # 构造链接命名空间时读的就是这里的 `name`。
        "name": projected_title(learner),
        "preferences": list(learner.preferences),
        "goals": list(learner.goals),
        "plans": list(learner.plans),
        "aliases": list(learner.aliases),
        "links": list(learner.links),
    }


def _mastery_view(mastery: Any) -> dict[str, Any]:
    return {
        "memory_id": f"mastery:{mastery.topic_key}",
        "memory_type": "mastery",
        "topic_key": mastery.topic_key,
        "topic_title": mastery.topic_title,
        # 评审新发现 7 同类：这里曾自己写 `mastery.name or mastery.topic_title`
        # （没有 v2 判定、也没有 learner 回退），是第五个"标题投影站点"。统一走权威规则。
        "name": projected_title(mastery),
        "description": mastery.description or "",
        "aliases": list(mastery.aliases),
        "keywords": list(getattr(mastery, "keywords", []) or []),
        "version": int(mastery.version),
        "overview": mastery.overview,
        "understood": list(mastery.understood),
        "difficulties": list(mastery.difficulties),
        "review_advice": list(mastery.review_advice),
        "links": list(mastery.links),
        "evidence_refs": list(mastery.evidence_refs),
    }


def _batch_diff(state: MemoryManagerState) -> list[dict[str, Any]]:
    """本批证据带来的变更摘要（只给 memory_id/outcome/计数，不给正文）。"""
    diff: list[dict[str, Any]] = []
    for entry in state.get("batch_processed") or []:
        diff.append(
            {
                "operation_id": entry.get("operation_id"),
                "outcome": entry.get("outcome"),
                "mutation_count": entry.get("mutation_count"),
                "review_candidate_count": entry.get("review_candidate_count"),
            }
        )
    return diff


def _build_payload(
    *,
    documents: list[dict[str, Any]],
    existing_summary: bytes | None,
    batch_diff: list[dict[str, Any]],
    max_chars: int,
) -> tuple[str, bool]:
    """拼 LLM 输入；超预算时置 ``degraded``（§4.5-② 决议 B 组：只重写主题路由段）。"""
    import json

    payload = {
        "learner": next((d for d in documents if d["memory_type"] == "learner"), None),
        "mastery": [d for d in documents if d["memory_type"] == "mastery"],
        "existing_summary": (existing_summary or b"").decode("utf-8", errors="replace"),
        "batch_diff": batch_diff,
        "degraded": False,
    }
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if max_chars <= 0 or len(rendered) <= max_chars:
        return rendered, False
    # 超限：只保留"主题路由"所需的骨架（topic_key/title/overview 截断），并显式告知模型
    trimmed = {
        "learner": None,
        "mastery": [
            {
                "memory_id": doc["memory_id"],
                "topic_key": doc["topic_key"],
                "name": doc["name"],
                "overview": str(doc.get("overview") or "")[:120],
            }
            for doc in documents
            if doc["memory_type"] == "mastery"
        ],
        "existing_summary": payload["existing_summary"],
        "batch_diff": batch_diff,
        "degraded": True,
    }
    return json.dumps(trimmed, ensure_ascii=False, sort_keys=True), True


# ---------------------------------------------------------------------------
# summary 落盘
# ---------------------------------------------------------------------------


async def _write_summary(
    *,
    root: str,
    user_id: UUID,
    batch_operation_id: UUID,
    result: ConsolidationResult,
    existing_summary: bytes | None,
    degraded: bool,
) -> summary_file.SummaryMeta:
    """渲染并写入 summary；降级时画像/偏好沿用旧版（§4.5-② 决议）。"""
    profile = list(result.user_profile)
    preferences = list(result.stable_preferences)
    if degraded:
        previous = _parse_sections((existing_summary or b"").decode("utf-8", errors="replace"))
        profile = previous.get(PROFILE_HEADING, [])
        preferences = previous.get(PREFERENCES_HEADING, [])
    body = render_summary_body(
        user_profile=profile,
        stable_preferences=preferences,
        topic_routes=[
            (route.topic_key, route.status_line, route.proficiency) for route in result.topic_routes
        ],
    )
    return await summary_file.write_summary(
        root,
        user_id,
        body,
        batch_operation_id=batch_operation_id,
        generated_at=datetime.now(UTC),
        degraded=degraded,
    )


def render_summary_body(
    *,
    user_profile: list[str],
    stable_preferences: list[str],
    topic_routes: list[tuple[str, str, str]],
) -> str:
    """按 §2.3 的 v1 格式渲染三个小节（首行标记由 ``summary_file`` 负责）。"""
    lines: list[str] = [PROFILE_HEADING]
    lines.extend(f"- {item}" for item in user_profile)
    lines.append("")
    lines.append(PREFERENCES_HEADING)
    lines.extend(f"- {item}" for item in stable_preferences)
    lines.append("")
    lines.append(ROUTES_HEADING)
    for topic_key, status_line, proficiency in topic_routes:
        lines.append(f"- {topic_key} | {status_line} | {proficiency} → mastery:{topic_key}")
    return "\n".join(lines)


def _parse_sections(text: str) -> dict[str, list[str]]:
    """从旧 summary 里取回指定小节的 bullet（降级时保留旧画像/偏好）。"""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line in (PROFILE_HEADING, PREFERENCES_HEADING, ROUTES_HEADING):
            current = line
            sections.setdefault(current, [])
            continue
        if line.startswith("## "):
            current = None
            continue
        if current and line.startswith("- "):
            sections[current].append(line[2:].strip())
    return sections


# ---------------------------------------------------------------------------
# 治理：keywords / aliases
# ---------------------------------------------------------------------------


async def _apply_frontmatter_governance(
    ctx: MemoryRuntimeContext,
    state: MemoryManagerState,
    *,
    user_id: UUID,
    documents: list[dict[str, Any]],
    result: ConsolidationResult,
    operation: MemoryOperation,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """keywords 与 aliases 归并**合并成每个文档一次提交**，返回 (keywords, aliases) 结果。

    两件事都写同一份 frontmatter，分成两次提交必然踩乐观并发：第一次成功升版后，
    第二次带的 `expected_version` 已经过期（实测报 `IntegrityError`/版本冲突）。
    合并成一次既避免冲突，也少一个无意义版本。

    - **keywords**（Phase 5 裁决 A 的唯一生产者，§2.3）：只在词集确实变化时下补丁；
    - **aliases 归并**（§5.9③）：只把被并入主题的名字/别名**加进** canonical 的 aliases，
      **不删除任何文档**——旧名字仍能作为 `[[link]]` 的解析键命中 canonical，
      映射关系随结果与 commit 记录留痕；人工纠错走既有 P0 命令。
    """
    by_topic = {doc["topic_key"]: doc for doc in documents if doc["memory_type"] == "mastery"}
    by_id = {doc["memory_id"]: doc for doc in documents}

    keyword_plan_input: dict[str, list[str]] = {}
    keyword_skipped = 0
    for item in result.keywords[:MAX_KEYWORD_PATCHES]:
        doc = by_id.get(item.memory_id)
        if doc is None or doc["memory_type"] != "mastery":
            keyword_skipped += 1
            continue
        words = [word.strip() for word in item.keywords if word.strip()]
        if not words or sorted(words) == sorted(doc.get("keywords") or []):
            keyword_skipped += 1
            continue
        keyword_plan_input[item.memory_id] = words[:8]

    alias_additions: dict[str, list[str]] = {}
    mappings: list[dict[str, Any]] = []
    rejected: list[str] = []
    for merge in result.alias_merges[:MAX_ALIAS_MERGES]:
        canonical = by_topic.get(merge.canonical_topic_key)
        if canonical is None:
            rejected.append(f"canonical 不存在: {merge.canonical_topic_key}")
            continue
        incoming: list[str] = []
        for topic_key in merge.merged_topic_keys:
            merged = by_topic.get(topic_key)
            if merged is None or merged["memory_id"] == canonical["memory_id"]:
                continue
            incoming.extend([str(merged.get("name") or topic_key), topic_key])
            incoming.extend(str(alias) for alias in merged.get("aliases") or [])
        current = list(canonical.get("aliases") or [])
        additions = [alias for alias in dict.fromkeys(incoming) if alias and alias not in current]
        if not additions:
            continue
        alias_additions[canonical["memory_id"]] = additions[:8]
        mappings.append(
            {
                "canonical_memory_id": canonical["memory_id"],
                "merged_topic_keys": list(merge.merged_topic_keys),
                "aliases_added": additions,
                "reason": merge.reason,
            }
        )

    plans: list[CommitMutationPlan] = []
    for memory_id in sorted({*keyword_plan_input, *alias_additions}):
        doc = by_id[memory_id]
        plans.append(
            CommitMutationPlan(
                mutation_id=uuid4(),
                memory_id=memory_id,
                target_memory_type="mastery",
                topic_title=doc.get("topic_title"),
                action="frontmatter_patch",
                expected_version=int(doc["version"]),
                frontmatter_patch=FrontMatterPatch(
                    keywords=keyword_plan_input.get(memory_id, []),
                    aliases=alias_additions.get(memory_id, []),
                ),
            )
        )
    outcome: dict[str, Any] = (
        await _commit(ctx, state, user_id=user_id, plans=plans, operation=operation)
        if plans
        else {"committed": 0, "failed": []}
    )
    failed: list[dict[str, str]] = list(outcome["failed"])
    failed_ids = {item["memory_id"] for item in failed}
    keywords = {
        "applied": len([mid for mid in keyword_plan_input if mid not in failed_ids]),
        "skipped": keyword_skipped,
        "failed": failed,
    }
    aliases = {
        "applied": len([mid for mid in alias_additions if mid not in failed_ids]),
        "mappings": mappings,
        "rejected": rejected,
        "failed": failed,
    }
    return keywords, aliases


async def _commit(
    ctx: MemoryRuntimeContext,
    state: MemoryManagerState,
    *,
    user_id: UUID,
    plans: list[CommitMutationPlan],
    operation: MemoryOperation | None = None,
) -> dict[str, Any]:
    """共用的治理提交入口：走正常不可变版本 + fence（失租则整批中止）。

    单个计划失败不拖垮其余：`commit_plans` 是**整事务**语义，因此这里逐个提交，
    把失败原因收进结果（§5.9："任何一路失败都记录最终一致告警，允许各自幂等重试"）。
    """
    fencing = state.get("fencing") or {}
    commit_operation = operation or _batch_operation(state)
    committed = 0
    failed: list[dict[str, str]] = []
    for plan in plans:
        try:
            await ctx.memory_service.commit_plans(
                operation_id=commit_operation.operation_id,
                user_id=user_id,
                actor_type=commit_operation.actor_type,
                plans=[plan],
                prompt_version=SUMMARY_CONSOLIDATE_PROMPT_VERSION,
                model_name=getattr(ctx.openai_client, "model_name", None),
                expected_worker=fencing.get("worker_id"),
                expected_generation=fencing.get("generation"),
            )
            committed += 1
        except Exception as exc:  # 治理失败只告警，不影响已提交成员
            logger.warning(
                "consolidation 治理提交失败 memory=%s: %s: %s",
                plan.memory_id,
                type(exc).__name__,
                exc,
            )
            # reason 带上异常摘要：只记类型名会让"IntegrityError"这类失败无从排查，
            # 截断到 200 字符避免把 SQL/数据带进结果。
            failed.append(
                {"memory_id": plan.memory_id, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
            )
    return {"committed": committed, "failed": failed}


# ---------------------------------------------------------------------------
# 治理：悬空链接 / KG 双路
# ---------------------------------------------------------------------------


async def _govern_dangling_links(
    ctx: MemoryRuntimeContext,
    state: MemoryManagerState,
    *,
    user_id: UUID,
    documents: list[dict[str, Any]],
    operation: MemoryOperation,
) -> dict[str, Any]:
    """悬空链接两级制（§5.9④ / 决议 E 组）：扫描 → 记候选 → ≥2 批正式建档。"""
    from backend.memory.persistence import dangling_links as dangling_repo

    # 解析命名空间复用仓储提供的 link_namespace（§3.2 的 memory_id → name → aliases
    # 三级优先级由它保证），不在这里重复实现一套拼键逻辑。
    namespace = dangling_repo.link_namespace(
        [
            {
                "memory_id": doc["memory_id"],
                "title": doc.get("name") or doc.get("topic_title") or "",
                "aliases": list(doc.get("aliases") or []),
            }
            for doc in documents
        ]
    )
    sightings: dict[str, dict[str, Any]] = {}
    for doc in documents:
        for raw_target in doc.get("links") or []:
            target = str(raw_target).strip()
            if not target:
                continue
            try:
                target_key = dangling_repo.normalize_link_target(target)
            except dangling_repo.DanglingLinkError:
                # 单个脏链（控制字符等）不能中断整批治理
                continue
            if dangling_repo.resolve_link_target(target, known=namespace) is not None:
                continue
            entry = sightings.setdefault(
                target_key,
                {"target": target, "target_key": target_key, "source_memory_ids": []},
            )
            if doc["memory_id"] not in entry["source_memory_ids"]:
                entry["source_memory_ids"].append(doc["memory_id"])
    if not sightings:
        return {"sighted": 0, "candidates": 0, "promoted": [], "failed": False}
    try:
        async with ctx.session_factory() as session:
            async with session.begin():
                await dangling_repo.record_sightings(
                    session,
                    user_id=user_id,
                    batch_operation_id=operation.operation_id,
                    sightings=list(sightings.values()),
                )
                candidates = await dangling_repo.list_candidates(
                    session, user_id=user_id, min_batches=DANGLING_PROMOTE_MIN_BATCHES
                )
    except Exception as exc:  # 治理失败只告警
        logger.warning("悬空链接登记失败: %s", type(exc).__name__)
        return {"sighted": len(sightings), "candidates": 0, "promoted": [], "failed": True}
    promoted = await _promote_dangling_candidates(
        ctx,
        state,
        user_id=user_id,
        candidates=candidates,
        namespace=namespace,
        operation=operation,
    )
    return {
        "sighted": len(sightings),
        "candidates": len(candidates),
        "promoted": promoted,
        "failed": False,
    }


async def _promote_dangling_candidates(
    ctx: MemoryRuntimeContext,
    state: MemoryManagerState,
    *,
    user_id: UUID,
    candidates: list[dict[str, Any]],
    namespace: dict[str, str],
    operation: MemoryOperation,
) -> list[str]:
    """对达到门槛的候选正式建档（§5.9④：累计 ≥2 批）。

    建档 = 走正常提交路径 create 一个最小 mastery 文档（name/description + 一句
    overview 说明"这是从悬空链接收集来的候选主题"），来源批次与证据留在
    ``memory_dangling_links`` 行上（§5.9④"候选和正式主题都保留来源 evidence/批次"）。
    """
    from backend.memory.persistence import dangling_links as dangling_repo

    promoted: list[str] = []
    for candidate in candidates:
        # 建档用**规范化后的 target_key**而不是原始文本：`[[Ｅllipse]]`（全角）与
        # `[[Ellipse]]` 的 key 都是 `Ellipse`，用原文拼会造出两个主题。
        target_key = str(candidate.get("target_key") or candidate.get("target") or "").strip()
        if not target_key or target_key in namespace:
            continue
        memory_id = f"mastery:{target_key}"
        if len(memory_id) > 160:
            continue
        plan = CommitMutationPlan(
            mutation_id=uuid4(),
            memory_id=memory_id,
            target_memory_type="mastery",
            topic_title=target_key,
            action="create",
            frontmatter_patch=FrontMatterPatch(
                name=target_key[:120], description="待补充的主题（由悬空链接候选转正）"
            ),
            mastery_patch=None,
        )
        outcome = await _commit(ctx, state, user_id=user_id, plans=[plan], operation=operation)
        if outcome.get("committed"):
            promoted.append(memory_id)
            try:
                async with ctx.session_factory() as session:
                    async with session.begin():
                        await dangling_repo.mark_promoted(
                            session,
                            user_id=user_id,
                            target_key=target_key,
                            memory_id=memory_id,
                        )
            except Exception as exc:
                logger.warning("悬空候选建档回写失败 %s: %s", memory_id, type(exc).__name__)
    return promoted


async def _refresh_document_versions(
    ctx: MemoryRuntimeContext, *, user_id: UUID, documents: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """治理提交之后重读活动版本/checksum，供 KG 双路使用（review I-3 附带项）。"""
    if not documents:
        return documents
    async with ctx.session_factory() as session:
        rows = await docs_repo.list_active_documents(session, user_id=user_id)
    active = {str(row["memory_id"]): row for row in rows}
    refreshed: list[dict[str, Any]] = []
    for doc in documents:
        row = active.get(str(doc.get("memory_id")))
        if row is None:
            refreshed.append(doc)
            continue
        updated = dict(doc)
        updated["version"] = int(row.get("active_version") or doc.get("version") or 0)
        updated["checksum"] = str(row.get("active_checksum") or doc.get("checksum") or "")
        refreshed.append(updated)
    return refreshed


def _topic_version(documents: list[dict[str, Any]], topic_key: str) -> int:
    for doc in documents:
        if doc.get("topic_key") == topic_key:
            return int(doc.get("version") or 0)
    return 0


def _topic_checksum(documents: list[dict[str, Any]], topic_key: str) -> str:
    for doc in documents:
        if doc.get("topic_key") == topic_key:
            return str(doc.get("checksum") or "")
    return ""


async def _dual_write_kg(
    ctx: MemoryRuntimeContext,
    state: MemoryManagerState,
    *,
    user_id: UUID,
    batch_operation_id: UUID,
    documents: list[dict[str, Any]],
    result: ConsolidationResult,
) -> dict[str, Any]:
    """KG 双路更新（§5.9「KG 双路更新」）：失败只告警，不影响长期记忆。

    flag 关闭或模块未装配时返回 skipped——读路径（context build）那边有自己的协调点，
    不依赖这里是否写过。
    """
    if not bool(getattr(ctx.settings, "memory_kg_dual_write_enabled", False)):
        return {"status": "skipped", "reason": "flag_disabled"}
    try:
        from backend.memory.services import kg_dual_write
    except Exception as exc:  # 模块缺失时不能拖垮整批
        logger.warning("KG 双写模块不可用: %s", type(exc).__name__)
        return {"status": "skipped", "reason": "module_unavailable"}
    changed_topics: list[dict[str, Any]] = [
        {
            "memory_id": f"mastery:{route.topic_key}",
            "topic_key": route.topic_key,
            "version": _topic_version(documents, route.topic_key),
            "checksum": _topic_checksum(documents, route.topic_key),
            "keywords": [],
            "direction": "positive" if route.proficiency != "learning" else "learning",
            "strength": 0.8 if route.proficiency == "expert" else 0.6,
            "occurred_at": datetime.now(UTC),
        }
        for route in result.topic_routes[:MAX_CONSOLIDATION_TOPICS]
        if any(d.get("topic_key") == route.topic_key for d in documents)
    ]
    outcome = await kg_dual_write.after_consolidation(
        user_id=user_id,
        batch_operation_id=batch_operation_id,
        # review-2 新发现 8：这里同样必须是**批次** operation——`state["operation"]` 此刻
        # 仍被投影成最后一个成员（还原在 finalize_batch_result），传错会让 KG 幂等键与
        # 审计锚错位到成员上。
        operation_id=_batch_operation(state).operation_id,
        changed_topics=changed_topics,
        conflicts=[
            {"memory_ids": list(item.memory_ids), "description": item.description}
            for item in result.conflicts
        ],
        settings=ctx.settings,
        session_factory=ctx.session_factory,
        logger=logger,
    )
    return {
        "status": outcome.status,
        "reason": outcome.reason,
        "projection_operation_ids": [str(item) for item in outcome.projection_operation_ids],
        "conflicted_memory_ids": list(outcome.conflicted_memory_ids),
    }


__all__ = [
    "DANGLING_PROMOTE_MIN_BATCHES",
    "MAX_ALIAS_MERGES",
    "MAX_CONSOLIDATION_TOPICS",
    "MAX_KEYWORD_PATCHES",
    "consolidate_user_memory",
    "render_summary_body",
]
