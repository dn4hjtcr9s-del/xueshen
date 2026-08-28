"""知识总结读写服务（知识总结方案 §12、§15）。

服务层负责把 HTTP 请求规范化为冻结契约、校验知识总结专属 cursor，并将
Repository 的结构化行映射为公开 DTO；它不访问模型，也不读取 Generation Job JSON。
"""

from __future__ import annotations

import base64
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.contracts.errors import (
    KnowledgeSummaryInvalidContentError,
    KnowledgeSummaryInvalidCursorError,
    KnowledgeSummaryMergeConflictError,
    KnowledgeSummaryNotFoundError,
    KnowledgeSummaryVersionConflictError,
)
from backend.conversation.contracts.knowledge_summary import (
    AllKnowledgeSection,
    KnowledgeSection,
    KnowledgeSummaryContent,
    KnowledgeSummaryDetailResponse,
    KnowledgeSummaryItem,
    KnowledgeSummaryItemEditInput,
    KnowledgeSummaryListItem,
    KnowledgeSummaryListResponse,
    KnowledgeSummaryPatchRequest,
    KnowledgeSummarySourcePage,
    KnowledgeSummarySourceView,
    KnowledgeSummaryStatsResponse,
    KnowledgeSummaryTopicGroup,
    KnowledgeSummaryTopicGroupResponse,
    OverviewEditInput,
    PendingReviewView,
    PossibleDuplicateView,
)
from backend.conversation.knowledge_summary.normalization import (
    KNOWLEDGE_SECTIONS,
    build_search_text,
    canonicalize_item_text_v1,
    canonicalize_title_v1,
    content_hash_v1,
    excerpt_text,
    state_hash_v1,
)
from backend.conversation.persistence import knowledge_summaries as summaries_repo
from backend.settings import Settings
from backend.shared.cursor import canonical_json, sign_cursor

_LIST_ROUTE = "knowledge-summaries:list"
_TOPIC_GROUPS_ROUTE = "knowledge-summaries:topic-groups"
_SOURCES_ROUTE = "knowledge-summaries:sources"
_CURSOR_SCHEMA_VERSION = 1
_CURSOR_TTL = timedelta(hours=24)

SummarySort = Literal["relevance_desc", "updated_desc", "title_asc"]


