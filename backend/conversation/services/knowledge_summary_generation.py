"""知识总结 Generation Job 执行服务（知识总结方案 §9–§14）。

本模块把冻结输入清单、确定性过滤、候选召回、Structured Output 校验和短事务提交
串成一个可 fencing 的 Worker 流程。模型只提出受约束计划，所有 ID、来源、版本、
敏感信息和用户保护规则都由本模块确定性执行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation import metrics
from backend.conversation.contracts.knowledge_summary import (
    MAX_ITEM_SOURCES,
    MAX_SECTION_ITEMS,
    MAX_SUMMARY_ARRAY_ITEMS,
    AppendItemMutation,
    CandidateItem,
    CreateSummaryPlan,
    IgnoreItemMutation,
    IgnoreOverviewMutation,
    KnowledgeCandidate,
    KnowledgeExtractionResult,
    KnowledgeMergePlanResult,
    KnowledgeSummaryContent,
    KnowledgeSummaryItem,
    MergeItemSourceMutation,
    MergeOverviewSourceMutation,
    MergeSummaryPlan,
    NeedsReviewSummaryPlan,
    NoChangeSummaryPlan,
    ReplaceItemMutation,
    ReplaceOverviewMutation,
    SetOverviewMutation,
    validate_merge_plan_against_candidates,
)
from backend.conversation.gateways.knowledge_summary_openai import (
    EXTRACT_PROMPT_VERSION,
    EXTRACT_SCHEMA_VERSION,
    MERGE_PROMPT_VERSION,
    MERGE_SCHEMA_VERSION,
    KnowledgeSummaryGateway,
    KnowledgeSummaryGatewayError,
    build_request_hash,
)
from backend.conversation.knowledge_summary.normalization import (
    KNOWLEDGE_CANONICAL_VERSION,
    build_search_text,
    canonicalize_item_text_v1,
    canonicalize_quote_v1,
    canonicalize_title_v1,
    content_hash_v1,
    state_hash_v1,
)
from backend.conversation.persistence import knowledge_summaries as summaries_repo
from backend.conversation.persistence import knowledge_summary_generations as generations_repo
from backend.conversation.services.token_counter import TokenCounter

logger = logging.getLogger("conversation.services.knowledge_summary_generation")

_SENSITIVE_PATTERNS = (
    re.compile(r"-----BEGIN .* PRIVATE KEY-----", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\b(?:API[_ -]?KEY|PASSWORD|SECRET)\s*=", re.I),
)
_SENSITIVE_LABELS = re.compile(
    r"(?:邮箱|手机号|身份证|银行卡|token|密码|api[_ -]?key|secret)", re.I
)

MAX_SUMMARY_CANONICAL_CHARS = 24_000


class GenerationCancelled(Exception):
    """输入已经过时、来源被删除或运行条件关闭，Job 不应复活。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GenerationRetryable(Exception):
    """暂时性模型、数据库或版本冲突，允许 Job 退避重试。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GenerationDeadLetter(Exception):
    """稳定的业务校验或不可重试错误。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LeaseLost(Exception):
    """旧 Worker 已失去 lease，不能再执行任何数据库副作用。"""


class GenerationVersionStale(Exception):
    """合并目标版本变化，需要重新生成 merge plan。"""


class SummaryIdentityConflict(Exception):
    """创建总结命中 active identity 唯一索引，需要重新召回并规划。"""


@dataclass(frozen=True)
class FrozenInput:
    """冻结 manifest、模型可见消息和来源事实。"""

    manifest: dict[str, Any]
    messages: dict[UUID, dict[str, Any]]
    conversation_summary: dict[str, Any] | None


@dataclass(frozen=True)
class CandidateContext:
    """候选及其召回结果，供 merge plan 和提交阶段复用。"""

    candidate: KnowledgeCandidate
    recalled: list[dict[str, Any]]
    ambiguous_exact: bool
    exact_target_id: UUID | None


