"""回答合同与证据上下文打包。

本模块只负责回答阶段的确定性编排：把检索证据绑定到改写任务，按任务与证据角色
分配回答预算，并对单个长 chunk 做 token 级截断。它不改变检索召回和排序结果。
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Literal

from backend.conversation.contracts.graph import (
    AnswerContract,
    AnswerEvidenceAnnotation,
    AnswerEvidenceBudget,
    AnswerHistoryItem,
    AnswerMemoryContext,
    AnswerSubquestion,
    AnswerTaskContract,
)
from backend.conversation.services.token_counter import TokenCounter


def build_answer_contract(
    *,
    current_question: str,
    standalone_question: str,
    rewrite_plan: dict[str, Any],
    snapshot: Any,
    evidence_items: list[dict[str, Any]],
    evidence_assessment: dict[str, Any] | None,
    token_counter: TokenCounter,
    total_budget: int,
) -> tuple[AnswerContract, str, list[str]]:
    """构造回答合同，并返回合同、已裁剪证据文本与证据引用。"""
    subqueries = list(rewrite_plan.get("subqueries") or [])
    requires_evidence = bool(rewrite_plan.get("need_retrieval", True))
    tasks = _build_tasks(
        subqueries,
        evidence_items,
        evidence_assessment,
        fallback_question=standalone_question,
        requires_evidence=requires_evidence,
    )
    annotations = _build_annotations(evidence_items, tasks)
    packed_items, budget_trace = _pack_evidence(
        evidence_items,
        tasks,
        annotations,
        token_counter=token_counter,
        total_budget=max(0, total_budget),
    )
    packed_by_id = {_evidence_key(item): item for item in packed_items}
    for task in tasks:
        original_ids = set(task.evidence_ids)
        task.evidence_ids = [
            evidence_id for evidence_id in task.evidence_ids if evidence_id in packed_by_id
        ]
        task.evidence_roles = sorted(
            {
                role
                for evidence_id in task.evidence_ids
                for role in packed_by_id[evidence_id].get("_answer_roles", [])
            }
        )
        if not task.required:
            task.status = "covered"
        elif not task.evidence_ids:
            if original_ids:
                task.missing_aspects.append("回答上下文预算未保留该任务证据")
            task.status = "missing"
        elif (
            set(task.evidence_ids) != original_ids
            or task.status == "partially_covered"
            or any(
                bool(packed_by_id[evidence_id].get("_answer_truncated"))
                for evidence_id in task.evidence_ids
            )
        ):
            task.status = "partially_covered"
        else:
            task.status = "covered"

    selected_ids = set(packed_by_id)
    for annotation in annotations:
        if not annotation.task_ids:
            annotation.coverage = "unassigned"
        elif annotation.evidence_id in selected_ids:
            annotation.coverage = (
                "partial"
                if packed_by_id[annotation.evidence_id].get("_answer_truncated")
                else "covered"
            )
        else:
            annotation.coverage = "partial"

    evidence_lines: list[str] = []
    evidence_refs: list[str] = []
    for item in packed_items:
        citation_id = _citation_id(item)
        evidence_refs.append(citation_id)
        evidence_lines.append(f"[{citation_id}]\n{item.get('content_text') or ''}")

    history: list[AnswerHistoryItem] = []
    if snapshot.conversation_summary:
        history.append(AnswerHistoryItem(role="summary", content=snapshot.conversation_summary))
    history.extend(
        AnswerHistoryItem(role=message.role, content=message.content)
        for message in snapshot.recent_messages
    )
    contract = AnswerContract(
        current_question=current_question or standalone_question or "当前问题",
        standalone_question=standalone_question,
        subquestions=[
            AnswerSubquestion(
                subquery_id=str(item.get("subquery_id") or "task-main"),
                question=str(item.get("query_text") or standalone_question or "当前问题"),
                intent=str(item.get("intent") or ""),
                coverage_target=str(item.get("coverage_target") or ""),
            )
            for item in subqueries
        ],
        necessary_history=history,
        relevant_memory=AnswerMemoryContext(
            status=snapshot.memory.status,
            learner=snapshot.memory.learner,
            mastery=snapshot.memory.mastery,
            truncated=snapshot.memory.truncated,
        ),
        tasks=tasks,
        evidence_annotations=annotations,
        partial_refusal_rules=_partial_refusal_rules(tasks),
        evidence_budget=AnswerEvidenceBudget.model_validate(budget_trace),
    )
    return contract, "\n".join(evidence_lines) or "（无证据）", evidence_refs


def _build_tasks(
    subqueries: list[dict[str, Any]],
    evidence_items: list[dict[str, Any]],
    assessment: dict[str, Any] | None,
    *,
    fallback_question: str,
    requires_evidence: bool,
) -> list[AnswerTaskContract]:
    """按 subquery 建立回答任务；无子问题时保留一个主任务。"""
    synthetic_main_task = not subqueries
    if synthetic_main_task:
        subqueries = [
            {
                "subquery_id": "task-main",
                "query_text": fallback_question or "当前问题",
                "intent": "main",
                "coverage_target": "",
            }
        ]
    missing = [str(item) for item in (assessment or {}).get("missing_aspects") or []]
    assessment_status = str((assessment or {}).get("status") or "sufficient")
    tasks: list[AnswerTaskContract] = []
    for subquery in subqueries:
        task_id = str(subquery.get("subquery_id") or "task-main")
        evidence_ids = [
            _evidence_key(item)
            for item in evidence_items
            if task_id in {str(value) for value in (item.get("matched_subquery_ids") or [])}
        ]
        if synthetic_main_task:
            evidence_ids = [_evidence_key(item) for item in evidence_items]
        task_missing = _matching_missing_aspects(subquery, missing)
        status: Literal["covered", "partially_covered", "missing"]
        if not requires_evidence:
            status = "covered"
        elif not evidence_ids:
            status = "missing"
        elif task_missing or (assessment_status == "insufficient" and not missing):
            status = "partially_covered"
        else:
            status = "covered"
        task = AnswerTaskContract(
            task_id=task_id,
            subquery_id=task_id if task_id != "task-main" else None,
            task_type=str(subquery.get("intent") or ""),
            question=str(subquery.get("query_text") or "当前问题"),
            required=requires_evidence,
            evidence_ids=evidence_ids,
            status=status,
            missing_aspects=task_missing if task_missing or not evidence_ids else [],
        )
        tasks.append(task)
    return tasks


def _build_annotations(
    evidence_items: list[dict[str, Any]], tasks: list[AnswerTaskContract]
) -> list[AnswerEvidenceAnnotation]:
    """为每条 evidence 计算 task 关联和确定性证据角色。"""
    task_by_id = {task.task_id: task for task in tasks}
    task_ids = set(task_by_id)
    main_task_id = tasks[0].task_id if len(tasks) == 1 else None
    annotations: list[AnswerEvidenceAnnotation] = []
    for item in evidence_items:
        evidence_id = _evidence_key(item)
        matched = [
            str(value)
            for value in (item.get("matched_subquery_ids") or [])
            if str(value) in task_ids
        ]
        if not matched and main_task_id == "task-main":
            matched = [main_task_id]
        relevance_notes = [
            (f"{task_id}: matched_subquery_ids 直接命中任务“{task_by_id[task_id].question}”")
            for task_id in matched
        ]
        if matched == ["task-main"]:
            relevance_notes = ["task-main: 未拆分子问题，chunk 归入当前主任务"]
        roles = [_evidence_role(item)]
        annotations.append(
            AnswerEvidenceAnnotation(
                evidence_id=evidence_id,
                chunk_ids=[str(value) for value in (item.get("chunk_ids") or [])],
                task_ids=matched,
                roles=roles,
                relevance_notes=relevance_notes,
                coverage="covered" if matched else "unassigned",
            )
        )
    return annotations


def _pack_evidence(
    evidence_items: list[dict[str, Any]],
    tasks: list[AnswerTaskContract],
    annotations: list[AnswerEvidenceAnnotation],
    *,
    token_counter: TokenCounter,
    total_budget: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按任务和角色预留预算，再用剩余额度补充高分证据。"""
    task_ids = [task.task_id for task in tasks]
    task_budget = _even_budget(task_ids, total_budget)
    annotation_by_id = {annotation.evidence_id: annotation for annotation in annotations}
    ordered = sorted(
        evidence_items,
        key=lambda item: (
            -float(item.get("score") or 0),
            _evidence_key(item),
        ),
    )
    item_by_id = {_evidence_key(item): item for item in ordered}
    role_groups: dict[str, set[str]] = defaultdict(set)
    for annotation in annotations:
        for task_id in annotation.task_ids:
            role_groups[task_id].update(annotation.roles)
    role_budget = {
        task_id: _even_budget(sorted(roles), task_budget.get(task_id, 0))
        for task_id, roles in role_groups.items()
    }

    # 每个 task-role 先给最高分证据预留额度，防止高分单任务吞掉全部上下文。
    allocations: dict[str, int] = defaultdict(int)
    for task_id in task_ids:
        for role, quota in role_budget.get(task_id, {}).items():
            candidates = [
                item
                for item in ordered
                if task_id in annotation_by_id[_evidence_key(item)].task_ids
                and role in annotation_by_id[_evidence_key(item)].roles
            ]
            if candidates and quota > 0:
                evidence_id = _evidence_key(candidates[0])
                allocations[evidence_id] = max(allocations[evidence_id], quota)

    packed_by_id: dict[str, dict[str, Any]] = {}
    used = 0
    for evidence_id, allocation in allocations.items():
        item = item_by_id[evidence_id]
        content = str(item.get("content_text") or "")
        packed = dict(item)
        packed["content_text"] = token_counter.truncate(content, allocation)
        packed["token_count"] = token_counter.count(packed["content_text"])
        packed["_answer_roles"] = annotation_by_id[evidence_id].roles
        packed["_answer_truncated"] = packed["token_count"] < token_counter.count(content)
        if packed["token_count"] <= 0:
            continue
        packed_by_id[evidence_id] = packed
        used += int(packed["token_count"])

    # 预留后剩余预算按原证据分数补齐；同一个 chunk 始终只计一次。
    for item in ordered:
        if used >= total_budget:
            break
        evidence_id = _evidence_key(item)
        annotation = annotation_by_id[evidence_id]
        if not annotation.task_ids:
            continue
        content = str(item.get("content_text") or "")
        content_tokens = token_counter.count(content)
        existing_tokens = int(packed_by_id.get(evidence_id, {}).get("token_count") or 0)
        expandable = max(0, content_tokens - existing_tokens)
        if expandable <= 0:
            continue
        target_tokens = existing_tokens + min(total_budget - used, expandable)
        packed = dict(item)
        packed["content_text"] = token_counter.truncate(content, target_tokens)
        packed["token_count"] = token_counter.count(packed["content_text"])
        packed["_answer_roles"] = annotation.roles
        packed["_answer_truncated"] = packed["token_count"] < content_tokens
        packed_by_id[evidence_id] = packed
        used += int(packed["token_count"]) - existing_tokens

    selected_ids = set(packed_by_id)
    packed_items = [
        packed_by_id[_evidence_key(item)] for item in ordered if _evidence_key(item) in selected_ids
    ]
    original_tokens = sum(
        token_counter.count(str(item.get("content_text") or "")) for item in evidence_items
    )
    trace = {
        "total_budget": total_budget,
        "used_tokens": used,
        "truncated_tokens": max(0, original_tokens - used),
        "task_budgets": task_budget,
        "role_budgets": role_budget,
        "task_selected_evidence_ids": {
            task_id: [
                evidence_id
                for evidence_id in selected_ids
                if task_id in annotation_by_id[evidence_id].task_ids
            ]
            for task_id in task_ids
        },
        "selected_evidence_ids": sorted(selected_ids),
        "dropped_evidence_ids": [
            _evidence_key(item)
            for item in evidence_items
            if _evidence_key(item) not in selected_ids
        ],
    }
    return packed_items, trace


