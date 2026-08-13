"""aggregate_results / deduplicate_and_rerank / evaluate_evidence 节点（方案 §13/§14）。

- 聚合：按 corpus_id + chunk_id 去重、合并 matched_subquery_ids（§13.1）；
- 相邻合并：同 corpus/book/chapter 且 chunk_index 连续时前后各 1 个，
  总计最多 3 个原始 chunk、合并后最多 1,500 tokens，禁止链式扩张（Q7/§13.1 #5）；
- Citation snippet 由服务端从 content_text 确定性生成（Q6/§13.3）；
- 证据预算：按 Evidence Token 预算截断（§13.1 #8）；
- evaluate_evidence：Structured EvidenceAssessment；EVIDENCE_LOOP_ENABLED=false
  直接判定 sufficient（附录 A.10）。
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from backend.conversation.contracts.api import Citation
from backend.conversation.contracts.graph import EvidenceAssessment
from backend.conversation.contracts.retrieval import (
    EvidenceSet,
    MergedEvidence,
    SearchHitRef,
)
from backend.conversation.graph.state import ConversationRuntimeContext
from backend.conversation.services.token_counter import TokenCounter


async def aggregate_results(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
) -> dict[str, Any]:
    """收集成功 Worker 的 SearchHit，按 corpus_id + chunk_id 去重（§13.1 #1-#4）。"""
    plan_revision = int(state.get("plan_revision") or 0)
    worker_results = state.get("worker_results") or {}
    hits: list[SearchHitRef] = []
    seen: set[tuple[str, str]] = set()
    matched: dict[tuple[str, str], set[str]] = {}
    for key, raw in worker_results.items():
        revision = int(key.split(":", 1)[0])
        if revision != plan_revision:
            continue  # 旧 revision 不混入本轮证据（§10.3）
        if raw.get("status") != "succeeded":
            continue
        for hit in raw.get("hits") or []:
            hit_ref = SearchHitRef(**hit)
            dedup_key = (hit_ref.corpus_id, hit_ref.chunk_id)
            if dedup_key in seen:
                matched[dedup_key].add(str(raw.get("subquery_id", "")))
                continue
            seen.add(dedup_key)
            matched[dedup_key] = {str(raw.get("subquery_id", ""))}
            hits.append(hit_ref)
    # 把 matched_subquery_ids 合并回 hit（跨子问题覆盖度，§13.1 #4）
    return {
        "evidence_hits": hits,
        "matched_subquery_ids": {k: sorted(v) for k, v in matched.items()},
    }


async def deduplicate_and_rerank(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
    settings: Any,
    token_counter: TokenCounter,
) -> dict[str, Any]:
    """相邻合并 + 确定性重排 + 证据预算截断 + Citation 生成（§13.1/§13.2/§13.3）。"""
    import dataclasses

    raw_hits = state.get("evidence_hits") or []
    hits = [hit if isinstance(hit, SearchHitRef) else SearchHitRef(**hit) for hit in raw_hits]
    matched = state.get("matched_subquery_ids") or {}
    merged = _merge_adjacent_chunks(hits, matched, settings)
    # 确定性重排（§13.2）：命中子问题数量降序 + score 降序 + chunk_id 稳定 tie-break
    merged.sort(key=lambda e: (-len(e.matched_subquery_ids), -e.score, e.evidence_id))
    # 证据预算截断（§13.1 #8）
    budget = settings.conversation_evidence_token_budget
    kept: list[MergedEvidence] = []
    total = 0
    truncated = 0
    for item in merged:
        if total + item.token_count > budget and kept:
            truncated += item.token_count
            continue
        total += item.token_count
        kept.append(item)
    evidence_set = EvidenceSet(items=tuple(kept), total_tokens=total)

    def _item_dict(item: MergedEvidence) -> dict[str, Any]:
        out = dataclasses.asdict(item)
        citation = out["citation"]
        if hasattr(citation, "model_dump"):
            out["citation"] = citation.model_dump(mode="json")
        return out

    # §17.4.1：citation.available 事件（评审 C2：流式引用可达前端）。
    # P2（第三轮评审）：只发一次——补检索重跑本节点时不重复发送整组引用，
    # 避免前端累积重复引用（前端按 citation_id 去重亦可行，此处源头去重）。
    citations_emitted = False
    if kept and runtime is not None and not state.get("_citations_emitted"):
        await _emit_citation_available(runtime, state, kept)
        citations_emitted = True

    return {
        "evidence_set": {
            "items": [_item_dict(item) for item in evidence_set.items],
            "total_tokens": evidence_set.total_tokens,
        },
        "evidence_assessment": None,
        "_citations_emitted": citations_emitted or bool(state.get("_citations_emitted")),
    }


