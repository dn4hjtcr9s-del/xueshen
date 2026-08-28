"""知识总结 Generation 的 API 层服务（知识总结方案 §15.8–§15.11）。

处理手动生成/重试/重新整理、当前 Turn Generation 查询、单 Job 状态读取和
review dismiss。所有写操作都带用户隔离、幂等校验、限流和 tombstone 抑制。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.contracts.errors import (
    ConversationNotFoundError,
    KnowledgeSummaryGenerationNotFoundError,
    KnowledgeSummaryRateLimitedError,
    KnowledgeSummaryRequestIdempotencyConflictError,
    KnowledgeSummaryReviewNotFoundError,
    KnowledgeSummarySourceChangedError,
    KnowledgeSummarySourceSuppressedError,
    TurnNotFoundError,
)
from backend.conversation.contracts.knowledge_summary import (
    AffectedKnowledgeSummary,
    CreateKnowledgeSummaryGenerationRequest,
    CurrentTurnKnowledgeSummaryGenerationResponse,
    KnowledgeSummaryGenerationResponse,
    KnowledgeSummaryGenerationStatusResponse,
    ReviewReasonCode,
)
from backend.conversation.persistence import (
    knowledge_summaries as summaries_repo,
)
from backend.conversation.persistence import (
    knowledge_summary_generations as generations_repo,
)
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo
from backend.settings import Settings
from backend.shared.ratelimit import FixedWindowRateLimiter, retry_after_seconds

_CLIENT_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def _status_retryable(status: str) -> bool:
    return status in ("pending", "processing", "retry_wait")


class KnowledgeSummaryGenerationApiService:
    """知识总结 Generation 相关 API 的服务层。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        rate_limiter: FixedWindowRateLimiter,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._rate_limiter = rate_limiter

    # ------------------------------------------------------------------
    # 手动创建 Generation Job（§15.8）
    # ------------------------------------------------------------------

    async def ensure_manual_generation(
        self,
        *,
        user_id: UUID,
        thread_id: UUID,
        turn_id: UUID,
        request: CreateKnowledgeSummaryGenerationRequest,
        client_ip_address: str | None,
    ) -> KnowledgeSummaryGenerationResponse:
        """手动触发/重试/刷新知识总结生成。"""
        if not _CLIENT_REQUEST_ID_RE.match(request.client_request_id):
            raise KnowledgeSummaryRequestIdempotencyConflictError("client_request_id 格式非法")

        async with self._session_factory() as session:
            async with session.begin():
                # 1. Thread/Turn 归属与完成状态。
                thread = await threads_repo.get_thread(session, thread_id)
                if (
                    thread is None
                    or thread["user_id"] != user_id
                    or str(thread.get("status")) not in {"active", "archived"}
                ):
                    raise ConversationNotFoundError("会话不存在或不可用")
                turn = await turns_repo.get_turn(session, turn_id, for_update=True)
                if turn is None or turn["thread_id"] != thread_id or turn["user_id"] != user_id:
                    raise TurnNotFoundError("Turn 不存在或无权访问")
                if str(turn["status"]) != "completed":
                    raise KnowledgeSummarySourceChangedError("Turn 尚未完成")
                source_checkpoint_id = str(turn.get("source_checkpoint_id") or "")
                if not source_checkpoint_id:
                    raise KnowledgeSummarySourceChangedError("来源 checkpoint 缺失")

                # 2. Tombstone 抑制（同步拒绝旧 Turn）。
                tombstone_turns = await summaries_repo.list_tombstone_turns(
                    session, user_id=user_id, turn_id=turn_id
                )
                if tombstone_turns:
                    raise KnowledgeSummarySourceSuppressedError("该对话来源已被用户删除抑制")

                # 3. 同 client_request_id 幂等查找。
                existing_by_client = await self._get_job_by_client_request(
                    session, user_id=user_id, client_request_id=request.client_request_id
                )
                if existing_by_client is not None:
                    if not self._same_manual_request(
                        existing_by_client,
                        user_id=user_id,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        source_checkpoint_id=source_checkpoint_id,
                        force=request.force,
                    ):
                        raise KnowledgeSummaryRequestIdempotencyConflictError(
                            "client_request_id 已被不同参数复用"
                        )
                    return self._generation_response(existing_by_client)

                # 4. 同 checkpoint 可复用 Job 查找。
                latest_job = await self._get_latest_job_for_turn(
                    session,
                    user_id=user_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    source_checkpoint_id=source_checkpoint_id,
                    include_cancelled=True,
                )
                latest_status: str | None = None
                if latest_job is not None:
                    latest_status = str(latest_job["status"])
                    # 取消原因是 Thread 删除或 source changed 时拒绝重试。
                    if latest_status == "cancelled":
                        reason = str(latest_job.get("last_error_code") or "")
                        if reason in (
                            "THREAD_DELETED",
                            "KNOWLEDGE_SUMMARY_THREAD_DELETED",
                            "KNOWLEDGE_SUMMARY_SOURCE_CHANGED",
                        ):
                            raise KnowledgeSummarySourceChangedError("该来源已不可用，无法重试")
                    # 仅 force=false 时允许复用现有 Job；force=true 必须新建 manual_refresh。
                    if not request.force and latest_status in (
                        "pending",
                        "processing",
                        "retry_wait",
                        "succeeded",
                        "no_change",
                        "needs_review",
                    ):
                        return self._generation_response(latest_job)
                    # dead_letter / cancelled（非上述原因）在 force=false 时创建 manual_retry。

                # 5. 限流：user 桶始终检查；IP 桶在解析出 IP 时检查。
                user_bucket = "knowledge_summary_manual:user"
                user_limit = (
                    self._settings.conversation_knowledge_summary_manual_rate_limit_per_minute
                )
                if self._rate_limiter.is_limited(
                    user_bucket, str(user_id), user_limit, window_seconds=60
                ):
                    raise KnowledgeSummaryRateLimitedError(
                        "手动生成过于频繁，请稍后重试",
                        retry_after=retry_after_seconds(60),
                    )
                if client_ip_address is not None:
                    ip_bucket = "knowledge_summary_manual:ip"
                    ip_limit = (
                        self._settings.conversation_knowledge_summary_ip_rate_limit_per_minute
                    )
                    if self._rate_limiter.is_limited(
                        ip_bucket, client_ip_address, ip_limit, window_seconds=60
                    ):
                        raise KnowledgeSummaryRateLimitedError(
                            "当前网络手动生成过于频繁，请稍后重试",
                            retry_after=retry_after_seconds(60),
                        )

                # 6. 创建新 Job。
                trigger: str
                if request.force:
                    trigger = "manual_refresh"
                elif latest_job is not None and latest_status in (
                    "dead_letter",
                    "cancelled",
                ):
                    trigger = "manual_retry"
                else:
                    trigger = "manual"

                generation_id = uuid4()
                idempotency_key = (
                    f"knowledge-summary:{trigger}:{user_id}:{request.client_request_id}"
                )
                # 读取主来源 user message 的 occurred_at。
                result = await session.execute(
                    text(
                        "SELECT occurred_at FROM conversation.conversation_messages "
                        "WHERE thread_id = :thread_id AND turn_id = :turn_id AND role = 'user' "
                        "  AND status = 'completed' "
                        "ORDER BY sequence LIMIT 1"
                    ),
                    {"thread_id": thread_id, "turn_id": turn_id},
                )
                msg_row = result.mappings().first()
                primary_occurred_at = (
                    msg_row["occurred_at"] if msg_row is not None else datetime.now(UTC)
                )
                inserted = await generations_repo.insert_generation_job(
                    session,
                    generation_id=generation_id,
                    idempotency_key=idempotency_key,
                    client_request_id=request.client_request_id,
                    user_id=user_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    source_checkpoint_id=source_checkpoint_id,
                    trigger=trigger,
                    primary_turn_occurred_at=primary_occurred_at,
                )
                if not inserted:
                    # 并发冲突：重新读取并返回。
                    existing = await self._get_job_by_client_request(
                        session,
                        user_id=user_id,
                        client_request_id=request.client_request_id,
                    )
                    if existing is None:
                        raise KnowledgeSummaryRequestIdempotencyConflictError(
                            "Job 幂等键冲突但无法定位已有记录"
                        )
                    return self._generation_response(existing)

                # 限流计数只计实际创建。
                self._rate_limiter.hit(user_bucket, str(user_id), user_limit, window_seconds=60)
                if client_ip_address is not None:
                    self._rate_limiter.hit(
                        ip_bucket, client_ip_address, ip_limit, window_seconds=60
                    )

                job = await generations_repo.get_generation(session, generation_id)
                assert job is not None
                return self._generation_response(job)

    # ------------------------------------------------------------------
    # 当前 Turn Generation 查询（§15.9）
    # ------------------------------------------------------------------

    async def get_current_generation_for_turn(
        self,
        *,
        user_id: UUID,
        thread_id: UUID,
        turn_id: UUID,
    ) -> CurrentTurnKnowledgeSummaryGenerationResponse:
        """按 §15.9 选择当前 Turn 应展示的最新非 cancelled Generation。"""
        async with self._session_factory() as session:
            async with session.begin():
                thread = await threads_repo.get_thread(session, thread_id)
                if thread is None or thread["user_id"] != user_id:
                    raise ConversationNotFoundError("会话不存在或无权访问")
                turn = await turns_repo.get_turn(session, turn_id)
                if turn is None or turn["thread_id"] != thread_id or turn["user_id"] != user_id:
                    raise TurnNotFoundError("Turn 不存在或无权访问")
                job = await self._get_latest_job_for_turn(
                    session, user_id=user_id, thread_id=thread_id, turn_id=turn_id
                )
                if job is None:
                    return CurrentTurnKnowledgeSummaryGenerationResponse(generation=None)
                return CurrentTurnKnowledgeSummaryGenerationResponse(
                    generation=await self._status_response_with_session(session, job)
                )

    # ------------------------------------------------------------------
    # 单 Job 状态（§15.10）
    # ------------------------------------------------------------------

    async def get_generation_status(
        self,
        *,
        user_id: UUID,
        generation_id: UUID,
    ) -> KnowledgeSummaryGenerationStatusResponse:
        """读取单个 Generation Job 的当前状态。"""
        async with self._session_factory() as session:
            async with session.begin():
                job = await generations_repo.get_generation(session, generation_id)
                if job is None or job["user_id"] != user_id:
                    raise KnowledgeSummaryGenerationNotFoundError("Generation 不存在或无权访问")
                return await self._status_response_with_session(session, job)

    # ------------------------------------------------------------------
    # Dismiss Review（§15.11）
    # ------------------------------------------------------------------

    async def dismiss_review(
        self,
        *,
        user_id: UUID,
        generation_id: UUID,
        review_id: UUID,
    ) -> None:
        """用户忽略一条待确认建议，并重新计算 summary review_state。"""
        async with self._session_factory() as session:
            async with session.begin():
                # 校验 Generation 归属。
                job = await generations_repo.get_generation(session, generation_id)
                if job is None or job["user_id"] != user_id:
                    raise KnowledgeSummaryGenerationNotFoundError("Generation 不存在或无权访问")

                # 读取并锁定 review。
                review_result = await session.execute(
                    text(
                        "SELECT * FROM conversation.knowledge_summary_reviews "
                        "WHERE review_id = :review_id AND user_id = :user_id"
                    ),
                    {"review_id": review_id, "user_id": user_id},
                )
                review = review_result.mappings().first()
                if review is None or review["generation_id"] != generation_id:
                    raise KnowledgeSummaryReviewNotFoundError("Review 不存在或无权访问")
                if str(review["status"]) != "pending":
                    return

                summary_id = UUID(str(review["summary_id"]))
                summary = await summaries_repo.get_summary_for_mutation(
                    session, user_id=user_id, summary_id=summary_id
                )
                if summary is None or summary["status"] != "active":
                    raise KnowledgeSummaryReviewNotFoundError("关联总结不存在")

                current_version = int(summary["version"])
                # 标记 review dismissed。
                await session.execute(
                    text(
                        "UPDATE conversation.knowledge_summary_reviews "
                        "SET status = 'dismissed', resolved_at = now() "
                        "WHERE review_id = :review_id AND status = 'pending'"
                    ),
                    {"review_id": review_id},
                )

                # 计算新的 review_state。
                new_review_state = await self._compute_review_state(
                    session, user_id=user_id, summary_id=summary_id
                )
                if new_review_state == str(summary["review_state"]):
                    # 无状态变化：无需 version/revision。
                    return

                next_version = current_version + 1
                from backend.conversation.knowledge_summary.normalization import state_hash_v1

                next_state_hash = state_hash_v1(
                    topic_group_title=summary["topic_group_title"],
                    topic_title=summary["topic_title"],
                    content_hash=str(summary["content_hash"]),
                    protected_sections=list(summary["protected_sections"]),
                    review_state=new_review_state,
                )
                await summaries_repo.update_summary_snapshot(
                    session,
                    summary_id=summary_id,
                    user_id=user_id,
                    topic_group_title=summary["topic_group_title"],
                    topic_title=summary["topic_title"],
                    normalized_topic_group=summary["normalized_topic_group"],
                    normalized_topic_title=summary["normalized_topic_title"],
                    content=summary["content"],
                    search_text=summary["search_text"],
                    protected_sections=list(summary["protected_sections"]),
                    version=next_version,
                    content_hash=str(summary["content_hash"]),
                    state_hash=next_state_hash,
                    review_state=new_review_state,
                )
                await summaries_repo.insert_revision(
                    session,
                    revision_id=uuid4(),
                    summary_id=summary_id,
                    user_id=user_id,
                    version=next_version,
                    base_version=current_version,
                    mutation_type="conflict_resolved",
                    actor_type="user",
                    topic_group_title=summary["topic_group_title"],
                    topic_title=summary["topic_title"],
                    content=summary["content"],
                    protected_sections=list(summary["protected_sections"]),
                    content_hash=str(summary["content_hash"]),
                    changed_sections=[],
                )

    # ------------------------------------------------------------------
    # 内部 helpers
    # ------------------------------------------------------------------

    async def _get_job_by_client_request(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        client_request_id: str,
    ) -> dict[str, Any] | None:
        result = await session.execute(
            text(
                "SELECT * FROM conversation.knowledge_summary_generation_jobs "
                "WHERE user_id = :user_id AND client_request_id = :client_request_id"
            ),
            {"user_id": user_id, "client_request_id": client_request_id},
        )
        row = result.mappings().first()
        return dict(row) if row is not None else None

    async def _get_latest_job_for_turn(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        thread_id: UUID,
        turn_id: UUID,
        source_checkpoint_id: str | None = None,
        include_cancelled: bool = False,
    ) -> dict[str, Any] | None:
        """按 §15.9 选择展示 Job；手动重试时可额外读取 cancelled 记录。"""
        cancelled_filter = "" if include_cancelled else "AND status <> 'cancelled'"
        checkpoint_filter = (
            ""
            if source_checkpoint_id is None
            else "AND source_checkpoint_id = :source_checkpoint_id"
        )
        result = await session.execute(
            text(
                f"""
                SELECT *
                FROM conversation.knowledge_summary_generation_jobs
                WHERE user_id = :user_id AND thread_id = :thread_id AND turn_id = :turn_id
                  {checkpoint_filter}
                  {cancelled_filter}
                ORDER BY created_at DESC,
                         CASE trigger
                             WHEN 'manual_refresh' THEN 0
                             WHEN 'manual_retry' THEN 1
                             WHEN 'manual' THEN 2
                             WHEN 'ops_retry' THEN 3
                             ELSE 4
                         END,
                         generation_id DESC
                LIMIT 1
                """
            ),
            {
                "user_id": user_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "source_checkpoint_id": source_checkpoint_id,
            },
        )
        row = result.mappings().first()
        return dict(row) if row is not None else None

    def _same_manual_request(
        self,
        job: dict[str, Any],
        *,
        user_id: UUID,
        thread_id: UUID,
        turn_id: UUID,
        source_checkpoint_id: str,
        force: bool,
    ) -> bool:
        """严格比较幂等请求参数，避免 force 改变时复用错误触发器。"""
        allowed_triggers = {"manual_refresh"} if force else {"manual", "manual_retry"}
        return (
            job["user_id"] == user_id
            and job["thread_id"] == thread_id
            and job["turn_id"] == turn_id
            and str(job["source_checkpoint_id"]) == source_checkpoint_id
            and str(job["trigger"]) in allowed_triggers
        )

    def _generation_response(self, job: dict[str, Any]) -> KnowledgeSummaryGenerationResponse:
        return KnowledgeSummaryGenerationResponse(
            generation_id=job["generation_id"],
            trigger=job["trigger"],
            status=job["status"],
            status_path=f"/api/v1/knowledge-summary-generations/{job['generation_id']}",
        )

    async def _status_response_with_session(
        self,
        session: AsyncSession,
        job: dict[str, Any],
    ) -> KnowledgeSummaryGenerationStatusResponse:
        """从数据库查询 affected summary 标题，组装完整状态响应。"""
        affected_ids = list(job.get("affected_summary_ids") or [])
        affected: list[AffectedKnowledgeSummary] = []
        if affected_ids:
            result = await session.execute(
                text(
                    "SELECT summary_id, topic_group_title, topic_title "
                    "FROM conversation.knowledge_summaries "
                    "WHERE summary_id = ANY(:summary_ids) AND user_id = :user_id"
                ),
                {"summary_ids": affected_ids, "user_id": job["user_id"]},
            )
            title_by_id = {
                row["summary_id"]: (row["topic_group_title"], row["topic_title"])
                for row in result.mappings()
            }
            for summary_id in affected_ids:
                title = title_by_id.get(summary_id, ("", ""))
                affected.append(
                    AffectedKnowledgeSummary(
                        summary_id=summary_id,
                        topic_group_title=title[0],
                        topic_title=title[1],
                    )
                )

        review_codes: list[ReviewReasonCode] = []
        if str(job["status"]) == "needs_review":
            review_result = await session.execute(
                text(
                    "SELECT DISTINCT reason_code FROM conversation.knowledge_summary_reviews "
                    "WHERE generation_id = :generation_id AND status = 'pending'"
                ),
                {"generation_id": job["generation_id"]},
            )
            review_codes = cast(
                list[ReviewReasonCode],
                [str(row["reason_code"]) for row in review_result.mappings()],
            )

        return KnowledgeSummaryGenerationStatusResponse(
            generation_id=job["generation_id"],
            thread_id=job["thread_id"],
            turn_id=job["turn_id"],
            trigger=job["trigger"],
            status=job["status"],
            affected_summaries=affected,
            warning_codes=list(job.get("warning_codes") or []),
            review_reason_codes=review_codes,
            retryable=_status_retryable(job["status"]),
            created_at=job["created_at"],
            updated_at=job["updated_at"],
            completed_at=job.get("completed_at"),
        )

    def _status_response(self, job: dict[str, Any]) -> KnowledgeSummaryGenerationStatusResponse:
        """无 session 时的简化版本（标题留空）。"""
        affected: list[AffectedKnowledgeSummary] = []
        for summary_id in job.get("affected_summary_ids") or []:
            affected.append(
                AffectedKnowledgeSummary(
                    summary_id=summary_id,
                    topic_group_title="",
                    topic_title="",
                )
            )
        review_codes: list[ReviewReasonCode] = []
        if str(job["status"]) == "needs_review":
            review_codes = cast(list[ReviewReasonCode], list(job.get("review_reason_codes") or []))
        return KnowledgeSummaryGenerationStatusResponse(
            generation_id=job["generation_id"],
            thread_id=job["thread_id"],
            turn_id=job["turn_id"],
            trigger=job["trigger"],
            status=job["status"],
            affected_summaries=affected,
            warning_codes=list(job.get("warning_codes") or []),
            review_reason_codes=review_codes,
            retryable=_status_retryable(job["status"]),
            created_at=job["created_at"],
            updated_at=job["updated_at"],
            completed_at=job.get("completed_at"),
        )

    async def _compute_review_state(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        summary_id: UUID,
    ) -> str:
        """从结构化表计算 summary 的 effective review_state。"""
        result = await session.execute(
            text(
                """
                SELECT CASE
                    WHEN EXISTS (
                        SELECT 1 FROM conversation.knowledge_summary_reviews r
                        WHERE r.summary_id = :summary_id
                          AND r.user_id = :user_id
                          AND r.status = 'pending'
                    ) THEN 'conflict'
                    WHEN EXISTS (
                        SELECT 1
                        FROM conversation.knowledge_summary_duplicate_candidates d
                        WHERE d.user_id = :user_id
                          AND d.status = 'pending'
                          AND (d.summary_id = :summary_id
                               OR d.possible_target_summary_id = :summary_id)
                    ) THEN 'possible_duplicate'
                    ELSE 'clean'
                END AS state
                """
            ),
            {"summary_id": summary_id, "user_id": user_id},
        )
        return str(result.mappings().one()["state"])