class KnowledgeSummaryService:
    """协调知识总结只读查询与 24 小时 HMAC cursor。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def list_summaries(
        self,
        *,
        user_id: UUID,
        query: str | None,
        topic_group: str | None,
        section_types: list[AllKnowledgeSection],
        review_state: Literal["clean", "possible_duplicate", "conflict"] | None,
        sort: SummarySort | None,
        cursor: str | None,
        limit: int,
    ) -> KnowledgeSummaryListResponse:
        """实现 §15.1 列表、搜索、筛选和三种稳定排序。"""
        query_raw, query_canonical = _normalize_query(query)
        normalized_topic_group = _normalize_optional_topic_group(topic_group)
        normalized_sections = sorted(set(section_types))
        resolved_sort = _resolve_list_sort(query_canonical, sort)
        filters = {
            "query_raw": query_raw,
            "query_canonical": query_canonical,
            "topic_group": normalized_topic_group,
            "section_type": normalized_sections,
            "review_state": review_state,
        }
        last_keys = self._resolve_cursor(
            cursor,
            route=_LIST_ROUTE,
            user_id=user_id,
            filters=filters,
            sort=resolved_sort,
        )
        async with self._session_factory() as session:
            rows = await summaries_repo.list_summaries(
                session,
                user_id=user_id,
                query_canonical=query_canonical,
                query_raw=query_raw,
                topic_group=normalized_topic_group,
                section_types=normalized_sections,
                review_state=review_state,
                sort=resolved_sort,
                last_keys=last_keys,
                limit=limit + 1,
            )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [self._list_item_from_row(row) for row in page_rows]
        next_cursor = None
        if has_more and page_rows:
            next_cursor = self._issue_cursor(
                route=_LIST_ROUTE,
                user_id=user_id,
                filters=filters,
                sort=resolved_sort,
                last_keys=_summary_last_keys(page_rows[-1], resolved_sort),
            )
        return KnowledgeSummaryListResponse(items=items, next_cursor=next_cursor, has_more=has_more)

    async def list_topic_groups(
        self,
        *,
        user_id: UUID,
        query: str | None,
        cursor: str | None,
        limit: int,
    ) -> KnowledgeSummaryTopicGroupResponse:
        """实现 §15.2 的大主题聚合和按更新时间稳定分页。"""
        query_raw, query_canonical = _normalize_query(query)
        filters = {"query_raw": query_raw, "query_canonical": query_canonical}
        last_keys = self._resolve_cursor(
            cursor,
            route=_TOPIC_GROUPS_ROUTE,
            user_id=user_id,
            filters=filters,
            sort="updated_desc",
        )
        async with self._session_factory() as session:
            rows = await summaries_repo.list_topic_groups(
                session,
                user_id=user_id,
                query_canonical=query_canonical,
                last_keys=last_keys,
                limit=limit + 1,
            )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [KnowledgeSummaryTopicGroup.model_validate(row) for row in page_rows]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = self._issue_cursor(
                route=_TOPIC_GROUPS_ROUTE,
                user_id=user_id,
                filters=filters,
                sort="updated_desc",
                last_keys={"updated_at": _timestamp(last["updated_at"]), "key": last["key"]},
            )
        return KnowledgeSummaryTopicGroupResponse(
            items=items, next_cursor=next_cursor, has_more=has_more
        )

    async def get_stats(self, *, user_id: UUID) -> KnowledgeSummaryStatsResponse:
        """实现 §15.3 统计，不依赖前端静态数据。"""
        async with self._session_factory() as session:
            row = await summaries_repo.get_stats(session, user_id=user_id)
        return KnowledgeSummaryStatsResponse.model_validate(row)

    async def get_summary_detail(
        self, *, user_id: UUID, summary_id: UUID
    ) -> KnowledgeSummaryDetailResponse:
        """实现 §15.4，返回 active 总结及其结构化 review/duplicate 视图。"""
        async with self._session_factory() as session:
            summary = await summaries_repo.get_active_summary(
                session, user_id=user_id, summary_id=summary_id
            )
            if summary is None:
                raise KnowledgeSummaryNotFoundError("知识总结不存在或无权访问")
            review_count = await summaries_repo.count_pending_reviews(
                session, user_id=user_id, summary_id=summary_id
            )
            review_rows = await summaries_repo.list_pending_reviews(
                session, user_id=user_id, summary_id=summary_id, limit=10
            )
            duplicate_rows = await summaries_repo.list_possible_duplicates(
                session, user_id=user_id, summary_id=summary_id, limit=5
            )
        content = KnowledgeSummaryContent.model_validate(summary["content"])
        reviews = [self._pending_review_from_row(row) for row in review_rows]
        duplicates = [
            PossibleDuplicateView.model_validate(_decimal_to_float(row)) for row in duplicate_rows
        ]
        return KnowledgeSummaryDetailResponse(
            summary_id=summary["summary_id"],
            topic_group_title=summary["topic_group_title"],
            topic_title=summary["topic_title"],
            status="active",
            review_state=summary["effective_review_state"],
            version=summary["version"],
            content_schema_version=summary["content_schema_version"],
            content=content,
            protected_sections=summary["protected_sections"],
            source_count=summary["source_count"],
            available_source_count=summary["available_source_count"],
            source_message_count=summary["source_message_count"],
            last_generated_at=summary["last_generated_at"],
            created_at=summary["created_at"],
            updated_at=summary["updated_at"],
            pending_review_count=review_count,
            pending_reviews=reviews,
            possible_duplicates=duplicates,
        )

    async def list_source_turns(
        self,
        *,
        user_id: UUID,
        summary_id: UUID,
        cursor: str | None,
        limit: int,
    ) -> KnowledgeSummarySourcePage:
        """实现 §15.5：先确认总结可访问，再按 Turn 聚合来源和 keyset 分页。"""
        async with self._session_factory() as session:
            summary = await summaries_repo.get_active_summary(
                session, user_id=user_id, summary_id=summary_id
            )
            if summary is None:
                raise KnowledgeSummaryNotFoundError("知识总结不存在或无权访问")
            filters: dict[str, object] = {}
            last_keys = self._resolve_cursor(
                cursor,
                route=_SOURCES_ROUTE,
                user_id=user_id,
                filters=filters,
                sort="occurred_at_desc",
                summary_id=summary_id,
                summary_version=int(summary["version"]),
            )
            rows = await summaries_repo.list_source_turns(
                session,
                user_id=user_id,
                summary_id=summary_id,
                last_keys=last_keys,
                limit=limit + 1,
            )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [
            KnowledgeSummarySourceView(
                source_turn_id=row["turn_id"],
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                support_message_ids=row["support_message_ids"],
                support_roles=row["support_roles"],
                question_excerpt=excerpt_text(row["question_content"], max_length=300),
                status=row["status"],
                occurred_at=row["occurred_at"],
            )
            for row in page_rows
        ]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = self._issue_cursor(
                route=_SOURCES_ROUTE,
                user_id=user_id,
                filters=filters,
                sort="occurred_at_desc",
                last_keys={
                    "occurred_at": _timestamp(last["occurred_at"]),
                    "turn_id": str(last["turn_id"]).lower(),
                },
                summary_id=summary_id,
                summary_version=int(summary["version"]),
            )
        return KnowledgeSummarySourcePage(items=items, next_cursor=next_cursor, has_more=has_more)

    async def patch_summary(
        self,
        *,
        user_id: UUID,
        summary_id: UUID,
        request: KnowledgeSummaryPatchRequest,
    ) -> KnowledgeSummaryDetailResponse:
        """按 §12.7 / §15.6 原子提交用户编辑、保护状态与 Revision。"""
        async with self._session_factory() as session:
            async with session.begin():
                summary = await summaries_repo.get_summary_for_mutation(
                    session, user_id=user_id, summary_id=summary_id
                )
                if summary is None or summary["status"] != "active":
                    raise KnowledgeSummaryNotFoundError("知识总结不存在或无权访问")
                current_version = int(summary["version"])
                if request.expected_version != current_version:
                    raise KnowledgeSummaryVersionConflictError(
                        "知识总结版本已变化，请刷新后重试",
                        current_version=current_version,
                    )

                topic_group_title = summary["topic_group_title"]
                topic_title = summary["topic_title"]
                if "topic_group_title" in request.model_fields_set:
                    topic_group_title = _trim_and_validate_title(
                        request.topic_group_title, max_length=160, field_name="大主题标题"
                    )
                if "topic_title" in request.model_fields_set:
                    topic_title = _trim_and_validate_title(
                        request.topic_title, max_length=240, field_name="知识点标题"
                    )
                normalized_topic_group = _normalize_title_for_patch(
                    topic_group_title, max_length=160, field_name="大主题标题"
                )
                normalized_topic_title = _normalize_title_for_patch(
                    topic_title, max_length=240, field_name="知识点标题"
                )
                if await summaries_repo.has_active_identity_conflict(
                    session,
                    user_id=user_id,
                    summary_id=summary_id,
                    normalized_topic_group=normalized_topic_group,
                    normalized_topic_title=normalized_topic_title,
                ):
                    raise KnowledgeSummaryMergeConflictError("编辑后的知识总结身份已存在")

                current_content = KnowledgeSummaryContent.model_validate(summary["content"])
                next_content, edited_sections = _apply_content_patch(current_content, request)
                next_protected_sections, protection_changes = _apply_protection_patch(
                    current_protected_sections=summary["protected_sections"],
                    edited_sections=edited_sections,
                    unlock_sections=request.unlock_sections,
                )
                next_content_hash = content_hash_v1(next_content)
                next_state_hash = state_hash_v1(
                    topic_group_title=topic_group_title,
                    topic_title=topic_title,
                    content_hash=next_content_hash,
                    protected_sections=next_protected_sections,
                    review_state=summary["review_state"],
                )
                if next_state_hash != summary["state_hash"]:
                    if (
                        topic_group_title != summary["topic_group_title"]
                        or topic_title != summary["topic_title"]
                    ):
                        await summaries_repo.upsert_summary_alias(
                            session,
                            alias_id=uuid4(),
                            user_id=user_id,
                            summary_id=summary_id,
                            normalized_topic_group=summary["normalized_topic_group"],
                            display_alias=summary["topic_title"],
                            normalized_alias=summary["normalized_topic_title"],
                        )
                        await summaries_repo.upsert_summary_alias(
                            session,
                            alias_id=uuid4(),
                            user_id=user_id,
                            summary_id=summary_id,
                            normalized_topic_group=normalized_topic_group,
                            display_alias=topic_title,
                            normalized_alias=normalized_topic_title,
                        )

                    next_version = current_version + 1
                    content_payload = next_content.model_dump(mode="json")
                    changed_sections = sorted(edited_sections | protection_changes)
                    await summaries_repo.update_summary_snapshot(
                        session,
                        summary_id=summary_id,
                        user_id=user_id,
                        topic_group_title=topic_group_title,
                        topic_title=topic_title,
                        normalized_topic_group=normalized_topic_group,
                        normalized_topic_title=normalized_topic_title,
                        content=content_payload,
                        search_text=build_search_text(
                            topic_group_title=topic_group_title,
                            topic_title=topic_title,
                            content=next_content,
                        ),
                        protected_sections=next_protected_sections,
                        version=next_version,
                        content_hash=next_content_hash,
                        state_hash=next_state_hash,
                    )
                    await summaries_repo.insert_revision(
                        session,
                        revision_id=uuid4(),
                        summary_id=summary_id,
                        user_id=user_id,
                        version=next_version,
                        base_version=current_version,
                        mutation_type="user_edit",
                        actor_type="user",
                        topic_group_title=topic_group_title,
                        topic_title=topic_title,
                        content=content_payload,
                        protected_sections=next_protected_sections,
                        content_hash=next_content_hash,
                        changed_sections=changed_sections,
                    )
        return await self.get_summary_detail(user_id=user_id, summary_id=summary_id)

    async def delete_summary(
        self,
        *,
        user_id: UUID,
        summary_id: UUID,
        expected_version: int,
    ) -> None:
        """按 §12.10 / §15.7 写 tombstone、关闭待处理关系并幂等软删除总结。"""
        async with self._session_factory() as session:
            async with session.begin():
                summary = await summaries_repo.get_summary_for_mutation(
                    session, user_id=user_id, summary_id=summary_id
                )
                if summary is None:
                    tombstone = await summaries_repo.get_tombstone_for_deleted_summary(
                        session, user_id=user_id, summary_id=summary_id
                    )
                    if tombstone is not None:
                        return
                    raise KnowledgeSummaryNotFoundError("知识总结不存在或无权访问")
                if summary["status"] == "deleted":
                    return

                current_version = int(summary["version"])
                if expected_version != current_version:
                    raise KnowledgeSummaryVersionConflictError(
                        "知识总结版本已变化，请刷新后重试",
                        current_version=current_version,
                    )

                source_turns = await summaries_repo.list_tombstone_turn_rows(
                    session, user_id=user_id, summary_id=summary_id
                )
                alias_rows = await summaries_repo.list_summary_aliases(
                    session, user_id=user_id, summary_id=summary_id
                )
                # 当前身份另存于 tombstone 的规范化标题列，不占历史 alias 的 20 条配额。
                normalized_aliases = sorted(str(row["normalized_alias"]) for row in alias_rows[:20])
                latest_source_occurred_at = max(
                    (row["source_occurred_at"] for row in source_turns),
                    default=None,
                )
                tombstone_id = uuid4()
                await summaries_repo.insert_tombstone(
                    session,
                    tombstone_id=tombstone_id,
                    user_id=user_id,
                    deleted_summary_id=summary_id,
                    normalized_topic_group=summary["normalized_topic_group"],
                    normalized_topic_title=summary["normalized_topic_title"],
                    normalized_aliases=normalized_aliases,
                    latest_source_occurred_at=latest_source_occurred_at,
                )
                await summaries_repo.insert_tombstone_turns(
                    session,
                    tombstone_id=tombstone_id,
                    user_id=user_id,
                    turn_rows=source_turns,
                )
                await summaries_repo.resolve_reviews_for_deleted_summary(
                    session, user_id=user_id, summary_id=summary_id
                )
                duplicate_counterpart_ids = (
                    await summaries_repo.list_pending_duplicate_counterpart_ids(
                        session, user_id=user_id, summary_id=summary_id
                    )
                )
                duplicate_counterparts = await summaries_repo.lock_summary_rows(
                    session, user_id=user_id, summary_ids=duplicate_counterpart_ids
                )
                await summaries_repo.resolve_duplicates_for_deleted_summary(
                    session, user_id=user_id, summary_id=summary_id
                )
                for counterpart in duplicate_counterparts:
                    await _record_duplicate_resolution_revision(
                        session, user_id=user_id, summary=counterpart
                    )

                next_version = current_version + 1
                content = KnowledgeSummaryContent.model_validate(summary["content"])
                await summaries_repo.insert_revision(
                    session,
                    revision_id=uuid4(),
                    summary_id=summary_id,
                    user_id=user_id,
                    version=next_version,
                    base_version=current_version,
                    mutation_type="delete",
                    actor_type="user",
                    topic_group_title=summary["topic_group_title"],
                    topic_title=summary["topic_title"],
                    content=content.model_dump(mode="json"),
                    protected_sections=sorted(summary["protected_sections"]),
                    content_hash=summary["content_hash"],
                    changed_sections=[],
                )
                await summaries_repo.mark_summary_deleted(
                    session,
                    user_id=user_id,
                    summary_id=summary_id,
                    version=next_version,
                )

    def _list_item_from_row(self, row: dict[str, Any]) -> KnowledgeSummaryListItem:
        """用 v1 content 计算页面摘要和七类章节数量。"""
        content = KnowledgeSummaryContent.model_validate(row["content"])
        section_counts: dict[AllKnowledgeSection, int] = {
            "overview": 1 if content.overview is not None else 0,
            "definitions": len(content.definitions),
            "theorems": len(content.theorems),
            "formulas": len(content.formulas),
            "properties": len(content.properties),
            "methods": len(content.methods),
            "pitfalls": len(content.pitfalls),
        }
        return KnowledgeSummaryListItem(
            summary_id=row["summary_id"],
            topic_group_title=row["topic_group_title"],
            topic_title=row["topic_title"],
            overview_excerpt=excerpt_text(
                content.overview.text if content.overview is not None else None,
                max_length=280,
            ),
            section_counts=section_counts,
            source_count=row["source_count"],
            available_source_count=row["available_source_count"],
            source_message_count=row["source_message_count"],
            review_state=row["effective_review_state"],
            version=row["version"],
            updated_at=row["updated_at"],
        )

    def _pending_review_from_row(self, row: dict[str, Any]) -> PendingReviewView:
        """验证确认过的 proposed_content 形状，防止内部自由 JSON 泄漏到 API。"""
        proposal = row["proposed_content"]
        if not isinstance(proposal, dict):
            raise KnowledgeSummaryInvalidContentError("待确认建议内容格式非法")
        title = proposal.get("proposed_topic_title")
        sections = proposal.get("proposed_sections")
        if not isinstance(title, str) or not isinstance(sections, dict):
            raise KnowledgeSummaryInvalidContentError("待确认建议内容格式非法")
        allowed_sections = set(KNOWLEDGE_SECTIONS)
        if set(sections) - allowed_sections:
            raise KnowledgeSummaryInvalidContentError("待确认建议章节非法")
        typed_sections = cast(dict[KnowledgeSection, list[str]], sections)
        return PendingReviewView(
            review_id=row["review_id"],
            generation_id=row["generation_id"],
            reason_code=row["reason_code"],
            proposed_topic_title=title,
            proposed_sections=typed_sections,
            source_turn_id=row["source_turn_id"],
            created_at=row["created_at"],
        )

    def _issue_cursor(
        self,
        *,
        route: str,
        user_id: UUID,
        filters: Mapping[str, object],
        sort: str,
        last_keys: dict[str, object],
        summary_id: UUID | None = None,
        summary_version: int | None = None,
    ) -> str:
        """以 §15.1 / §15.5 的完整绑定字段签发 24 小时不透明 cursor。"""
        now = datetime.now(UTC)
        payload: dict[str, object] = {
            "schema_version": _CURSOR_SCHEMA_VERSION,
            "route": route,
            "user_key": _user_key(self._settings.cursor_hmac_key, user_id),
            "filters_hash": _filters_hash(filters),
            "sort": sort,
            "last_keys": last_keys,
            "issued_at": _timestamp(now),
            "expires_at": _timestamp(now + _CURSOR_TTL),
        }
        if summary_id is not None and summary_version is not None:
            payload["summary_id"] = str(summary_id).lower()
            payload["summary_version"] = summary_version
        return sign_cursor(self._settings.cursor_hmac_key, payload)

    def _resolve_cursor(
        self,
        token: str | None,
        *,
        route: str,
        user_id: UUID,
        filters: Mapping[str, object],
        sort: str,
        summary_id: UUID | None = None,
        summary_version: int | None = None,
    ) -> dict[str, Any] | None:
        """验证 cursor 的签名、过期、用户、筛选、排序和来源 summary 版本绑定。"""
        if token is None:
            return None
        payload = _decode_signed_cursor(self._settings.cursor_hmac_key, token)
        expected: dict[str, object] = {
            "schema_version": _CURSOR_SCHEMA_VERSION,
            "route": route,
            "user_key": _user_key(self._settings.cursor_hmac_key, user_id),
            "filters_hash": _filters_hash(filters),
            "sort": sort,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise KnowledgeSummaryInvalidCursorError("知识总结 cursor 绑定不匹配")
        if summary_id is not None and summary_version is not None:
            if (
                payload.get("summary_id") != str(summary_id).lower()
                or payload.get("summary_version") != summary_version
            ):
                raise KnowledgeSummaryInvalidCursorError("知识总结来源 cursor 已失效")
        expires_at = _parse_timestamp(payload.get("expires_at"))
        issued_at = _parse_timestamp(payload.get("issued_at"))
        if expires_at is None or issued_at is None or datetime.now(UTC) > expires_at:
            raise KnowledgeSummaryInvalidCursorError("知识总结 cursor 无效或已过期")
        last_keys = payload.get("last_keys")
        if not isinstance(last_keys, dict):
            raise KnowledgeSummaryInvalidCursorError("知识总结 cursor 缺少分页位置")
        return _parse_last_keys(route, sort, last_keys)


async def _record_duplicate_resolution_revision(
    session: AsyncSession, *, user_id: UUID, summary: dict[str, Any]
) -> None:
    """删除一端后同步存活对端的有效状态、版本、哈希和 duplicate_resolved Revision。"""
    review_state = await summaries_repo.compute_effective_review_state(
        session, user_id=user_id, summary_id=summary["summary_id"]
    )
    content = KnowledgeSummaryContent.model_validate(summary["content"])
    next_version = int(summary["version"]) + 1
    next_state_hash = state_hash_v1(
        topic_group_title=summary["topic_group_title"],
        topic_title=summary["topic_title"],
        content_hash=str(summary["content_hash"]),
        protected_sections=list(summary["protected_sections"]),
        review_state=review_state,
    )
    await summaries_repo.update_summary_snapshot(
        session,
        summary_id=summary["summary_id"],
        user_id=user_id,
        topic_group_title=summary["topic_group_title"],
        topic_title=summary["topic_title"],
        normalized_topic_group=summary["normalized_topic_group"],
        normalized_topic_title=summary["normalized_topic_title"],
        content=content.model_dump(mode="json"),
        search_text=summary["search_text"],
        protected_sections=list(summary["protected_sections"]),
        version=next_version,
        content_hash=str(summary["content_hash"]),
        state_hash=next_state_hash,
        review_state=review_state,
    )
    await summaries_repo.insert_revision(
        session,
        revision_id=uuid4(),
        summary_id=summary["summary_id"],
        user_id=user_id,
        version=next_version,
        base_version=int(summary["version"]),
        mutation_type="duplicate_resolved",
        actor_type="system",
        topic_group_title=summary["topic_group_title"],
        topic_title=summary["topic_title"],
        content=content.model_dump(mode="json"),
        protected_sections=list(summary["protected_sections"]),
        content_hash=str(summary["content_hash"]),
        changed_sections=[],
    )


def _trim_and_validate_title(value: str | None, *, max_length: int, field_name: str) -> str:
    """PATCH 显式提交标题时统一 trim，并将规范化失败映射为公开内容错误。"""
    if value is None:
        raise KnowledgeSummaryInvalidContentError(f"{field_name}不能为 null")
    trimmed = value.strip()
    if not trimmed or len(trimmed) > max_length:
        raise KnowledgeSummaryInvalidContentError(f"{field_name}长度非法")
    return trimmed


def _normalize_title_for_patch(value: str, *, max_length: int, field_name: str) -> str:
    """把版本化标题规范化的异常收敛为 PATCH 的统一错误码。"""
    try:
        return canonicalize_title_v1(value, max_length=max_length)
    except ValueError as exc:
        raise KnowledgeSummaryInvalidContentError(f"{field_name}规范化失败") from exc


def _apply_content_patch(
    current_content: KnowledgeSummaryContent,
    request: KnowledgeSummaryPatchRequest,
) -> tuple[KnowledgeSummaryContent, set[str]]:
    """按 item ID 规则应用 overview 与章节完整替换，不触碰未出现的章节。"""
    content = current_content.model_copy(deep=True)
    edited_sections: set[str] = set()
    _validate_request_item_ids(request)

    if "overview" in request.model_fields_set:
        edited_sections.add("overview")
        overview_input = request.overview
        if overview_input is None:
            content.overview = None
        else:
            content.overview = _edit_item(
                existing_item=content.overview,
                request_item=overview_input,
                section="overview",
            )

    if "sections" in request.model_fields_set:
        for section, input_items in request.sections.items():
            edited_sections.add(section)
            existing_items = list(getattr(content, section))
            setattr(
                content,
                section,
                _replace_section_items(
                    existing_items=existing_items,
                    request_items=input_items,
                    section=section,
                ),
            )
    try:
        return KnowledgeSummaryContent.model_validate(
            content.model_dump(mode="python")
        ), edited_sections
    except ValueError as exc:
        raise KnowledgeSummaryInvalidContentError("知识总结编辑内容不合法") from exc


def _validate_request_item_ids(request: KnowledgeSummaryPatchRequest) -> None:
    """拒绝跨 overview/章节重复引用同一 item ID，避免完整替换语义歧义。"""
    item_ids: list[UUID] = []
    if "overview" in request.model_fields_set and request.overview is not None:
        if request.overview.item_id is not None:
            item_ids.append(request.overview.item_id)
    if "sections" in request.model_fields_set:
        for items in request.sections.values():
            item_ids.extend(item.item_id for item in items if item.item_id is not None)
    if len(item_ids) != len(set(item_ids)):
        raise KnowledgeSummaryInvalidContentError("同一请求中的 item_id 不得重复")


def _replace_section_items(
    *,
    existing_items: list[KnowledgeSummaryItem],
    request_items: list[KnowledgeSummaryItemEditInput],
    section: KnowledgeSection,
) -> list[KnowledgeSummaryItem]:
    """应用数组章节的完整替换，并验证 ID 归属和 canonical 文本唯一性。"""
    existing_by_id = {item.item_id: item for item in existing_items}
    result: list[KnowledgeSummaryItem] = []
    canonical_texts: set[str] = set()
    for request_item in request_items:
        item = _edit_item(
            existing_item=existing_by_id.get(request_item.item_id)
            if request_item.item_id is not None
            else None,
            request_item=request_item,
            section=section,
        )
        canonical = _canonical_item_text_for_patch(item.text, section=section)
        if canonical in canonical_texts:
            raise KnowledgeSummaryInvalidContentError(f"{section} 存在重复条目")
        canonical_texts.add(canonical)
        result.append(item)
    return result


def _edit_item(
    *,
    existing_item: KnowledgeSummaryItem | None,
    request_item: KnowledgeSummaryItemEditInput | OverviewEditInput,
    section: AllKnowledgeSection,
) -> KnowledgeSummaryItem:
    """保留未变 canonical 文本的 AI 来源；用户改写或新建条目均清空来源。"""
    canonical_new = _canonical_item_text_for_patch(request_item.text, section=section)
    if request_item.item_id is not None and existing_item is None:
        raise KnowledgeSummaryInvalidContentError(f"{section} 包含未知或跨章节 item_id")
    if existing_item is not None:
        canonical_existing = _canonical_item_text_for_patch(existing_item.text, section=section)
        if canonical_existing == canonical_new:
            return existing_item.model_copy(update={"text": request_item.text})
        return KnowledgeSummaryItem(
            item_id=existing_item.item_id,
            text=request_item.text,
            origin="user",
            source_ids=[],
        )
    return KnowledgeSummaryItem(
        item_id=uuid4(),
        text=request_item.text,
        origin="user",
        source_ids=[],
    )


def _canonical_item_text_for_patch(value: str, *, section: AllKnowledgeSection) -> str:
    """将 canonical 空值等内容问题转换为冻结的 INVALID_CONTENT 错误。"""
    try:
        return canonicalize_item_text_v1(value)
    except ValueError as exc:
        raise KnowledgeSummaryInvalidContentError(f"{section} 条目内容不能为空") from exc


def _apply_protection_patch(
    *,
    current_protected_sections: list[str],
    edited_sections: set[str],
    unlock_sections: list[AllKnowledgeSection],
) -> tuple[list[str], set[str]]:
    """用户编辑章节自动加保护；显式解锁只作用于未同时编辑的章节。"""
    protected = set(current_protected_sections)
    changes: set[str] = set()
    for section in edited_sections:
        if section not in protected:
            protected.add(section)
            changes.add(f"protection:{section}")
    for section in unlock_sections:
        if section in protected:
            protected.remove(section)
            changes.add(f"protection:{section}")
    return sorted(protected), changes


def _normalize_query(query: str | None) -> tuple[str | None, str | None]:
    """规范化可选 query；显式空白 query 按 §15.1 拒绝。"""
    if query is None:
        return None, None
    query_raw = " ".join(query.split())
    if not query_raw or len(query_raw) > 200:
        raise KnowledgeSummaryInvalidContentError("搜索词必须为 1–200 字符")
    try:
        return query_raw, canonicalize_title_v1(query_raw, max_length=240)
    except ValueError as exc:
        raise KnowledgeSummaryInvalidContentError("搜索词规范化失败") from exc


def _normalize_optional_topic_group(topic_group: str | None) -> str | None:
    """空字符串视为未提供；其他输入必须是可用的规范化大主题 key。"""
    if topic_group is None or not topic_group.strip():
        return None
    try:
        return canonicalize_title_v1(topic_group, max_length=160)
    except ValueError as exc:
        raise KnowledgeSummaryInvalidContentError("大主题筛选条件非法") from exc


def _resolve_list_sort(query_canonical: str | None, sort: SummarySort | None) -> SummarySort:
    """落实 §15.1 的默认排序及无 query relevance 拒绝规则。"""
    resolved: SummarySort = sort or (
        "relevance_desc" if query_canonical is not None else "updated_desc"
    )
    if resolved == "relevance_desc" and query_canonical is None:
        raise KnowledgeSummaryInvalidContentError("无搜索词时不能按相关性排序")
    return resolved


def _summary_last_keys(row: dict[str, Any], sort: SummarySort) -> dict[str, object]:
    """将本页末行转换为对应排序唯一需要的 cursor last_keys。"""
    if sort == "relevance_desc":
        return {
            "exact_rank": int(row["exact_rank"]),
            "substring_hit": int(row["substring_hit"]),
            "query_trigram_score": f"{row['query_trigram_score']:.5f}",
            "updated_at": _timestamp(row["updated_at"]),
            "summary_id": str(row["summary_id"]).lower(),
        }
    if sort == "updated_desc":
        return {
            "updated_at": _timestamp(row["updated_at"]),
            "summary_id": str(row["summary_id"]).lower(),
        }
    return {
        "normalized_topic_group": row["normalized_topic_group"],
        "normalized_topic_title": row["normalized_topic_title"],
        "summary_id": str(row["summary_id"]).lower(),
    }


def _decode_signed_cursor(secret: str, token: str) -> dict[str, Any]:
    """解析 sign_cursor 生成的 base64url.body 签名，不复用通用游标的不同 payload。"""
    try:
        body_b64, signature = token.rsplit(".", 1)
        expected = hmac.new(
            secret.encode("utf-8"), f"cursor:v1:{body_b64}".encode(), sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("签名不匹配")
        padding = "=" * (-len(body_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body_b64 + padding).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload 非对象")
        return cast(dict[str, Any], payload)
    except Exception as exc:
        raise KnowledgeSummaryInvalidCursorError("知识总结 cursor 无效") from exc


def _parse_last_keys(route: str, sort: str, last_keys: dict[str, Any]) -> dict[str, Any]:
    """只接受各路由/排序冻结的 key 集合与值类型。"""
    expected_keys: set[str]
    if route == _SOURCES_ROUTE:
        expected_keys = {"occurred_at", "turn_id"}
    elif sort == "relevance_desc":
        expected_keys = {
            "exact_rank",
            "substring_hit",
            "query_trigram_score",
            "updated_at",
            "summary_id",
        }
    elif sort == "updated_desc" and route == _TOPIC_GROUPS_ROUTE:
        expected_keys = {"updated_at", "key"}
    elif sort == "updated_desc":
        expected_keys = {"updated_at", "summary_id"}
    else:
        expected_keys = {"normalized_topic_group", "normalized_topic_title", "summary_id"}
    if set(last_keys) != expected_keys:
        raise KnowledgeSummaryInvalidCursorError("知识总结 cursor 分页键非法")
    try:
        if route == _SOURCES_ROUTE:
            return {
                "occurred_at": _require_timestamp(last_keys["occurred_at"]),
                "turn_id": UUID(str(last_keys["turn_id"])),
            }
        if sort == "relevance_desc":
            return {
                "exact_rank": int(last_keys["exact_rank"]),
                "substring_hit": int(last_keys["substring_hit"]),
                "query_trigram_score": str(last_keys["query_trigram_score"]),
                "updated_at": _require_timestamp(last_keys["updated_at"]),
                "summary_id": UUID(str(last_keys["summary_id"])),
            }
        if sort == "updated_desc" and route == _TOPIC_GROUPS_ROUTE:
            return {
                "updated_at": _require_timestamp(last_keys["updated_at"]),
                "key": str(last_keys["key"]),
            }
        if sort == "updated_desc":
            return {
                "updated_at": _require_timestamp(last_keys["updated_at"]),
                "summary_id": UUID(str(last_keys["summary_id"])),
            }
        return {
            "normalized_topic_group": str(last_keys["normalized_topic_group"]),
            "normalized_topic_title": str(last_keys["normalized_topic_title"]),
            "summary_id": UUID(str(last_keys["summary_id"])),
        }
    except (TypeError, ValueError) as exc:
        raise KnowledgeSummaryInvalidCursorError("知识总结 cursor 分页键非法") from exc


def _decimal_to_float(row: dict[str, Any]) -> dict[str, Any]:
    """psycopg numeric 映射为公开 DTO 所需的 JSON 兼容浮点数。"""
    converted = dict(row)
    converted["match_score"] = float(converted["match_score"])
    return converted


def _filters_hash(filters: Mapping[str, object]) -> str:
    """用 canonical JSON 固定 filters hash；limit 故意不参与。"""
    return sha256(canonical_json(filters).encode("utf-8")).hexdigest()


def _user_key(secret: str, user_id: UUID) -> str:
    """按知识总结 cursor 契约计算用户绑定 HMAC。"""
    return hmac.new(secret.encode("utf-8"), str(user_id).encode("utf-8"), sha256).hexdigest()


def _timestamp(value: datetime) -> str:
    """输出 cursor 冻结要求的 RFC3339 UTC 时间。"""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    """解析 cursor 的 RFC3339 UTC 时间，拒绝缺失、无时区和非 UTC 值。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _require_timestamp(value: object) -> datetime:
    """为 last_keys 解析必填 RFC3339 UTC 时间。"""
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise ValueError("时间非法")
    return parsed