async def _emit_citation_available(
    runtime: ConversationRuntimeContext,
    state: dict[str, Any],
    kept: list[MergedEvidence],
) -> None:
    """citation.available 事件（§17.4.1）：每证据一条，供前端实时渲染引用。"""
    from backend.conversation.contracts.events import TurnEventWrite

    repo = runtime.conversation_repository
    if repo is None or repo.session_factory is None:
        return
    turn_id = state["turn_id"]
    request_id = str(state.get("request_id") or "")
    run_id = str(state.get("run_id") or "")
    async with repo.session_factory() as session:
        async with session.begin():
            for item in kept:
                citation = item.citation
                await runtime.turn_event_writer.append(
                    session,
                    write=TurnEventWrite(
                        turn_id=turn_id,
                        event_type="citation.available",
                        request_id=request_id,
                        run_id=run_id,
                        payload={"citation": citation.model_dump(mode="json")},
                    ),
                )


def _merge_adjacent_chunks(
    hits: list[SearchHitRef],
    matched: dict[tuple[str, str], set[str]],
    settings: Any,
) -> list[MergedEvidence]:
    """§13.1 #5：同 corpus/book/chapter 且 chunk_index 连续时合并（最多 3 个原始 chunk）。"""
    per_side = settings.conversation_retrieval_adjacent_chunks_per_side
    max_tokens = settings.conversation_retrieval_merged_hit_max_tokens
    # 按 (corpus, book, chapter, chunk_index) 分组
    groups: dict[tuple[Any, ...], list[SearchHitRef]] = {}
    for hit in hits:
        key = (hit.corpus_id, hit.book_id, tuple(hit.chapter_path))
        groups.setdefault(key, []).append(hit)
    merged_items: list[MergedEvidence] = []
    for _, group in groups.items():
        group.sort(key=lambda h: h.chunk_index)
        used: set[int] = set()
        for i, hit in enumerate(group):
            if i in used:
                continue
            # 取前后各 per_side 个连续 chunk
            cluster = [hit]
            j = i + 1
            while j < len(group) and len(cluster) < (2 * per_side + 1) and j not in used:
                if group[j].chunk_index == cluster[-1].chunk_index + 1:
                    cluster.append(group[j])
                    used.add(j)
                    j += 1
                else:
                    break
            j = i - 1
            while j >= 0 and len(cluster) < (2 * per_side + 1) and j not in used:
                if group[j].chunk_index == cluster[0].chunk_index - 1:
                    cluster.insert(0, group[j])
                    used.add(j)
                    j -= 1
                else:
                    break
            used.add(i)
            cluster.sort(key=lambda h: h.chunk_index)
            tokens = sum(h.token_count or 0 for h in cluster)
            if len(cluster) > 1 and tokens > max_tokens:
                # 超限则不合并（保留最高分原始 chunk），禁止链式扩张（Q7）
                best = max(cluster, key=lambda h: h.score)
                merged_items.append(_to_merged(best, [best], matched))
                continue
            merged_items.append(_to_merged(cluster[0], cluster, matched))
    return merged_items