class KnowledgeSummaryGenerationService:
    """单个 Generation Job 的完整执行器。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: Any,
        gateway: KnowledgeSummaryGateway,
        token_counter: TokenCounter,
        worker_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._gateway = gateway
        self._token_counter = token_counter
        self._worker_id = worker_id
        self._execute_task: asyncio.Task[Any] | None = None

    async def execute(self, row: dict[str, Any]) -> None:
        """执行一个已 claim 的 Job；终态写回与内容提交严格带 fencing。"""
        generation_id = row["generation_id"]
        lease_lost = asyncio.Event()
        self._execute_task = asyncio.current_task()
        heartbeat = asyncio.create_task(self._lease_heartbeat(row, lease_lost))
        try:
            self._ensure_generation_enabled()
            frozen = await self._freeze_or_validate_input(row)
            extraction = await self._get_or_run_extraction(row, frozen)
            candidates, warnings = _filter_candidates(
                extraction,
                frozen.messages,
                trigger=str(row["trigger"]),
                config=self._config,
            )
            metrics.knowledge_summary_candidates_total.labels(disposition="model_output").inc(
                len(extraction.candidates)
            )
            metrics.knowledge_summary_candidates_total.labels(disposition="accepted").inc(
                len(candidates)
            )
            if len(extraction.candidates) > len(candidates):
                metrics.knowledge_summary_candidates_total.labels(disposition="filtered").inc(
                    len(extraction.candidates) - len(candidates)
                )
            if not candidates:
                await self._finish(row, "no_change", warning_codes=warnings)
                return

            for replan_attempt in range(2):
                contexts = await self._recall(row, candidates)
                merge_plan = await self._get_or_run_merge_plan(row, frozen, candidates, contexts)
                try:
                    await self._commit(
                        row,
                        frozen=frozen,
                        candidates=candidates,
                        contexts=contexts,
                        merge_plan=merge_plan,
                        warning_codes=warnings,
                    )
                    break
                except SummaryIdentityConflict:
                    await self._clear_merge_plan(row)
                    row["merge_plan_result"] = None
                    if replan_attempt == 1:
                        raise GenerationRetryable("KNOWLEDGE_SUMMARY_IDENTITY_CONFLICT") from None
            else:
                raise AssertionError("知识总结重新规划循环未完成")
        except asyncio.CancelledError:
            if lease_lost.is_set():
                logger.info("Generation lease 续租失败，取消旧 Worker: %s", generation_id)
                return
            raise
        except GenerationCancelled as exc:
            await self._finish(row, "cancelled", error_code=exc.code)
        except GenerationDeadLetter as exc:
            await self._dead_letter(row, exc.code)
        except GenerationRetryable as exc:
            await self._retry(row, exc.code)
        except GenerationVersionStale:
            async with self._session_factory() as session:
                async with session.begin():
                    if not await generations_repo.clear_merge_plan(
                        session,
                        row["generation_id"],
                        worker_id=self._worker_id,
                        lease_generation=int(row["lease_generation"]),
                    ):
                        raise LeaseLost from None
            row["merge_plan_result"] = None
            await self._retry(row, "KNOWLEDGE_SUMMARY_VERSION_STALE")
        except LeaseLost:
            logger.info("Generation lease 已失效，放弃旧 Worker 副作用: %s", generation_id)
        except Exception:
            logger.exception("知识总结 Generation Job 未分类异常: %s", generation_id)
            await self._retry(row, "KNOWLEDGE_SUMMARY_DATABASE_TRANSIENT")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._execute_task = None

    async def _clear_merge_plan(self, row: Mapping[str, Any]) -> None:
        """在当前 fencing lease 下清除旧计划，以最新召回结果重新规划。"""
        async with self._session_factory() as session:
            async with session.begin():
                if not await generations_repo.clear_merge_plan(
                    session,
                    row["generation_id"],
                    worker_id=self._worker_id,
                    lease_generation=int(row["lease_generation"]),
                ):
                    raise LeaseLost

    async def _lease_heartbeat(self, row: Mapping[str, Any], lease_lost: asyncio.Event) -> None:
        """以小于 lease 一半的周期续租；失败时取消当前模型调用。"""
        lease_seconds = int(
            getattr(self._config, "conversation_knowledge_summary_lease_seconds", 60)
        )
        interval = max(0.1, lease_seconds / 3)
        try:
            while True:
                await asyncio.sleep(interval)
                async with self._session_factory() as session:
                    async with session.begin():
                        renewed = await generations_repo.renew_generation_lease(
                            session,
                            row["generation_id"],
                            worker_id=self._worker_id,
                            lease_generation=int(row["lease_generation"]),
                            lease_seconds=lease_seconds,
                        )
                if renewed:
                    continue
                logger.warning("Generation lease 续租未命中: %s", row["generation_id"])
                lease_lost.set()
                if self._execute_task is not None:
                    self._execute_task.cancel()
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Generation lease 续租异常: %s", row["generation_id"])
            lease_lost.set()
            if self._execute_task is not None:
                self._execute_task.cancel()

    def _ensure_generation_enabled(self) -> None:
        if not getattr(self._config, "conversation_knowledge_summary_enabled", False):
            raise GenerationCancelled("KNOWLEDGE_SUMMARY_FEATURE_DISABLED")
        if not getattr(self._config, "conversation_knowledge_summary_generation_enabled", False):
            raise GenerationCancelled("KNOWLEDGE_SUMMARY_GENERATION_DISABLED")

    async def _freeze_or_validate_input(self, row: dict[str, Any]) -> FrozenInput:
        async with self._session_factory() as session:
            async with session.begin():
                source = await summaries_repo.get_generation_source_rows(
                    session,
                    thread_id=row["thread_id"],
                    turn_id=row["turn_id"],
                    user_id=row["user_id"],
                )
                if source is None:
                    raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")
                _validate_primary_source(source, row)
                frozen = await _build_frozen_input(
                    session,
                    source=source,
                    row=row,
                    token_counter=self._token_counter,
                    config=self._config,
                )
                stored = row.get("input_manifest")
                if stored is not None:
                    if stored.get("input_hash") != frozen.manifest.get("input_hash"):
                        raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")
                else:
                    fenced = await generations_repo.update_generation_payload(
                        session,
                        row["generation_id"],
                        worker_id=self._worker_id,
                        lease_generation=int(row["lease_generation"]),
                        input_manifest=frozen.manifest,
                    )
                    if not fenced:
                        raise LeaseLost
                return frozen

    async def _get_or_run_extraction(
        self, row: dict[str, Any], frozen: FrozenInput
    ) -> KnowledgeExtractionResult:
        if row.get("extraction_result") is not None:
            return _rehydrate_extraction(row["extraction_result"], frozen.messages)

        request = _build_extract_request(frozen)
        request_hash = build_request_hash(
            model=_gateway_model_name(self._gateway, self._config),
            purpose="extract",
            prompt_version=EXTRACT_PROMPT_VERSION,
            schema_version=EXTRACT_SCHEMA_VERSION,
            input_manifest_hash=str(frozen.manifest["input_hash"]),
            existing_summaries=[],
            request=request,
        )
        cached = await self._cached_call(row, "extract", request_hash)
        if cached is not None and cached.get("response_payload") is not None:
            return _rehydrate_extraction(cached["response_payload"], frozen.messages)

        try:
            result, usage = await self._gateway.extract(request)
            # 先做来源/quote 业务校验；敏感信息过滤在候选过滤阶段完成。
            _validate_extraction_sources(result, frozen.messages)
            scrubbed = _scrub_extraction(result, frozen.messages)
        except KnowledgeSummaryGatewayError as exc:
            await self._record_failed_call(row, "extract", request_hash, exc.code)
            if exc.retryable:
                raise GenerationRetryable(exc.code) from exc
            raise GenerationDeadLetter(exc.code) from exc
        except ValueError as exc:
            await self._record_failed_call(
                row, "extract", request_hash, "KNOWLEDGE_SUMMARY_SOURCE_VALIDATION"
            )
            if await self._failed_call_count(row, "extract") >= 2:
                raise GenerationDeadLetter("KNOWLEDGE_SUMMARY_SOURCE_VALIDATION") from exc
            raise GenerationRetryable("KNOWLEDGE_SUMMARY_SOURCE_VALIDATION") from exc

        async with self._session_factory() as session:
            async with session.begin():
                if not await generations_repo.update_generation_payload(
                    session,
                    row["generation_id"],
                    worker_id=self._worker_id,
                    lease_generation=int(row["lease_generation"]),
                    extraction_result=scrubbed,
                ):
                    raise LeaseLost
                await generations_repo.insert_model_call(
                    session,
                    call_id=uuid4(),
                    generation_id=row["generation_id"],
                    purpose="extract",
                    model_name=_gateway_model_name(self._gateway, self._config),
                    prompt_version=EXTRACT_PROMPT_VERSION,
                    schema_version=EXTRACT_SCHEMA_VERSION,
                    request_hash=request_hash,
                    response_payload=scrubbed,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    latency_ms=int(usage.get("latency_ms") or 0),
                    status="succeeded",
                )
        row["extraction_result"] = scrubbed
        return result

    async def _recall(
        self, row: dict[str, Any], candidates: list[KnowledgeCandidate]
    ) -> list[CandidateContext]:
        contexts: list[CandidateContext] = []
        async with self._session_factory() as session:
            for candidate in candidates:
                group = canonicalize_title_v1(candidate.topic_group_title, max_length=160)
                title = canonicalize_title_v1(candidate.topic_title, max_length=240)
                aliases = [
                    canonicalize_title_v1(alias, max_length=240) for alias in candidate.aliases
                ]
                recalled = await summaries_repo.recall_summary_candidates(
                    session,
                    user_id=row["user_id"],
                    topic_group=group,
                    topic_title=title,
                    aliases=aliases,
                )
                exact = [item for item in recalled if int(item["exact_kind"]) > 0]
                exact_target = UUID(str(exact[0]["summary_id"])) if len(exact) == 1 else None
                contexts.append(
                    CandidateContext(
                        candidate=candidate,
                        recalled=recalled,
                        ambiguous_exact=len(exact) > 1,
                        exact_target_id=exact_target,
                    )
                )
        return contexts

    async def _get_or_run_merge_plan(
        self,
        row: dict[str, Any],
        frozen: FrozenInput,
        candidates: list[KnowledgeCandidate],
        contexts: list[CandidateContext],
    ) -> KnowledgeMergePlanResult:
        if row.get("merge_plan_result") is not None:
            result = KnowledgeMergePlanResult.model_validate(row["merge_plan_result"])
            validate_merge_plan_against_candidates(result, candidates)
            _validate_merge_targets(result, contexts, candidates)
            return result

        request = _build_merge_request(frozen, candidates, contexts, row)
        existing = [
            {
                "summary_id": str(summary["summary_id"]),
                "version": int(summary["version"]),
                "state_hash": str(summary["state_hash"]),
            }
            for context in contexts
            for summary in context.recalled
        ]
        request_hash = build_request_hash(
            model=_gateway_model_name(self._gateway, self._config),
            purpose="merge_plan",
            prompt_version=MERGE_PROMPT_VERSION,
            schema_version=MERGE_SCHEMA_VERSION,
            input_manifest_hash=str(frozen.manifest["input_hash"]),
            existing_summaries=existing,
            request=request,
        )
        cached = await self._cached_call(row, "merge_plan", request_hash)
        if cached is not None and cached.get("response_payload") is not None:
            result = KnowledgeMergePlanResult.model_validate(cached["response_payload"])
            validate_merge_plan_against_candidates(result, candidates)
            _validate_merge_targets(result, contexts, candidates)
            return result

        try:
            result, usage = await self._gateway.merge_plan(request)
            validate_merge_plan_against_candidates(result, candidates)
            _validate_merge_targets(result, contexts, candidates)
            scrubbed = result.model_dump(mode="json")
        except KnowledgeSummaryGatewayError as exc:
            await self._record_failed_call(row, "merge_plan", request_hash, exc.code)
            if exc.retryable:
                raise GenerationRetryable(exc.code) from exc
            raise GenerationDeadLetter(exc.code) from exc
        except ValueError as exc:
            await self._record_failed_call(
                row, "merge_plan", request_hash, "KNOWLEDGE_SUMMARY_PLAN_INVALID"
            )
            if await self._failed_call_count(row, "merge_plan") >= 2:
                raise GenerationDeadLetter("KNOWLEDGE_SUMMARY_PLAN_INVALID") from exc
            raise GenerationRetryable("KNOWLEDGE_SUMMARY_PLAN_INVALID") from exc

        async with self._session_factory() as session:
            async with session.begin():
                if not await generations_repo.update_generation_payload(
                    session,
                    row["generation_id"],
                    worker_id=self._worker_id,
                    lease_generation=int(row["lease_generation"]),
                    merge_plan_result=scrubbed,
                ):
                    raise LeaseLost
                await generations_repo.insert_model_call(
                    session,
                    call_id=uuid4(),
                    generation_id=row["generation_id"],
                    purpose="merge_plan",
                    model_name=_gateway_model_name(self._gateway, self._config),
                    prompt_version=MERGE_PROMPT_VERSION,
                    schema_version=MERGE_SCHEMA_VERSION,
                    request_hash=request_hash,
                    response_payload=scrubbed,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    latency_ms=int(usage.get("latency_ms") or 0),
                    status="succeeded",
                )
        row["merge_plan_result"] = scrubbed
        return result

    async def _commit(
        self,
        row: dict[str, Any],
        *,
        frozen: FrozenInput,
        candidates: list[KnowledgeCandidate],
        contexts: list[CandidateContext],
        merge_plan: KnowledgeMergePlanResult,
        warning_codes: list[str],
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await _lock_fenced_generation(
                    session,
                    row["generation_id"],
                    worker_id=self._worker_id,
                    lease_generation=int(row["lease_generation"]),
                )
                current = await summaries_repo.get_generation_source_rows(
                    session,
                    thread_id=row["thread_id"],
                    turn_id=row["turn_id"],
                    user_id=row["user_id"],
                )
                if current is None:
                    raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")
                _validate_primary_source(current, row)
                if row.get("input_manifest") is not None and row["input_manifest"].get(
                    "input_hash"
                ) != frozen.manifest.get("input_hash"):
                    raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")

                target_ids = [
                    plan.target_summary_id
                    for plan in merge_plan.plans
                    if isinstance(plan, MergeSummaryPlan)
                ]
                target_ids.extend(
                    target_id
                    for plan in merge_plan.plans
                    if isinstance(plan, CreateSummaryPlan)
                    for target_id in plan.possible_duplicate_target_ids
                )
                target_ids.extend(
                    target_id
                    for plan in merge_plan.plans
                    if isinstance(plan, NeedsReviewSummaryPlan)
                    for target_id in plan.target_summary_ids
                )
                locked = await summaries_repo.lock_summary_rows(
                    session, user_id=row["user_id"], summary_ids=target_ids
                )
                locked_by_id = {UUID(str(item["summary_id"])): item for item in locked}
                if len(locked_by_id) != len(set(target_ids)):
                    raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")
                if any(
                    isinstance(plan, MergeSummaryPlan)
                    and int(locked_by_id[plan.target_summary_id]["version"]) != plan.target_version
                    for plan in merge_plan.plans
                ):
                    raise GenerationVersionStale

                affected: list[UUID] = []
                had_change = False
                had_review = False
                for plan in merge_plan.plans:
                    candidate = candidates[plan.candidate_index]
                    if isinstance(plan, CreateSummaryPlan):
                        suppressed = await _candidate_tombstone_suppressed(
                            session, row=row, candidate=candidate
                        )
                        if suppressed == "suppressed":
                            warning_codes.append("DELETED_TOPIC_OLD_SOURCE")
                            continue
                        if suppressed == "ambiguous":
                            warning_codes.append("AMBIGUOUS_DELETED_TOPIC")
                            continue
                        try:
                            async with session.begin_nested():
                                created_summary_ids = await _apply_create(
                                    session,
                                    row=row,
                                    candidate=candidate,
                                    plan=plan,
                                    frozen=frozen,
                                    duplicate_targets=locked_by_id,
                                )
                        except IntegrityError as exc:
                            if _is_exact_topic_conflict(exc):
                                raise SummaryIdentityConflict from exc
                            raise
                        affected.extend(created_summary_ids)
                        had_change = True
                    elif isinstance(plan, MergeSummaryPlan):
                        suppressed = await _candidate_tombstone_suppressed(
                            session, row=row, candidate=candidate
                        )
                        if suppressed == "suppressed":
                            warning_codes.append("DELETED_TOPIC_OLD_SOURCE")
                            continue
                        if suppressed == "ambiguous":
                            warning_codes.append("AMBIGUOUS_DELETED_TOPIC")
                            continue
                        changed = await _apply_merge(
                            session,
                            row=row,
                            candidate=candidate,
                            plan=plan,
                            target=locked_by_id[plan.target_summary_id],
                            frozen=frozen,
                            warning_codes=warning_codes,
                        )
                        affected.append(plan.target_summary_id)
                        had_change = had_change or changed
                    elif isinstance(plan, NeedsReviewSummaryPlan):
                        had_review = True
                        for target_id in plan.target_summary_ids:
                            await summaries_repo.insert_review_and_mark_conflict(
                                session,
                                review_id=uuid4(),
                                generation_id=row["generation_id"],
                                summary=locked_by_id[target_id],
                                candidate_index=plan.candidate_index,
                                reason_code=plan.reason_code,
                                internal_reason=plan.reason,
                                proposed_content={
                                    "proposed_topic_title": candidate.topic_title,
                                    "proposed_sections": plan.proposed_sections,
                                },
                                generation_id_for_revision=row["generation_id"],
                            )
                            affected.append(target_id)
                    elif isinstance(plan, NoChangeSummaryPlan):
                        continue

                terminal = (
                    "needs_review" if had_review else ("succeeded" if had_change else "no_change")
                )
                if not await generations_repo.finish_generation(
                    session,
                    row["generation_id"],
                    worker_id=self._worker_id,
                    lease_generation=int(row["lease_generation"]),
                    status=terminal,
                    affected_summary_ids=sorted(set(affected), key=str),
                    warning_codes=sorted(set(warning_codes)),
                ):
                    raise LeaseLost
        self._observe_plan_metrics(candidates, merge_plan)
        self._observe_terminal(row, terminal)

    @staticmethod
    def _observe_plan_metrics(
        candidates: list[KnowledgeCandidate], merge_plan: KnowledgeMergePlanResult
    ) -> None:
        """在事务成功后记录候选处置、合并决策、review 和条目变更指标。"""
        for plan in merge_plan.plans:
            candidate = candidates[plan.candidate_index]
            if isinstance(plan, CreateSummaryPlan):
                metrics.knowledge_summary_merge_total.labels(decision="create").inc()
                if candidate.overview is not None:
                    metrics.knowledge_summary_item_mutations_total.labels(
                        section="overview", action="create"
                    ).inc()
                for item in candidate.items:
                    metrics.knowledge_summary_item_mutations_total.labels(
                        section=item.section, action="create"
                    ).inc()
            elif isinstance(plan, MergeSummaryPlan):
                metrics.knowledge_summary_merge_total.labels(decision="merge").inc()
                if plan.overview_mutation is not None:
                    metrics.knowledge_summary_item_mutations_total.labels(
                        section="overview", action=_mutation_action(plan.overview_mutation)
                    ).inc()
                for mutation in plan.item_mutations:
                    item = candidate.items[mutation.candidate_item_index]
                    metrics.knowledge_summary_item_mutations_total.labels(
                        section=item.section, action=_mutation_action(mutation)
                    ).inc()
            elif isinstance(plan, NoChangeSummaryPlan):
                metrics.knowledge_summary_merge_total.labels(decision="no_change").inc()
            elif isinstance(plan, NeedsReviewSummaryPlan):
                metrics.knowledge_summary_merge_total.labels(decision="needs_review").inc()
                metrics.knowledge_summary_review_total.labels(reason=plan.reason_code).inc()

    async def _cached_call(
        self, row: dict[str, Any], purpose: str, request_hash: str
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            return await generations_repo.get_cached_model_call(
                session,
                generation_id=row["generation_id"],
                purpose=purpose,
                request_hash=request_hash,
            )

    async def _record_failed_call(
        self, row: dict[str, Any], purpose: str, request_hash: str, error_code: str
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                if not await _is_fenced(session, row):
                    raise LeaseLost
                await generations_repo.insert_model_call(
                    session,
                    call_id=uuid4(),
                    generation_id=row["generation_id"],
                    purpose=purpose,
                    model_name=_gateway_model_name(self._gateway, self._config),
                    prompt_version=EXTRACT_PROMPT_VERSION
                    if purpose == "extract"
                    else MERGE_PROMPT_VERSION,
                    schema_version=EXTRACT_SCHEMA_VERSION
                    if purpose == "extract"
                    else MERGE_SCHEMA_VERSION,
                    request_hash=request_hash,
                    response_payload=None,
                    input_tokens=None,
                    output_tokens=None,
                    latency_ms=0,
                    status="failed",
                    error_code=error_code,
                )

    async def _failed_call_count(self, row: dict[str, Any], purpose: str) -> int:
        async with self._session_factory() as session:
            return await generations_repo.count_failed_model_calls(
                session, generation_id=row["generation_id"], purpose=purpose
            )

    def _observe_terminal(self, row: Mapping[str, Any], status: str) -> None:
        """记录 Job 终态与耗时；字段只包含受控标签，不写正文。"""
        trigger = str(row.get("trigger", "unknown"))
        metrics.knowledge_summary_jobs_total.labels(trigger=trigger, status=status).inc()
        created_at = row.get("created_at")
        if isinstance(created_at, datetime):
            elapsed = max(0.0, (datetime.now(UTC) - created_at).total_seconds())
            metrics.knowledge_summary_job_duration_seconds.labels(
                trigger=trigger, status=status
            ).observe(elapsed)

    async def _finish(
        self,
        row: dict[str, Any],
        status: str,
        *,
        warning_codes: list[str] | None = None,
        error_code: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                if not await generations_repo.finish_generation(
                    session,
                    row["generation_id"],
                    worker_id=self._worker_id,
                    lease_generation=int(row["lease_generation"]),
                    status=status,
                    warning_codes=warning_codes,
                    error_code=error_code,
                ):
                    raise LeaseLost
        self._observe_terminal(row, status)

    async def _dead_letter(self, row: dict[str, Any], code: str) -> None:
        await self._finish(row, "dead_letter", error_code=code)

    async def _retry(self, row: dict[str, Any], code: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                max_attempts = int(
                    getattr(self._config, "conversation_knowledge_summary_max_attempts", 5)
                )
                if int(row["attempt_count"]) >= max_attempts:
                    if not await generations_repo.finish_generation(
                        session,
                        row["generation_id"],
                        worker_id=self._worker_id,
                        lease_generation=int(row["lease_generation"]),
                        status="dead_letter",
                        error_code=code,
                    ):
                        raise LeaseLost
                elif not await generations_repo.retry_generation(
                    session,
                    row["generation_id"],
                    worker_id=self._worker_id,
                    lease_generation=int(row["lease_generation"]),
                    error_code=code,
                    attempt_count=int(row["attempt_count"]),
                ):
                    raise LeaseLost


def _is_exact_topic_conflict(error: IntegrityError) -> bool:
    """仅把 active topic identity 唯一冲突转为重新召回，其余错误照常传播。"""
    original = getattr(error, "orig", None)
    diagnostic = getattr(original, "diag", None)
    constraint_name = getattr(original, "constraint_name", None) or getattr(
        diagnostic, "constraint_name", None
    )
    return constraint_name == "uq_knowledge_summaries_exact_topic"


async def _lock_fenced_generation(
    session: AsyncSession, generation_id: UUID, *, worker_id: str, lease_generation: int
) -> dict[str, Any]:
    result = await session.execute(
        text(
            "SELECT * FROM conversation.knowledge_summary_generation_jobs "
            "WHERE generation_id = :generation_id FOR UPDATE"
        ),
        {"generation_id": generation_id},
    )
    row = result.mappings().first()
    if (
        row is None
        or row["status"] != "processing"
        or row["lease_owner"] != worker_id
        or int(row["lease_generation"]) != lease_generation
    ):
        raise LeaseLost
    return dict(row)


async def _is_fenced(session: AsyncSession, row: Mapping[str, Any]) -> bool:
    result = await session.execute(
        text(
            "SELECT 1 FROM conversation.knowledge_summary_generation_jobs "
            "WHERE generation_id = :generation_id AND status = 'processing'"
            " AND lease_owner = :owner AND lease_generation = :generation"
        ),
        {
            "generation_id": row["generation_id"],
            "owner": row["lease_owner"] if row.get("lease_owner") else "",
            "generation": int(row["lease_generation"]),
        },
    )
    return result.first() is not None


def _validate_primary_source(source: Mapping[str, Any], row: Mapping[str, Any]) -> None:
    """校验 Thread/Turn/消息完整性和冻结 checkpoint。"""
    if source["thread_status"] not in ("active", "archived"):
        raise GenerationCancelled("KNOWLEDGE_SUMMARY_THREAD_DELETED")
    if source["turn_status"] != "completed":
        raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")
    if source["user_status"] != "completed" or source["assistant_status"] != "completed":
        raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")
    if source["assistant_eligible_for_context"] is not True:
        raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")
    if (
        not source["source_checkpoint_id"]
        or source["source_checkpoint_id"] != row["source_checkpoint_id"]
    ):
        raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")
    if source["assistant_role"] != "assistant" or source["user_role"] != "user":
        raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")
    if _timestamp(source["user_occurred_at"]) != _timestamp(row["primary_turn_occurred_at"]):
        raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")


async def _build_frozen_input(
    session: AsyncSession,
    *,
    source: Mapping[str, Any],
    row: Mapping[str, Any],
    token_counter: TokenCounter,
    config: Any,
) -> FrozenInput:
    """按最近连续后缀和 4000 token 上限构造并哈希 manifest。"""
    context_rows = await summaries_repo.list_context_source_messages(
        session,
        thread_id=source["thread_id"],
        before_sequence=int(source["user_sequence"]),
        limit=int(getattr(config, "conversation_knowledge_summary_context_messages", 6)),
    )
    selected: list[dict[str, Any]] = []
    total_tokens = 0
    for context_row in context_rows:
        count = token_counter.count(str(context_row["content"]))
        if total_tokens + count > int(
            getattr(config, "conversation_knowledge_summary_context_token_budget", 4000)
        ):
            break
        selected.append(context_row)
        total_tokens += count

    summary_row = await summaries_repo.get_previous_conversation_summary(
        session, thread_id=source["thread_id"], before_sequence=int(source["user_sequence"])
    )
    summary_payload: dict[str, Any] | None = None
    if summary_row is not None:
        summary_text = token_counter.truncate(str(summary_row["content"]), 1000)
        summary_hash = sha256(str(summary_row["content"]).encode("utf-8")).hexdigest()
        summary_payload = {"sequence": int(summary_row["sequence"]), "content": summary_text}
    else:
        summary_hash = None

    primary_messages = [
        {
            "message_id": str(source["user_message_id_actual"]),
            "role": "user",
            "sequence": int(source["user_sequence"]),
            "content_hash": str(source["user_content_hash"]),
        },
        {
            "message_id": str(source["assistant_message_id_actual"]),
            "role": "assistant",
            "sequence": int(source["assistant_sequence"]),
            "content_hash": str(source["assistant_content_hash"]),
        },
    ]
    context_manifest = [
        {
            "message_id": str(item["message_id"]),
            "role": str(item["role"]),
            "sequence": int(item["sequence"]),
            "content_hash": str(item["content_hash"]),
        }
        for item in reversed(selected)
    ]
    payload = {
        "schema_version": 1,
        "normalizer_version": KNOWLEDGE_CANONICAL_VERSION,
        "tokenizer": "o200k_base",
        "thread_id": str(source["thread_id"]),
        "turn_id": str(source["turn_id"]),
        "primary_turn_occurred_at": _timestamp(source["user_occurred_at"]),
        "source_checkpoint_id": str(row["source_checkpoint_id"]),
        "primary_messages": primary_messages,
        "context_messages": context_manifest,
        "conversation_summary_sequence": summary_payload["sequence"] if summary_payload else None,
        "conversation_summary_hash": summary_hash,
    }
    payload["input_hash"] = sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    messages = {
        UUID(str(source["user_message_id_actual"])): {
            "message_id": source["user_message_id_actual"],
            "thread_id": source["thread_id"],
            "turn_id": source["turn_id"],
            "role": "user",
            "sequence": source["user_sequence"],
            "content": source["user_content"],
            "content_hash": source["user_content_hash"],
            "occurred_at": source["user_occurred_at"],
        },
        UUID(str(source["assistant_message_id_actual"])): {
            "message_id": source["assistant_message_id_actual"],
            "thread_id": source["thread_id"],
            "turn_id": source["turn_id"],
            "role": "assistant",
            "sequence": source["assistant_sequence"],
            "content": source["assistant_content"],
            "content_hash": source["assistant_content_hash"],
            "occurred_at": source["assistant_occurred_at"],
        },
    }
    messages.update({UUID(str(item["message_id"])): item for item in selected})
    return FrozenInput(payload, messages, summary_payload)


def _build_extract_request(frozen: FrozenInput) -> dict[str, Any]:
    return {
        "input_manifest": frozen.manifest,
        "primary_messages": [
            _model_message(frozen.messages[UUID(item["message_id"])])
            for item in frozen.manifest["primary_messages"]
        ],
        "context_messages": [
            _model_message(frozen.messages[UUID(item["message_id"])])
            for item in frozen.manifest["context_messages"]
        ],
        "conversation_summary": frozen.conversation_summary,
    }


def _build_merge_request(
    frozen: FrozenInput,
    candidates: list[KnowledgeCandidate],
    contexts: list[CandidateContext],
    row: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "input_manifest": frozen.manifest,
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        "existing_summary_candidates": [
            {
                "candidate_index": index,
                "summary_id": str(summary["summary_id"]),
                "version": int(summary["version"]),
                "state_hash": str(summary["state_hash"]),
                "topic_group_title": summary["topic_group_title"],
                "topic_title": summary["topic_title"],
                "content": summary["content"],
                "protected_sections": summary["protected_sections"],
                "exact_kind": int(summary["exact_kind"]),
                "final_score": float(summary["final_score"]),
                "deterministic_exact_target": str(contexts[index].exact_target_id)
                if contexts[index].exact_target_id
                else None,
                "ambiguous_exact": contexts[index].ambiguous_exact,
            }
            for index, context in enumerate(contexts)
            for summary in context.recalled
        ],
        "trigger": row["trigger"],
        "validation_feedback": [row["last_error_code"]] if row.get("last_error_code") else [],
    }


def _model_message(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "message_id": str(row["message_id"]),
        "role": row["role"],
        "sequence": int(row["sequence"]),
        "content": row["content"],
    }


def _filter_candidates(
    result: KnowledgeExtractionResult,
    messages: Mapping[UUID, Mapping[str, Any]],
    *,
    trigger: str,
    config: Any,
) -> tuple[list[KnowledgeCandidate], list[str]]:
    """执行数学范围、敏感信息、精确去重、quote 和置信度过滤。"""
    candidate_threshold = float(
        getattr(
            config,
            "conversation_knowledge_summary_auto_confidence"
            if trigger == "auto"
            else "conversation_knowledge_summary_manual_confidence",
            0.75 if trigger == "auto" else 0.60,
        )
    )
    kept: list[KnowledgeCandidate] = []
    warnings: list[str] = []
    for candidate in result.candidates:
        if candidate.scope == "non_math" or candidate.reusable_value == "ignore":
            continue
        if candidate.confidence < candidate_threshold:
            warnings.append("LOW_CANDIDATE_CONFIDENCE")
            continue
        if _contains_sensitive(candidate.topic_group_title) or _contains_sensitive(
            candidate.topic_title
        ):
            warnings.append("SENSITIVE_INFORMATION")
            continue
        overview = candidate.overview
        if overview is not None:
            _validate_supports(overview, messages)
            if _contains_sensitive(overview.text):
                warnings.append("SENSITIVE_INFORMATION")
                continue
            if overview.confidence < 0.65:
                overview = None
        items: list[CandidateItem] = []
        seen: set[str] = set()
        for item in candidate.items:
            _validate_supports(item, messages)
            if item.confidence < 0.65:
                continue
            if _contains_sensitive(item.text):
                warnings.append("SENSITIVE_INFORMATION")
                continue
            canonical = canonicalize_item_text_v1(item.text)
            if canonical in seen:
                warnings.append("DUPLICATE_CANDIDATE_ITEM")
                continue
            seen.add(canonical)
            items.append(item)
        if overview is None and not items:
            continue
        kept.append(candidate.model_copy(update={"overview": overview, "items": items}))
    return kept, sorted(set(warnings))


def _validate_extraction_sources(
    result: KnowledgeExtractionResult, messages: Mapping[UUID, Mapping[str, Any]]
) -> None:
    for candidate in result.candidates:
        if candidate.overview is not None:
            _validate_supports(candidate.overview, messages)
        for item in candidate.items:
            _validate_supports(item, messages)


def _validate_supports(item: CandidateItem, messages: Mapping[UUID, Mapping[str, Any]]) -> None:
    for support in item.supports:
        message = messages.get(support.message_id)
        if message is None:
            raise ValueError("KNOWLEDGE_SUMMARY_SOURCE_MESSAGE_NOT_IN_MANIFEST")
        canonical_message = canonicalize_quote_v1(str(message["content"]))
        canonical_quote = canonicalize_quote_v1(support.quote)
        if not canonical_quote or canonical_quote not in canonical_message:
            raise ValueError("KNOWLEDGE_SUMMARY_SOURCE_QUOTE_NOT_CONTIGUOUS")


def _scrub_extraction(
    result: KnowledgeExtractionResult, messages: Mapping[UUID, Mapping[str, Any]]
) -> dict[str, Any]:
    """将 quote 转为 canonical offset/hash，禁止原始 quote 进入审计 JSON。"""
    payload = result.model_dump(mode="json")
    safe_candidates: list[dict[str, Any]] = []
    for candidate in payload["candidates"]:
        if _contains_sensitive(candidate["topic_group_title"]) or _contains_sensitive(
            candidate["topic_title"]
        ):
            continue
        if candidate.get("overview") and _contains_sensitive(candidate["overview"]["text"]):
            continue
        candidate["items"] = [
            item for item in candidate.get("items", []) if not _contains_sensitive(item["text"])
        ]
        if candidate.get("overview") or candidate["items"]:
            safe_candidates.append(candidate)
    payload["candidates"] = safe_candidates
    for candidate in payload["candidates"]:
        for item in ([candidate["overview"]] if candidate.get("overview") else []) + candidate[
            "items"
        ]:
            for support in item["supports"]:
                message = messages[UUID(str(support["message_id"]))]
                canonical_message = canonicalize_quote_v1(str(message["content"]))
                canonical_quote = canonicalize_quote_v1(str(support.pop("quote")))
                start = canonical_message.find(canonical_quote)
                support.update(
                    canonical_start=start,
                    canonical_end=start + len(canonical_quote),
                    quote_hash=sha256(canonical_quote.encode("utf-8")).hexdigest(),
                )
    return payload


def _rehydrate_extraction(
    payload: Mapping[str, Any], messages: Mapping[UUID, Mapping[str, Any]]
) -> KnowledgeExtractionResult:
    """从脱敏 offset/hash 恢复内存中的 CandidateItem.quote 供后续校验使用。"""
    data = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    for candidate in data.get("candidates", []):
        for item in ([candidate["overview"]] if candidate.get("overview") else []) + candidate.get(
            "items", []
        ):
            for support in item.get("supports", []):
                message = messages.get(UUID(str(support["message_id"])))
                if message is None:
                    raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")
                canonical_message = canonicalize_quote_v1(str(message["content"]))
                start, end = int(support["canonical_start"]), int(support["canonical_end"])
                quote = canonical_message[start:end]
                if sha256(quote.encode("utf-8")).hexdigest() != support["quote_hash"]:
                    raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")
                support["quote"] = quote
                support.pop("canonical_start", None)
                support.pop("canonical_end", None)
                support.pop("quote_hash", None)
    return KnowledgeExtractionResult.model_validate(data)


def _validate_merge_targets(
    result: KnowledgeMergePlanResult,
    contexts: Sequence[CandidateContext],
    candidates: Sequence[KnowledgeCandidate],
) -> None:
    for plan in result.plans:
        context = contexts[plan.candidate_index]
        allowed = {UUID(str(row["summary_id"])) for row in context.recalled}
        if isinstance(plan, MergeSummaryPlan):
            if plan.target_summary_id not in allowed:
                raise ValueError("KNOWLEDGE_SUMMARY_PLAN_TARGET_OUT_OF_SCOPE")
            if context.ambiguous_exact:
                raise ValueError("KNOWLEDGE_SUMMARY_PLAN_AMBIGUOUS_EXACT")
            if (
                context.exact_target_id is not None
                and plan.target_summary_id != context.exact_target_id
            ):
                raise ValueError("KNOWLEDGE_SUMMARY_PLAN_EXACT_TARGET_MISMATCH")
            if plan.match_confidence < 0.90:
                raise ValueError("KNOWLEDGE_SUMMARY_PLAN_MATCH_CONFIDENCE_LOW")
        elif isinstance(plan, CreateSummaryPlan):
            if context.ambiguous_exact or context.exact_target_id is not None:
                raise ValueError("KNOWLEDGE_SUMMARY_PLAN_EXACT_TARGET_MISMATCH")
            if any(target not in allowed for target in plan.possible_duplicate_target_ids):
                raise ValueError("KNOWLEDGE_SUMMARY_PLAN_DUPLICATE_TARGET_OUT_OF_SCOPE")
            if plan.possible_duplicate_target_ids and not 0.60 <= plan.match_confidence < 0.90:
                raise ValueError("KNOWLEDGE_SUMMARY_PLAN_DUPLICATE_SCORE_INVALID")
            if not plan.possible_duplicate_target_ids and 0.60 <= plan.match_confidence < 0.90:
                raise ValueError("KNOWLEDGE_SUMMARY_PLAN_DUPLICATE_TARGET_MISSING")
            if plan.match_confidence >= 0.90:
                raise ValueError("KNOWLEDGE_SUMMARY_PLAN_MATCH_CONFIDENCE_LOW")
        elif isinstance(plan, NeedsReviewSummaryPlan):
            if any(target not in allowed for target in plan.target_summary_ids):
                raise ValueError("KNOWLEDGE_SUMMARY_PLAN_REVIEW_TARGET_OUT_OF_SCOPE")
            if context.ambiguous_exact and plan.reason_code != "AMBIGUOUS_EXACT_ALIAS":
                raise ValueError("KNOWLEDGE_SUMMARY_PLAN_AMBIGUOUS_EXACT")
        elif isinstance(plan, NoChangeSummaryPlan):
            if context.ambiguous_exact:
                raise ValueError("KNOWLEDGE_SUMMARY_PLAN_AMBIGUOUS_EXACT")
            if plan.target_summary_id is not None and plan.target_summary_id not in allowed:
                raise ValueError("KNOWLEDGE_SUMMARY_PLAN_TARGET_OUT_OF_SCOPE")
    if len(result.plans) != len(candidates):
        raise ValueError("KNOWLEDGE_SUMMARY_PLAN_COVERAGE_INVALID")


async def _apply_create(
    session: AsyncSession,
    *,
    row: Mapping[str, Any],
    candidate: KnowledgeCandidate,
    plan: CreateSummaryPlan,
    frozen: FrozenInput,
    duplicate_targets: dict[UUID, dict[str, Any]],
) -> list[UUID]:
    group = canonicalize_title_v1(candidate.topic_group_title, max_length=160)
    title = canonicalize_title_v1(candidate.topic_title, max_length=240)
    summary_id = uuid4()
    source_rows = _candidate_source_rows(candidate, frozen.messages)
    source_ids = {
        message_id: uuid4() for message_id in {item["message_id"] for item in source_rows}
    }
    content = _candidate_content(candidate, source_ids)
    content_model = KnowledgeSummaryContent.model_validate(content)
    content_hash = content_hash_v1(content_model)
    review_state = "possible_duplicate" if plan.possible_duplicate_target_ids else "clean"
    state_hash = state_hash_v1(
        topic_group_title=candidate.topic_group_title,
        topic_title=candidate.topic_title,
        content_hash=content_hash,
        protected_sections=[],
        review_state=review_state,
    )
    await summaries_repo.create_summary_snapshot(
        session,
        summary_id=summary_id,
        user_id=row["user_id"],
        topic_group_title=candidate.topic_group_title,
        topic_title=candidate.topic_title,
        normalized_topic_group=group,
        normalized_topic_title=title,
        content=content,
        search_text=build_search_text(
            topic_group_title=candidate.topic_group_title,
            topic_title=candidate.topic_title,
            content=content_model,
        ),
        protected_sections=[],
        content_hash=content_hash,
        state_hash=state_hash,
        review_state=review_state,
        generation_id=row["generation_id"],
        source_ids=list(source_ids.values()),
    )
    await summaries_repo.insert_source_rows_with_ids(
        session,
        summary_id=summary_id,
        user_id=row["user_id"],
        generation_id=row["generation_id"],
        trigger=str(row["trigger"]),
        source_checkpoint_id=str(row["source_checkpoint_id"]),
        source_rows=source_rows,
        source_ids_by_message=source_ids,
    )
    await summaries_repo.lock_and_recalculate_source_counts(session, summary_ids=[summary_id])
    for alias in [candidate.topic_title, *candidate.aliases]:
        normalized = canonicalize_title_v1(alias, max_length=240)
        await summaries_repo.upsert_generation_alias(
            session,
            alias_id=uuid4(),
            summary_id=summary_id,
            user_id=row["user_id"],
            normalized_topic_group=group,
            display_alias=alias,
            normalized_alias=normalized,
        )
    await summaries_repo.insert_generation_revision(
        session,
        revision_id=uuid4(),
        summary_id=summary_id,
        user_id=row["user_id"],
        version=1,
        base_version=0,
        mutation_type="create",
        topic_group_title=candidate.topic_group_title,
        topic_title=candidate.topic_title,
        content=content,
        protected_sections=[],
        content_hash=content_hash,
        changed_sections=["overview", *sorted({item.section for item in candidate.items})],
        source_ids=list(source_ids.values()),
        generation_id=row["generation_id"],
    )
    affected_summary_ids = [summary_id]
    for target_id in plan.possible_duplicate_target_ids:
        relation = await summaries_repo.insert_duplicate_candidate(
            session,
            duplicate_id=uuid4(),
            generation_id=row["generation_id"],
            summary_id=summary_id,
            possible_target_summary_id=target_id,
            user_id=row["user_id"],
            match_score=plan.match_confidence,
        )
        if str(relation["status"]) != "pending":
            continue
        relation_summary_id = UUID(str(relation["summary_id"]))
        relation_target_id = UUID(str(relation["possible_target_summary_id"]))
        counterpart_id = (
            relation_target_id if relation_summary_id == summary_id else relation_summary_id
        )
        target = duplicate_targets.get(counterpart_id)
        if target is None:
            raise GenerationCancelled("KNOWLEDGE_SUMMARY_SOURCE_CHANGED")
        await _record_duplicate_flag(session, row=row, summary=target)
        affected_summary_ids.append(counterpart_id)
    return affected_summary_ids


async def _apply_merge(
    session: AsyncSession,
    *,
    row: Mapping[str, Any],
    candidate: KnowledgeCandidate,
    plan: MergeSummaryPlan,
    target: Mapping[str, Any],
    frozen: FrozenInput,
    warning_codes: list[str],
) -> bool:
    current = KnowledgeSummaryContent.model_validate(target["content"])
    source_rows = _candidate_source_rows(candidate, frozen.messages)
    message_ids = [item["message_id"] for item in source_rows]
    existing_source_ids = await summaries_repo.get_source_ids_by_messages(
        session, summary_id=plan.target_summary_id, message_ids=message_ids
    )
    missing_ids = {
        message_id: uuid4() for message_id in set(message_ids) - set(existing_source_ids)
    }
    candidate_source_ids = {**existing_source_ids, **missing_ids}
    source_sort_keys = await summaries_repo.get_source_sort_keys(
        session,
        summary_id=plan.target_summary_id,
        source_ids=[*_content_source_ids(current), *candidate_source_ids.values()],
    )
    # 新来源尚未写入数据库，先用持久化消息时间构造排序键；只有最终 content
    # 实际引用的来源才允许在容量裁决后物化为 source row。
    for source_row in source_rows:
        source_id = candidate_source_ids[source_row["message_id"]]
        source_sort_keys.setdefault(
            source_id,
            (source_row["occurred_at"], int(source_row["sequence"]), source_id),
        )
    next_content = _apply_merge_mutations(
        current,
        candidate,
        plan,
        candidate_source_ids,
        target,
        source_sort_keys=source_sort_keys,
        warning_codes=warning_codes,
    )
    next_hash = content_hash_v1(next_content)
    referenced_source_ids = list(dict.fromkeys(_content_source_ids(next_content)))
    referenced_source_id_set = set(referenced_source_ids)
    referenced_missing_ids = {
        message_id: source_id
        for message_id, source_id in missing_ids.items()
        if source_id in referenced_source_id_set
    }
    if referenced_missing_ids:
        await summaries_repo.insert_source_rows_with_ids(
            session,
            summary_id=plan.target_summary_id,
            user_id=row["user_id"],
            generation_id=row["generation_id"],
            trigger=str(row["trigger"]),
            source_checkpoint_id=str(row["source_checkpoint_id"]),
            source_rows=[
                item for item in source_rows if item["message_id"] in referenced_missing_ids
            ],
            source_ids_by_message=referenced_missing_ids,
        )
    # 超限或 ignore 导致 content 未变化时，不写来源、版本、Revision 或来源计数。
    changed = next_hash != str(target["content_hash"])
    if not changed:
        return False
    next_version = int(target["version"]) + 1
    next_state_hash = state_hash_v1(
        topic_group_title=target["topic_group_title"],
        topic_title=target["topic_title"],
        content_hash=next_hash,
        protected_sections=target["protected_sections"],
        review_state=target["review_state"],
    )
    await summaries_repo.update_generation_summary_snapshot(
        session,
        summary_id=plan.target_summary_id,
        user_id=row["user_id"],
        content=next_content.model_dump(mode="json"),
        search_text=build_search_text(
            topic_group_title=target["topic_group_title"],
            topic_title=target["topic_title"],
            content=next_content,
        ),
        protected_sections=list(target["protected_sections"]),
        version=next_version,
        content_hash=next_hash,
        state_hash=next_state_hash,
        review_state=target["review_state"],
        generation_id=row["generation_id"],
    )
    await summaries_repo.insert_generation_revision(
        session,
        revision_id=uuid4(),
        summary_id=plan.target_summary_id,
        user_id=row["user_id"],
        version=next_version,
        base_version=int(target["version"]),
        mutation_type="auto_merge",
        topic_group_title=target["topic_group_title"],
        topic_title=target["topic_title"],
        content=next_content.model_dump(mode="json"),
        protected_sections=list(target["protected_sections"]),
        content_hash=next_hash,
        changed_sections=_changed_sections(current, next_content),
        source_ids=referenced_source_ids,
        generation_id=row["generation_id"],
    )
    await summaries_repo.lock_and_recalculate_source_counts(
        session, summary_ids=[plan.target_summary_id]
    )
    group = str(target["normalized_topic_group"])
    for alias in [candidate.topic_title, *candidate.aliases]:
        normalized = canonicalize_title_v1(alias, max_length=240)
        await summaries_repo.upsert_generation_alias(
            session,
            alias_id=uuid4(),
            summary_id=plan.target_summary_id,
            user_id=row["user_id"],
            normalized_topic_group=group,
            display_alias=alias,
            normalized_alias=normalized,
        )
    return True


def _mutation_action(mutation: Any) -> str:
    """将结构化 mutation 映射为受控 Prometheus action 标签。"""
    if isinstance(mutation, AppendItemMutation):
        return "append"
    if isinstance(mutation, MergeItemSourceMutation):
        return "merge_source"
    if isinstance(mutation, ReplaceItemMutation):
        return "replace"
    if isinstance(mutation, IgnoreItemMutation):
        return "ignore"
    if isinstance(mutation, SetOverviewMutation):
        return "set"
    if isinstance(mutation, MergeOverviewSourceMutation):
        return "merge_source"
    if isinstance(mutation, ReplaceOverviewMutation):
        return "replace"
    if isinstance(mutation, IgnoreOverviewMutation):
        return "ignore"
    return "unknown"


def _candidate_content(
    candidate: KnowledgeCandidate, source_ids: Mapping[UUID, UUID]
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 1,
        "overview": _new_item(candidate.overview, source_ids).model_dump(mode="json")
        if candidate.overview
        else None,
    }
    for section in (
        "definitions",
        "theorems",
        "formulas",
        "properties",
        "methods",
        "pitfalls",
    ):
        data[section] = [
            _new_item(item, source_ids).model_dump(mode="json")
            for item in candidate.items
            if item.section == section
        ]
    return KnowledgeSummaryContent.model_validate(data).model_dump(mode="json")


def _new_item(item: CandidateItem | None, source_ids: Mapping[UUID, UUID]) -> KnowledgeSummaryItem:
    assert item is not None
    return KnowledgeSummaryItem(
        item_id=uuid4(),
        text=item.text,
        origin="ai",
        source_ids=[source_ids[support.message_id] for support in item.supports],
    )


def _apply_merge_mutations(
    current: KnowledgeSummaryContent,
    candidate: KnowledgeCandidate,
    plan: MergeSummaryPlan,
    source_ids: Mapping[UUID, UUID],
    target: Mapping[str, Any],
    *,
    source_sort_keys: Mapping[UUID, tuple[datetime, int, UUID]],
    warning_codes: list[str],
) -> KnowledgeSummaryContent:
    """执行模型 merge plan，并以本地规则裁决保护、去重和内容/来源容量。"""
    data = current.model_dump(mode="python")
    protected = set(target["protected_sections"])
    if candidate.overview is not None and plan.overview_mutation is not None:
        overview_mutation = plan.overview_mutation
        overview_sources = [source_ids[s.message_id] for s in candidate.overview.supports]
        if isinstance(overview_mutation, SetOverviewMutation):
            if "overview" in protected:
                raise GenerationDeadLetter("KNOWLEDGE_SUMMARY_PROTECTED_SECTION_CONFLICT")
            if data.get("overview") is not None:
                raise GenerationDeadLetter("KNOWLEDGE_SUMMARY_PLAN_ITEM_OUT_OF_SCOPE")
            if _would_exceed_content_limit(data, candidate.overview.text):
                warning_codes.append("SECTION_LIMIT_REACHED")
            else:
                data["overview"] = _new_item(candidate.overview, source_ids).model_dump(
                    mode="python"
                )
        elif isinstance(overview_mutation, MergeOverviewSourceMutation):
            _ensure_existing_overview(data, overview_mutation.existing_overview_item_id)
            data["overview"]["source_ids"] = _union_source_ids(
                data["overview"]["source_ids"], overview_sources, source_sort_keys
            )
        elif isinstance(overview_mutation, ReplaceOverviewMutation):
            _ensure_existing_overview(data, overview_mutation.existing_overview_item_id)
            if "overview" in protected:
                raise GenerationDeadLetter("KNOWLEDGE_SUMMARY_PROTECTED_SECTION_CONFLICT")
            if data["overview"]["origin"] != "ai":
                raise GenerationDeadLetter("KNOWLEDGE_SUMMARY_UNSAFE_REPLACE")
            if _would_exceed_content_limit(
                data, candidate.overview.text, replacing_text=str(data["overview"]["text"])
            ):
                warning_codes.append("SECTION_LIMIT_REACHED")
            else:
                data["overview"]["text"] = candidate.overview.text
                data["overview"]["source_ids"] = _union_source_ids(
                    data["overview"]["source_ids"], overview_sources, source_sort_keys
                )
        elif isinstance(overview_mutation, IgnoreOverviewMutation):
            pass

    for item_mutation in plan.item_mutations:
        item = candidate.items[item_mutation.candidate_item_index]
        section = item.section
        items = data[section]
        item_sources = [source_ids[s.message_id] for s in item.supports]
        if isinstance(item_mutation, AppendItemMutation):
            duplicate = _find_canonical_item(items, item.text)
            if duplicate is not None:
                duplicate["source_ids"] = _union_source_ids(
                    duplicate["source_ids"], item_sources, source_sort_keys
                )
                continue
            if section in protected:
                raise GenerationDeadLetter("KNOWLEDGE_SUMMARY_PROTECTED_SECTION_CONFLICT")
            if (
                len(items) >= MAX_SECTION_ITEMS
                or _array_item_count(data) >= MAX_SUMMARY_ARRAY_ITEMS
                or _would_exceed_content_limit(data, item.text)
            ):
                warning_codes.append("SECTION_LIMIT_REACHED")
                continue
            items.append(_new_item(item, source_ids).model_dump(mode="python"))
        elif isinstance(item_mutation, (MergeItemSourceMutation, ReplaceItemMutation)):
            target_item = next(
                (
                    existing
                    for existing in items
                    if str(existing["item_id"]) == str(item_mutation.existing_item_id)
                ),
                None,
            )
            if target_item is None:
                raise GenerationDeadLetter("KNOWLEDGE_SUMMARY_PLAN_ITEM_OUT_OF_SCOPE")
            if isinstance(item_mutation, ReplaceItemMutation):
                if section in protected:
                    raise GenerationDeadLetter("KNOWLEDGE_SUMMARY_PROTECTED_SECTION_CONFLICT")
                if target_item["origin"] != "ai":
                    raise GenerationDeadLetter("KNOWLEDGE_SUMMARY_UNSAFE_REPLACE")
                duplicate = _find_canonical_item(
                    items, item.text, excluding_item_id=target_item["item_id"]
                )
                if duplicate is not None:
                    duplicate["source_ids"] = _union_source_ids(
                        duplicate["source_ids"], item_sources, source_sort_keys
                    )
                    continue
                if _would_exceed_content_limit(
                    data, item.text, replacing_text=str(target_item["text"])
                ):
                    warning_codes.append("SECTION_LIMIT_REACHED")
                    continue
                target_item["text"] = item.text
            target_item["source_ids"] = _union_source_ids(
                target_item["source_ids"], item_sources, source_sort_keys
            )
        elif isinstance(item_mutation, IgnoreItemMutation):
            pass
    return KnowledgeSummaryContent.model_validate(data)


def _array_item_count(data: Mapping[str, Any]) -> int:
    """统计六个数组章节的条目总数，不把独立 overview 计入 48 条上限。"""
    return sum(
        len(data.get(section, []))
        for section in ("definitions", "theorems", "formulas", "properties", "methods", "pitfalls")
    )


def _ensure_existing_overview(data: dict[str, Any], item_id: UUID) -> None:
    if data.get("overview") is None or str(data["overview"]["item_id"]) != str(item_id):
        raise GenerationDeadLetter("KNOWLEDGE_SUMMARY_PLAN_ITEM_OUT_OF_SCOPE")


def _find_canonical_item(
    items: Sequence[dict[str, Any]], value: str, *, excluding_item_id: UUID | str | None = None
) -> dict[str, Any] | None:
    """按 canonical 文本定位同一章节的既有条目，禁止模型制造重复内容。"""
    canonical = canonicalize_item_text_v1(value)
    for existing in items:
        if excluding_item_id is not None and str(existing["item_id"]) == str(excluding_item_id):
            continue
        if canonicalize_item_text_v1(str(existing["text"])) == canonical:
            return existing
    return None


def _content_canonical_length(data: Mapping[str, Any]) -> int:
    """计算 overview 加全部数组条目的规范化字符总量。"""
    texts: list[str] = []
    overview = data.get("overview")
    if overview is not None:
        texts.append(str(overview["text"]))
    for section in ("definitions", "theorems", "formulas", "properties", "methods", "pitfalls"):
        texts.extend(str(item["text"]) for item in data[section])
    return sum(len(canonicalize_item_text_v1(item_text)) for item_text in texts)


def _would_exceed_content_limit(
    data: Mapping[str, Any], candidate_text: str, *, replacing_text: str | None = None
) -> bool:
    """新增或替换内容时执行 24,000 个规范化字符的确定性上限裁决。"""
    after = _content_canonical_length(data) + len(canonicalize_item_text_v1(candidate_text))
    if replacing_text is not None:
        after -= len(canonicalize_item_text_v1(replacing_text))
    return after > MAX_SUMMARY_CANONICAL_CHARS


def _content_source_ids(content: KnowledgeSummaryContent) -> list[UUID]:
    """收集当前 content 的消息级 source ID，供来源时间截断查询。"""
    result: list[UUID] = []
    if content.overview is not None:
        result.extend(content.overview.source_ids)
    for section in ("definitions", "theorems", "formulas", "properties", "methods", "pitfalls"):
        result.extend(
            source_id for item in getattr(content, section) for source_id in item.source_ids
        )
    return result


def _union_source_ids(
    left: Sequence[str | UUID],
    right: Sequence[UUID],
    source_sort_keys: Mapping[UUID, tuple[datetime, int, UUID]],
) -> list[UUID]:
    """去重后选取最新 100 条来源，并按冻结的升序规则序列化。"""
    values = {UUID(str(value)) for value in left}
    values.update(right)
    fallback = (datetime.min.replace(tzinfo=UTC), -1, UUID(int=0))
    latest = sorted(
        values,
        key=lambda source_id: source_sort_keys.get(source_id, fallback),
        reverse=True,
    )[:MAX_ITEM_SOURCES]
    return sorted(latest, key=lambda source_id: source_sort_keys.get(source_id, fallback))


async def _record_duplicate_flag(
    session: AsyncSession, *, row: Mapping[str, Any], summary: dict[str, Any]
) -> None:
    """为既有重复关系对端同步状态快照、版本、哈希和 duplicate_flagged Revision。"""
    review_state = await summaries_repo.compute_effective_review_state(
        session, user_id=row["user_id"], summary_id=summary["summary_id"]
    )
    content = KnowledgeSummaryContent.model_validate(summary["content"])
    next_version = int(summary["version"]) + 1
    next_state_hash = state_hash_v1(
        topic_group_title=summary["topic_group_title"],
        topic_title=summary["topic_title"],
        content_hash=str(summary["content_hash"]),
        protected_sections=summary["protected_sections"],
        review_state=review_state,
    )
    await summaries_repo.update_generation_summary_snapshot(
        session,
        summary_id=summary["summary_id"],
        user_id=row["user_id"],
        content=content.model_dump(mode="json"),
        search_text=summary["search_text"],
        protected_sections=list(summary["protected_sections"]),
        version=next_version,
        content_hash=str(summary["content_hash"]),
        state_hash=next_state_hash,
        review_state=review_state,
        generation_id=row["generation_id"],
    )
    await summaries_repo.insert_generation_revision(
        session,
        revision_id=uuid4(),
        summary_id=summary["summary_id"],
        user_id=row["user_id"],
        version=next_version,
        base_version=int(summary["version"]),
        mutation_type="duplicate_flagged",
        topic_group_title=summary["topic_group_title"],
        topic_title=summary["topic_title"],
        content=content.model_dump(mode="json"),
        protected_sections=list(summary["protected_sections"]),
        content_hash=str(summary["content_hash"]),
        changed_sections=[],
        source_ids=[],
        generation_id=row["generation_id"],
    )
    summary.update(version=next_version, state_hash=next_state_hash, review_state=review_state)


def _candidate_source_rows(
    candidate: KnowledgeCandidate, messages: Mapping[UUID, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    ids: list[UUID] = []
    if candidate.overview is not None:
        ids.extend(support.message_id for support in candidate.overview.supports)
    for item in candidate.items:
        ids.extend(support.message_id for support in item.supports)
    unique = sorted(set(ids), key=lambda value: (int(messages[value]["sequence"]), str(value)))
    return [dict(messages[message_id]) for message_id in unique]


def _changed_sections(before: KnowledgeSummaryContent, after: KnowledgeSummaryContent) -> list[str]:
    changed: list[str] = []
    for section in (
        "overview",
        "definitions",
        "theorems",
        "formulas",
        "properties",
        "methods",
        "pitfalls",
    ):
        if getattr(before, section) != getattr(after, section):
            changed.append(section)
    return changed


async def _candidate_tombstone_suppressed(
    session: AsyncSession, *, row: Mapping[str, Any], candidate: KnowledgeCandidate
) -> str:
    """返回 none/suppressed/ambiguous，避免模糊墓碑匹配自动复活旧内容。"""
    group = canonicalize_title_v1(candidate.topic_group_title, max_length=160)
    title = canonicalize_title_v1(candidate.topic_title, max_length=240)
    aliases = [canonicalize_title_v1(alias, max_length=240) for alias in candidate.aliases]
    tombstone = await summaries_repo.find_tombstone_match(
        session,
        user_id=row["user_id"],
        normalized_topic_group=group,
        normalized_topic_title=title,
        normalized_aliases=aliases,
    )
    if tombstone is None or row["primary_turn_occurred_at"] > tombstone["deleted_at"]:
        return "none"
    if str(tombstone["match_kind"]) == "ambiguous":
        return "ambiguous"
    return "suppressed"


def _contains_sensitive(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SENSITIVE_PATTERNS) or bool(
        _SENSITIVE_LABELS.search(value) and any(character.isdigit() for character in value)
    )


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        current = value.astimezone(UTC)
    else:
        current = datetime.fromisoformat(str(value)).astimezone(UTC)
    return current.isoformat().replace("+00:00", "Z")


def _gateway_model_name(gateway: Any, config: Any) -> str:
    return str(
        getattr(gateway, "model_name", "") or getattr(config, "openai_knowledge_summary_model", "")
    )