def _even_budget(keys: list[str], total: int) -> dict[str, int]:
    if not keys:
        return {}
    base, remainder = divmod(max(0, total), len(keys))
    return {key: base + (1 if index < remainder else 0) for index, key in enumerate(keys)}


def _matching_missing_aspects(subquery: dict[str, Any], missing_aspects: list[str]) -> list[str]:
    """把评估器的缺失面向确定性匹配到对应子任务。"""
    task_text = " ".join(
        str(subquery.get(key) or "") for key in ("query_text", "intent", "coverage_target")
    ).lower()
    matched: list[str] = []
    for aspect in missing_aspects:
        normalized = aspect.strip().lower()
        if not normalized:
            continue
        if normalized in task_text or task_text in normalized:
            matched.append(aspect)
            continue
        keywords = {word for word in re.split(r"[\s,，。；;：:、]+", normalized) if len(word) >= 2}
        if any(keyword in task_text for keyword in keywords):
            matched.append(aspect)
    return matched


def _evidence_role(item: dict[str, Any]) -> str:
    role = str(item.get("content_role") or "").lower()
    if any(word in role for word in ("definition", "theorem", "concept", "定义", "定理", "概念")):
        return "definition_theorem"
    if any(
        word in role
        for word in ("proof", "derivation", "method", "formula", "证明", "推导", "方法", "公式")
    ):
        return "method_derivation"
    if any(word in role for word in ("exercise", "example", "solution", "练习", "例题", "解答")):
        return "example_solution"
    if any(word in role for word in ("compare", "comparison", "对比", "比较")):
        return "comparison"
    return "context"


def _citation_id(item: dict[str, Any]) -> str:
    citation = item.get("citation") or {}
    if hasattr(citation, "citation_id"):
        return str(citation.citation_id)
    return str(citation.get("citation_id") or item.get("evidence_id") or "evidence")


def _evidence_key(item: dict[str, Any]) -> str:
    return str(item.get("evidence_id") or _citation_id(item))


def _partial_refusal_rules(tasks: list[AnswerTaskContract]) -> list[str]:
    """局部拒答规则：只拒答缺证据任务，不让一个缺口吞掉整题。"""
    deficient = [task.task_id for task in tasks if task.required and task.status != "covered"]
    if not deficient:
        return ["证据支持的任务正常回答，不得把长期记忆当教材证据。"]
    return [
        "继续回答已有证据支持的任务。",
        (f"仅对缺证据或证据不完整的任务说明“当前资料未直接给出该部分”：{', '.join(deficient)}。"),
        "不得用其他任务的 chunk 补齐缺失任务。",
        "只有全部必答任务都缺证据时，才可整体说明资料不足。",
    ]