def _to_merged(
    primary: SearchHitRef,
    cluster: list[SearchHitRef],
    matched: dict[tuple[str, str], set[str]],
) -> MergedEvidence:
    subquery_ids: set[str] = set()
    for hit in cluster:
        subquery_ids.update(matched.get((hit.corpus_id, hit.chunk_id), set()))
    content = "\n".join(hit.content_text for hit in cluster)
    refs: list[dict[str, Any]] = []
    for hit in cluster:
        refs.extend(dict(r) for r in hit.source_refs)
    evidence_id = str(uuid4())
    page_start = min(h.source_page_start or 0 for h in cluster)
    page_end = max(h.source_page_end or 0 for h in cluster)
    citation = Citation(
        # 第三轮必改 2：citation_id 固定 12 位 hex（与 answer.py 校验正则
        # \bC[0-9a-f]{12}\b 一致，模型按证据行引用时才能被验证器识别）。
        citation_id=f"C{evidence_id.replace('-', '')[:12]}",
        corpus_id=primary.corpus_id,
        chunk_ids=[h.chunk_id for h in cluster],
        book_id=primary.book_id,
        book_name=primary.book_name,
        chapter_path=list(primary.chapter_path),
        page_start=page_start or None,
        page_end=page_end or None,
        snippet=_deterministic_snippet(primary.content_text, max_chars=300),
        source_refs=refs,
        matched_subquery_ids=sorted(subquery_ids),
    )
    return MergedEvidence(
        evidence_id=evidence_id,
        chunk_ids=tuple(h.chunk_id for h in cluster),
        corpus_id=primary.corpus_id,
        book_id=primary.book_id,
        book_name=primary.book_name,
        chapter_path=primary.chapter_path,
        content_role=primary.content_role,
        content_text=content,
        token_count=sum(h.token_count or 0 for h in cluster),
        score=max(h.score for h in cluster),
        matched_subquery_ids=tuple(sorted(subquery_ids)),
        source_refs=tuple(refs),
        citation=citation,
    )


def _deterministic_snippet(content: str, *, max_chars: int = 300) -> str:
    """服务端确定性摘录（Q6/§13.3）：优先命中附近，兜底取开头，规范化空白。"""
    normalized = re.sub(r"\s+", " ", content).strip()
    if len(normalized) <= max_chars:
        return normalized
    # 优先在开头/标点附近切分，避免截断单词
    head = normalized[:max_chars]
    cut = max(head.rfind("。"), head.rfind("；"), head.rfind("，"), head.rfind(". "))
    if cut > max_chars // 2:
        return head[: cut + 1]
    return head


async def evaluate_evidence(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
) -> dict[str, Any]:
    flags = runtime.flags
    if not flags.get("evidence_loop", True) or not flags.get("agentic_rag", True):
        # 附录 A.10：EVIDENCE_LOOP 或 AGENTIC_RAG 关闭时跳过评估直接进 answer
        return {
            "evidence_assessment": EvidenceAssessment(
                status="sufficient", reason_codes=["evidence_loop_disabled"]
            ).model_dump(mode="json")
        }
    evidence_set = state.get("evidence_set") or {}
    items = evidence_set.get("items") or []
    summary = "\n".join(_evidence_line(item) for item in items)
    assessment_raw = await runtime.openai_gateway.assess_evidence(
        question=str(state.get("rewrite_plan", {}).get("standalone_question") or ""),
        evidence_summary=summary[:8000] or "（无证据）",
        budget_remaining=str(evidence_set.get("total_tokens") or 0),
    )
    assessment = EvidenceAssessment.model_validate(assessment_raw)
    # retrieval_iteration 的递增由 rewrite 节点在补检索轮处理（builder._node_rewrite），
    # evaluate 只产出评估结果（第三轮必改 3）。
    return {"evidence_assessment": assessment.model_dump(mode="json")}


def _evidence_line(item: dict[str, Any]) -> str:
    """证据行：兼容 dataclass 与 dict 两种形态（checkpoint 恢复后）。"""
    citation = item.get("citation") or {}
    if hasattr(citation, "citation_id"):
        citation_id = citation.citation_id
    else:
        citation_id = citation.get("citation_id", "")
    content = str(item.get("content_text") or "")
    return f"[{citation_id}] {content[:300]}"
