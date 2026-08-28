"""知识总结 Phase 1 Repository 与只读 API 集成测试（方案 §15）。

覆盖用户隔离、列表搜索和 cursor、主题/统计、结构化 review/duplicate、以及按 Turn
聚合且先聚合后分页的来源卡。所有数据仅写入 conversation_test。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from backend.conversation.contracts.knowledge_summary import (
    KnowledgeSummaryContent,
    KnowledgeSummaryItem,
)
from backend.conversation.knowledge_summary.normalization import (
    build_search_text,
    canonicalize_title_v1,
    content_hash_v1,
    state_hash_v1,
)
from backend.conversation.persistence import messages as messages_repo
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo

pytestmark = pytest.mark.asyncio


async def _make_app(
    conversation_session_factory, *, generation_enabled: bool = False
) -> AsyncClient:
    """构造显式开启只读 flag 的 API 测试客户端，不复用全局 Settings。"""
    from backend.app import create_app
    from backend.memory.api.dependencies import ApiRuntime
    from backend.settings import Settings

    settings = Settings(
        app_env="development",
        _env_file=None,
        conversation_knowledge_summary_enabled=True,
        conversation_knowledge_summary_generation_enabled=generation_enabled,
        openai_knowledge_summary_model=("contract-test-model" if generation_enabled else ""),
        conversation_knowledge_summary_structured_output_models=(
            "contract-test-model" if generation_enabled else ""
        ),
        conversation_knowledge_summary_manual_rate_limit_per_minute=(
            2 if generation_enabled else 6
        ),
    )
    runtime = ApiRuntime(
        settings=settings,
        session_factory=conversation_session_factory,
        memory_service=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        gateway_worker=None,  # type: ignore[arg-type]
    )
    app = create_app(settings=settings, runtime=runtime)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture()
async def client(conversation_session_factory) -> AsyncClient:
    """为每个测试装配带 Phase 1 路由的独立 ASGI 客户端。"""
    return await _make_app(conversation_session_factory)


@pytest.fixture()
async def generation_client(conversation_session_factory) -> AsyncClient:
    """为每个测试装配带 Phase 4 生成路由的独立 ASGI 客户端。"""
    return await _make_app(conversation_session_factory, generation_enabled=True)


def _auth(user_id: UUID) -> dict[str, str]:
    """开发认证头；用户 ID 必须来自服务端 AuthContext。"""
    return {"X-Dev-User-Id": str(user_id)}


async def _insert_turn_with_messages(
    session,
    *,
    user_id: UUID,
    occurred_at: datetime,
    question: str,
    answer: str,
    sequence_base: int,
) -> tuple[UUID, UUID, UUID, UUID]:
    """写入完成 Turn、用户消息和助手消息，供 source 外键与问题摘要使用。"""
    thread_id = uuid4()
    turn_id = uuid4()
    user_message_id = uuid4()
    assistant_message_id = uuid4()
    await threads_repo.insert_thread(session, thread_id, user_id)
    await turns_repo.insert_turn(
        session,
        turn_id=turn_id,
        thread_id=thread_id,
        user_id=user_id,
        client_request_id=f"source-{turn_id}",
        request_id=f"request-{turn_id}",
        run_id=f"run-{turn_id}",
        user_message_id=user_message_id,
        expected_thread_version=0,
        graph_thread_id=str(thread_id),
        next_attempt_at=occurred_at,
    )
    await session.execute(
        text(
            "UPDATE conversation.conversation_turns SET status = 'completed' "
            "WHERE turn_id = :turn_id"
        ),
        {"turn_id": turn_id},
    )
    await messages_repo.insert_message(
        session,
        message_id=user_message_id,
        thread_id=thread_id,
        turn_id=turn_id,
        user_id=user_id,
        sequence=sequence_base,
        role="user",
        content=question,
        content_hash=sha256(question.encode()).hexdigest(),
        occurred_at=occurred_at,
        completed_at=occurred_at,
    )
    await messages_repo.insert_message(
        session,
        message_id=assistant_message_id,
        thread_id=thread_id,
        turn_id=turn_id,
        user_id=user_id,
        sequence=sequence_base + 1,
        role="assistant",
        content=answer,
        content_hash=sha256(answer.encode()).hexdigest(),
        occurred_at=occurred_at + timedelta(seconds=1),
        completed_at=occurred_at + timedelta(seconds=1),
    )
    return thread_id, turn_id, user_message_id, assistant_message_id


async def _insert_summary(
    session,
    *,
    summary_id: UUID,
    user_id: UUID,
    topic_group_title: str,
    topic_title: str,
    source_ids: list[UUID],
    updated_at: datetime,
) -> None:
    """用内容 v1 和冻结哈希写入 active summary fixture。"""
    item = KnowledgeSummaryItem(
        item_id=uuid4(),
        text=f"{topic_title} 的可复用数学结论。",
        origin="ai",
        source_ids=source_ids or [uuid4()],
    )
    content = KnowledgeSummaryContent(
        overview=item,
        formulas=[item.model_copy(update={"item_id": uuid4()})],
    )
    normalized_group = canonicalize_title_v1(topic_group_title, max_length=160)
    normalized_title = canonicalize_title_v1(topic_title, max_length=240)
    digest = content_hash_v1(content)
    state_digest = state_hash_v1(
        topic_group_title=topic_group_title,
        topic_title=topic_title,
        content_hash=digest,
        protected_sections=[],
        review_state="clean",
    )
    await session.execute(
        text(
            """
            INSERT INTO conversation.knowledge_summaries (
                summary_id, user_id, topic_group_title, topic_title,
                normalized_topic_group, normalized_topic_title, status, review_state,
                content_schema_version, content, search_text, protected_sections, version,
                source_count, available_source_count, source_message_count,
                content_hash, state_hash, created_at, updated_at
            ) VALUES (
                :summary_id, :user_id, :topic_group_title, :topic_title,
                :normalized_topic_group, :normalized_topic_title, 'active', 'clean',
                1, CAST(:content AS jsonb), :search_text, '{}', 1,
                :source_count, :available_source_count, :source_message_count,
                :content_hash, :state_hash, :updated_at, :updated_at
            )
            """
        ),
        {
            "summary_id": summary_id,
            "user_id": user_id,
            "topic_group_title": topic_group_title,
            "topic_title": topic_title,
            "normalized_topic_group": normalized_group,
            "normalized_topic_title": normalized_title,
            "content": json.dumps(content.model_dump(mode="json"), ensure_ascii=False),
            "search_text": build_search_text(
                topic_group_title=topic_group_title,
                topic_title=topic_title,
                content=content,
            ),
            "source_count": len(source_ids),
            "available_source_count": len(source_ids),
            "source_message_count": len(source_ids),
            "content_hash": digest,
            "state_hash": state_digest,
            "updated_at": updated_at,
        },
    )


async def _insert_source(
    session,
    *,
    summary_id: UUID,
    user_id: UUID,
    thread_id: UUID,
    turn_id: UUID,
    message_id: UUID,
    role: str,
    occurred_at: datetime,
    sequence: int,
    status: str,
    source_id: UUID,
) -> None:
    """写入一条消息级 source，来源 API 将其在 Turn 层聚合。"""
    await session.execute(
        text(
            """
            INSERT INTO conversation.knowledge_summary_sources (
                source_id, summary_id, user_id, thread_id, turn_id, message_id,
                message_role, source_checkpoint_id, first_generation_id, first_trigger,
                status, message_occurred_at, message_sequence, created_at, unavailable_at
            ) VALUES (
                :source_id, :summary_id, :user_id, :thread_id, :turn_id, :message_id,
                :message_role, 'checkpoint', NULL, 'manual', :status,
                :occurred_at, :message_sequence, :occurred_at,
                CASE WHEN :status = 'unavailable' THEN :occurred_at ELSE NULL END
            )
            """
        ),
        {
            "source_id": source_id,
            "summary_id": summary_id,
            "user_id": user_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "message_id": message_id,
            "message_role": role,
            "status": status,
            "occurred_at": occurred_at,
            "message_sequence": sequence,
        },
    )


async def _insert_generation(session, *, user_id: UUID, thread_id: UUID, turn_id: UUID) -> UUID:
    """写入 review/duplicate 外键所需的终态 Generation fixture。"""
    generation_id = uuid4()
    now = datetime.now(UTC)
    await session.execute(
        text(
            """
            INSERT INTO conversation.knowledge_summary_generation_jobs (
                generation_id, idempotency_key, user_id, thread_id, turn_id,
                source_checkpoint_id, trigger, status, primary_turn_occurred_at,
                created_at, updated_at, completed_at
            ) VALUES (
                :generation_id, :idempotency_key, :user_id, :thread_id, :turn_id,
                'checkpoint', 'manual', 'needs_review', :now, :now, :now, :now
            )
            """
        ),
        {
            "generation_id": generation_id,
            "idempotency_key": f"fixture:{generation_id}",
            "user_id": user_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "now": now,
        },
    )
    return generation_id


async def _seed_summary_fixture(conversation_session_factory) -> dict[str, UUID]:
    """建立两张同用户总结、一个跨用户总结和三张可分页来源卡。"""
    user_id = uuid4()
    other_user_id = uuid4()
    summary_id = uuid4()
    counterpart_id = uuid4()
    other_summary_id = uuid4()
    now = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
    async with conversation_session_factory() as session:
        async with session.begin():
            first = await _insert_turn_with_messages(
                session,
                user_id=user_id,
                occurred_at=now - timedelta(hours=2),
                question="什么是椭圆离心率？",
                answer="椭圆离心率是 c/a。",
                sequence_base=1,
            )
            second = await _insert_turn_with_messages(
                session,
                user_id=user_id,
                occurred_at=now - timedelta(hours=1),
                question="离心率有哪些范围？",
                answer="椭圆满足 0<e<1。",
                sequence_base=1,
            )
            third = await _insert_turn_with_messages(
                session,
                user_id=user_id,
                occurred_at=now,
                question="如何判断椭圆的离心率？",
                answer="先确认 a、c 的定义，再计算 c/a。",
                sequence_base=1,
            )
            source_ids = [uuid4(), uuid4(), uuid4()]
            await _insert_summary(
                session,
                summary_id=summary_id,
                user_id=user_id,
                topic_group_title="圆锥曲线",
                topic_title="椭圆的离心率",
                source_ids=source_ids,
                updated_at=now,
            )
            await _insert_source(
                session,
                summary_id=summary_id,
                user_id=user_id,
                thread_id=first[0],
                turn_id=first[1],
                message_id=first[3],
                role="assistant",
                occurred_at=now - timedelta(hours=2) + timedelta(seconds=1),
                sequence=2,
                status="available",
                source_id=source_ids[0],
            )
            await _insert_source(
                session,
                summary_id=summary_id,
                user_id=user_id,
                thread_id=second[0],
                turn_id=second[1],
                message_id=second[3],
                role="assistant",
                occurred_at=now - timedelta(hours=1) + timedelta(seconds=1),
                sequence=2,
                status="unavailable",
                source_id=source_ids[1],
            )
            await _insert_source(
                session,
                summary_id=summary_id,
                user_id=user_id,
                thread_id=third[0],
                turn_id=third[1],
                message_id=third[3],
                role="assistant",
                occurred_at=now + timedelta(seconds=1),
                sequence=2,
                status="available",
                source_id=source_ids[2],
            )
            await _insert_summary(
                session,
                summary_id=counterpart_id,
                user_id=user_id,
                topic_group_title="圆锥曲线",
                topic_title="椭圆离心率的计算",
                source_ids=[source_ids[0]],
                updated_at=now - timedelta(minutes=1),
            )
            await _insert_summary(
                session,
                summary_id=other_summary_id,
                user_id=other_user_id,
                topic_group_title="圆锥曲线",
                topic_title="椭圆的离心率",
                source_ids=[uuid4()],
                updated_at=now,
            )
            generation_id = await _insert_generation(
                session,
                user_id=user_id,
                thread_id=first[0],
                turn_id=first[1],
            )
            await session.execute(
                text(
                    """
                    INSERT INTO conversation.knowledge_summary_reviews (
                        review_id, generation_id, summary_id, user_id, candidate_index,
                        reason_code, proposed_content, status, created_at
                    ) VALUES (
                        :review_id, :generation_id, :summary_id, :user_id, 0,
                        'UNSAFE_REPLACE', CAST(:proposed_content AS jsonb), 'pending', :now
                    )
                    """
                ),
                {
                    "review_id": uuid4(),
                    "generation_id": generation_id,
                    "summary_id": summary_id,
                    "user_id": user_id,
                    "proposed_content": json.dumps(
                        {
                            "proposed_topic_title": "椭圆的离心率",
                            "proposed_sections": {"formulas": ["椭圆满足 e=c/a，且 0<e<1。"]},
                        },
                        ensure_ascii=False,
                    ),
                    "now": now,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO conversation.knowledge_summary_duplicate_candidates (
                        duplicate_id, generation_id, summary_id, possible_target_summary_id,
                        user_id, match_score, status, created_at, updated_at
                    ) VALUES (
                        :duplicate_id, :generation_id, :summary_id, :target_summary_id,
                        :user_id, 0.80000, 'pending', :now, :now
                    )
                    """
                ),
                {
                    "duplicate_id": uuid4(),
                    "generation_id": generation_id,
                    "summary_id": summary_id,
                    "target_summary_id": counterpart_id,
                    "user_id": user_id,
                    "now": now,
                },
            )
    return {
        "user_id": user_id,
        "summary_id": summary_id,
        "counterpart_id": counterpart_id,
        "other_summary_id": other_summary_id,
    }


async def test_read_api_is_user_scoped_and_returns_structured_detail(
    conversation_session_factory,
    client: AsyncClient,
) -> None:
    """列表、统计与详情只返回当前用户数据，review/duplicate 不扫描 Job JSON。"""
    ids = await _seed_summary_fixture(conversation_session_factory)
    headers = _auth(ids["user_id"])

    response = await client.get(
        "/api/v1/knowledge-summaries",
        params={"query": "椭圆的离心率", "sort": "relevance_desc"},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["summary_id"] == str(ids["summary_id"])
    assert str(ids["other_summary_id"]) not in {item["summary_id"] for item in body["items"]}
    assert body["items"][0]["review_state"] == "conflict"

    groups = await client.get("/api/v1/knowledge-summaries/topic-groups", headers=headers)
    assert groups.status_code == 200
    assert groups.json()["items"] == [
        {
            "key": "圆锥曲线",
            "title": "圆锥曲线",
            "summary_count": 2,
            "updated_at": "2026-08-17T08:00:00Z",
        }
    ]

    stats = await client.get("/api/v1/knowledge-summaries/stats", headers=headers)
    assert stats.status_code == 200
    assert stats.json()["active_count"] == 2
    assert stats.json()["pending_review_count"] == 2
    assert stats.json()["available_source_count"] == 4

    detail = await client.get(
        f"/api/v1/knowledge-summaries/{ids['counterpart_id']}", headers=headers
    )
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["review_state"] == "possible_duplicate"
    assert detail_body["possible_duplicates"][0]["topic_title"] == "椭圆的离心率"
    assert detail_body["pending_reviews"] == []

    original_detail = await client.get(
        f"/api/v1/knowledge-summaries/{ids['summary_id']}", headers=headers
    )
    assert original_detail.status_code == 200
    assert original_detail.json()["pending_reviews"][0]["proposed_sections"] == {
        "formulas": ["椭圆满足 e=c/a，且 0<e<1。"]
    }

    forbidden = await client.get(
        f"/api/v1/knowledge-summaries/{ids['other_summary_id']}", headers=headers
    )
    assert forbidden.status_code == 404
    assert forbidden.json()["error"]["code"] == "KNOWLEDGE_SUMMARY_NOT_FOUND"


async def test_source_cards_group_before_pagination_and_bind_summary_version(
    conversation_session_factory,
    client: AsyncClient,
) -> None:
    """来源卡先 Turn 聚合后 keyset，unavailable 不重排且 cursor 绑定 summary version。"""
    ids = await _seed_summary_fixture(conversation_session_factory)
    headers = _auth(ids["user_id"])
    path = f"/api/v1/knowledge-summaries/{ids['summary_id']}/sources"

    first_page = await client.get(path, params={"limit": 1}, headers=headers)
    assert first_page.status_code == 200
    first = first_page.json()
    assert first["has_more"] is True
    assert first["items"][0]["source_turn_id"] == first["items"][0]["turn_id"]
    assert first["items"][0]["status"] == "available"
    assert first["items"][0]["question_excerpt"] == "如何判断椭圆的离心率？"
    assert "source_id" not in first["items"][0]

    second_page = await client.get(
        path,
        params={"limit": 1, "cursor": first["next_cursor"]},
        headers=headers,
    )
    assert second_page.status_code == 200
    second = second_page.json()["items"][0]
    assert second["status"] == "unavailable"
    assert second["question_excerpt"] is None

    async with conversation_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE conversation.knowledge_summaries SET version = version + 1 "
                    "WHERE summary_id = :summary_id"
                ),
                {"summary_id": ids["summary_id"]},
            )
    stale_cursor = await client.get(
        path,
        params={"cursor": first["next_cursor"]},
        headers=headers,
    )
    assert stale_cursor.status_code == 422
    assert stale_cursor.json()["error"]["code"] == "KNOWLEDGE_SUMMARY_INVALID_CURSOR"


async def test_list_cursor_binds_normalized_filters_and_rejects_relevance_without_query(
    conversation_session_factory,
    client: AsyncClient,
) -> None:
    """§15.1 cursor 不可跨筛选复用；无 query 不能请求 relevance 排序。"""
    ids = await _seed_summary_fixture(conversation_session_factory)
    headers = _auth(ids["user_id"])
    response = await client.get(
        "/api/v1/knowledge-summaries",
        params={"sort": "updated_desc", "limit": 1},
        headers=headers,
    )
    assert response.status_code == 200
    cursor = response.json()["next_cursor"]
    assert cursor is not None

    invalid = await client.get(
        "/api/v1/knowledge-summaries",
        params={"sort": "updated_desc", "topic_group": "圆锥曲线", "cursor": cursor},
        headers=headers,
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "KNOWLEDGE_SUMMARY_INVALID_CURSOR"

    relevance_without_query = await client.get(
        "/api/v1/knowledge-summaries",
        params={"sort": "relevance_desc"},
        headers=headers,
    )
    assert relevance_without_query.status_code == 422


async def test_patch_delete_revision_and_tombstone_are_atomic(
    conversation_session_factory,
    client: AsyncClient,
) -> None:
    """§15.6/§15.7：编辑保护、版本冲突、Revision 与墓碑删除必须原子完成。"""
    ids = await _seed_summary_fixture(conversation_session_factory)
    headers = _auth(ids["user_id"])
    path = f"/api/v1/knowledge-summaries/{ids['summary_id']}"

    detail = await client.get(path, headers=headers)
    assert detail.status_code == 200
    detail_body = detail.json()
    formula = detail_body["content"]["formulas"][0]
    patch = await client.patch(
        path,
        headers=headers,
        json={
            "expected_version": 1,
            "sections": {
                "formulas": [
                    {"item_id": formula["item_id"], "text": formula["text"]},
                    {"item_id": None, "text": "用户补充的椭圆判定条件。"},
                ]
            },
        },
    )
    assert patch.status_code == 200
    patched = patch.json()
    assert patched["version"] == 2
    assert patched["protected_sections"] == ["formulas"]
    assert patched["content"]["formulas"][0]["origin"] == "ai"
    assert patched["content"]["formulas"][1]["origin"] == "user"
    assert patched["content"]["formulas"][1]["source_ids"] == []

    group_patch = await client.patch(
        path,
        headers=headers,
        json={
            "expected_version": 2,
            "topic_group_title": "二次曲线",
        },
    )
    assert group_patch.status_code == 200
    assert group_patch.json()["version"] == 3

    async with conversation_session_factory() as session:
        alias_rows = (
            (
                await session.execute(
                    text(
                        """
                    SELECT normalized_topic_group, normalized_alias
                    FROM conversation.knowledge_summary_aliases
                    WHERE summary_id = :summary_id
                    ORDER BY normalized_topic_group, normalized_alias
                    """
                    ),
                    {"summary_id": ids["summary_id"]},
                )
            )
            .mappings()
            .all()
        )
    assert [dict(row) for row in alias_rows] == [
        {"normalized_topic_group": "二次曲线", "normalized_alias": "椭圆的离心率"},
        {"normalized_topic_group": "圆锥曲线", "normalized_alias": "椭圆的离心率"},
    ]

    stale = await client.patch(
        path,
        headers=headers,
        json={"expected_version": 2, "topic_title": "过期编辑"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "KNOWLEDGE_SUMMARY_VERSION_CONFLICT"
    assert stale.json()["error"]["current_version"] == 3

    deleted = await client.delete(path, params={"expected_version": 3}, headers=headers)
    assert deleted.status_code == 204
    repeated = await client.delete(path, params={"expected_version": 1}, headers=headers)
    assert repeated.status_code == 204

    hidden = await client.get(path, headers=headers)
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "KNOWLEDGE_SUMMARY_NOT_FOUND"

    async with conversation_session_factory() as session:
        tombstone = (
            (
                await session.execute(
                    text(
                        """
                    SELECT normalized_topic_group, normalized_topic_title,
                           cardinality(normalized_aliases) AS alias_count
                    FROM conversation.knowledge_summary_tombstones
                    WHERE user_id = :user_id AND deleted_summary_id = :summary_id
                    """
                    ),
                    {"user_id": ids["user_id"], "summary_id": ids["summary_id"]},
                )
            )
            .mappings()
            .one()
        )
        turn_count = (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM conversation.knowledge_summary_tombstone_turns
                    WHERE user_id = :user_id AND tombstone_id = (
                        SELECT tombstone_id
                        FROM conversation.knowledge_summary_tombstones
                        WHERE user_id = :user_id AND deleted_summary_id = :summary_id
                    )
                    """
                ),
                {"user_id": ids["user_id"], "summary_id": ids["summary_id"]},
            )
        ).scalar_one()
        revision = (
            (
                await session.execute(
                    text(
                        """
                    SELECT version, mutation_type
                    FROM conversation.knowledge_summary_revisions
                    WHERE summary_id = :summary_id
                    ORDER BY version DESC
                    LIMIT 1
                    """
                    ),
                    {"summary_id": ids["summary_id"]},
                )
            )
            .mappings()
            .one()
        )
        review_status = (
            await session.execute(
                text(
                    """
                    SELECT status
                    FROM conversation.knowledge_summary_reviews
                    WHERE summary_id = :summary_id
                    """
                ),
                {"summary_id": ids["summary_id"]},
            )
        ).scalar_one()
        duplicate = (
            (
                await session.execute(
                    text(
                        """
                    SELECT status, resolution_reason
                    FROM conversation.knowledge_summary_duplicate_candidates
                    WHERE summary_id = :summary_id
                    """
                    ),
                    {"summary_id": ids["summary_id"]},
                )
            )
            .mappings()
            .one()
        )
    assert tombstone["normalized_topic_group"] == "二次曲线"
    assert tombstone["normalized_topic_title"] == "椭圆的离心率"
    assert tombstone["alias_count"] == 2
    assert int(turn_count) == 3
    assert dict(revision) == {"version": 4, "mutation_type": "delete"}
    assert review_status == "resolved"
    assert dict(duplicate) == {"status": "resolved", "resolution_reason": "summary_deleted"}


async def _complete_turn_with_checkpoint(
    session,
    *,
    user_id: UUID,
    occurred_at: datetime,
    question: str,
    answer: str,
    sequence_base: int,
    checkpoint: str = "conv-src-v1:test-checkpoint",
) -> tuple[UUID, UUID]:
    """写入 completed Turn 并设置 source_checkpoint_id，供生成 API 测试复用。"""
    thread_id, turn_id, _user_message_id, _assistant_message_id = await _insert_turn_with_messages(
        session,
        user_id=user_id,
        occurred_at=occurred_at,
        question=question,
        answer=answer,
        sequence_base=sequence_base,
    )
    await session.execute(
        text(
            "UPDATE conversation.conversation_turns "
            "SET status = 'completed', source_checkpoint_id = :checkpoint "
            "WHERE turn_id = :turn_id"
        ),
        {"turn_id": turn_id, "checkpoint": checkpoint},
    )
    return thread_id, turn_id


@pytest.mark.asyncio
async def test_manual_generation_creates_job_and_returns_status(
    generation_client: AsyncClient,
    conversation_session_factory,
) -> None:
    """手动生成端点为 completed Turn 创建 Job 并可通过 status 路径查询。"""
    user_id = uuid4()
    occurred_at = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
    async with conversation_session_factory() as session:
        async with session.begin():
            thread_id, turn_id = await _complete_turn_with_checkpoint(
                session,
                user_id=user_id,
                occurred_at=occurred_at,
                question="请说明椭圆离心率。",
                answer="椭圆离心率 e=c/a，其中 a>c>0。",
                sequence_base=1,
            )

    path = f"/api/v1/conversations/{thread_id}/turns/{turn_id}/knowledge-summary-generations"
    headers = _auth(user_id)
    response = await generation_client.post(
        path,
        headers=headers,
        json={"client_request_id": "manual-gen-1", "force": False},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["trigger"] == "manual"
    assert body["status"] == "pending"
    assert body["status_path"].endswith(body["generation_id"])

    status_response = await generation_client.get(body["status_path"], headers=headers)
    assert status_response.status_code == 200
    status_body = status_response.json()
    assert status_body["generation_id"] == body["generation_id"]
    assert status_body["thread_id"] == str(thread_id)
    assert status_body["turn_id"] == str(turn_id)
    assert status_body["status"] == "pending"
    assert status_body["retryable"] is True


@pytest.mark.asyncio
async def test_manual_generation_is_idempotent_and_returns_same_job(
    generation_client: AsyncClient,
    conversation_session_factory,
) -> None:
    """同一 client_request_id 重复请求幂等返回同一 Generation。"""
    user_id = uuid4()
    occurred_at = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
    async with conversation_session_factory() as session:
        async with session.begin():
            thread_id, turn_id = await _complete_turn_with_checkpoint(
                session,
                user_id=user_id,
                occurred_at=occurred_at,
                question="请说明椭圆离心率。",
                answer="椭圆离心率 e=c/a，其中 a>c>0。",
                sequence_base=1,
            )

    path = f"/api/v1/conversations/{thread_id}/turns/{turn_id}/knowledge-summary-generations"
    headers = _auth(user_id)
    first = await generation_client.post(
        path,
        headers=headers,
        json={"client_request_id": "idem-1", "force": False},
    )
    assert first.status_code == 202
    second = await generation_client.post(
        path,
        headers=headers,
        json={"client_request_id": "idem-1", "force": False},
    )
    assert second.status_code == 202
    assert second.json()["generation_id"] == first.json()["generation_id"]


@pytest.mark.asyncio
async def test_manual_generation_rate_limited_by_user(
    generation_client: AsyncClient,
    conversation_session_factory,
) -> None:
    """user 桶固定窗口限流：超过每分钟限制后返回 429 并带 Retry-After。"""
    user_id = uuid4()
    occurred_at = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
    async with conversation_session_factory() as session:
        async with session.begin():
            thread_id, turn_id = await _complete_turn_with_checkpoint(
                session,
                user_id=user_id,
                occurred_at=occurred_at,
                question="请说明椭圆离心率。",
                answer="椭圆离心率 e=c/a，其中 a>c>0。",
                sequence_base=1,
            )

    path = f"/api/v1/conversations/{thread_id}/turns/{turn_id}/knowledge-summary-generations"
    headers = _auth(user_id)
    # force=true 会跳过复用逻辑、创建新的 manual_refresh Job；
    # fixture 将 user 桶限制设为 2，连续 3 次实际创建应触发限流。
    for i in range(3):
        response = await generation_client.post(
            path,
            headers=headers,
            json={"client_request_id": f"rate-{i}", "force": True},
        )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "KNOWLEDGE_SUMMARY_RATE_LIMITED"
    assert "retry-after" in response.headers
    assert int(response.headers["retry-after"]) > 0


@pytest.mark.asyncio
async def test_current_turn_generation_returns_latest_job(
    generation_client: AsyncClient,
    conversation_session_factory,
) -> None:
    """当前 Turn Generation 查询返回最新非 cancelled Job。"""
    user_id = uuid4()
    occurred_at = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
    async with conversation_session_factory() as session:
        async with session.begin():
            thread_id, turn_id = await _complete_turn_with_checkpoint(
                session,
                user_id=user_id,
                occurred_at=occurred_at,
                question="请说明椭圆离心率。",
                answer="椭圆离心率 e=c/a，其中 a>c>0。",
                sequence_base=1,
            )

    path = f"/api/v1/conversations/{thread_id}/turns/{turn_id}/knowledge-summary-generation"
    headers = _auth(user_id)
    empty = await generation_client.get(path, headers=headers)
    assert empty.status_code == 200
    assert empty.json()["generation"] is None

    create_path = f"/api/v1/conversations/{thread_id}/turns/{turn_id}/knowledge-summary-generations"
    created = await generation_client.post(
        create_path,
        headers=headers,
        json={"client_request_id": "current-1", "force": False},
    )
    assert created.status_code == 202

    current = await generation_client.get(path, headers=headers)
    assert current.status_code == 200
    assert current.json()["generation"]["generation_id"] == created.json()["generation_id"]


@pytest.mark.asyncio
async def test_dismiss_review_resolves_review_and_bumps_version(
    generation_client: AsyncClient,
    conversation_session_factory,
) -> None:
    """dismiss review 将 pending review 标记为 dismissed 并提升 summary version。"""
    user_id = uuid4()
    summary_id = uuid4()
    generation_id = uuid4()
    review_id = uuid4()
    occurred_at = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
    source_id = uuid4()
    async with conversation_session_factory() as session:
        async with session.begin():
            thread_id, turn_id = await _complete_turn_with_checkpoint(
                session,
                user_id=user_id,
                occurred_at=occurred_at,
                question="请说明椭圆离心率。",
                answer="椭圆离心率 e=c/a，其中 a>c>0。",
                sequence_base=1,
                checkpoint="checkpoint",
            )
            await _insert_summary(
                session,
                summary_id=summary_id,
                user_id=user_id,
                topic_group_title="圆锥曲线",
                topic_title="椭圆离心率",
                source_ids=[source_id],
                updated_at=occurred_at,
            )
            await session.execute(
                text(
                    "UPDATE conversation.knowledge_summaries "
                    "SET review_state = 'conflict' WHERE summary_id = :summary_id"
                ),
                {"summary_id": summary_id},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO conversation.knowledge_summary_generation_jobs (
                        generation_id, idempotency_key, client_request_id, user_id,
                        thread_id, turn_id, source_checkpoint_id, trigger, status,
                        primary_turn_occurred_at, created_at, updated_at
                    ) VALUES (
                        :generation_id, 'idem', NULL, :user_id,
                        :thread_id, :turn_id, 'checkpoint', 'manual', 'needs_review',
                        :occurred_at, :created_at, :created_at
                    )
                    """
                ),
                {
                    "generation_id": generation_id,
                    "user_id": user_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "occurred_at": occurred_at,
                    "created_at": occurred_at,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO conversation.knowledge_summary_reviews (
                        review_id, summary_id, generation_id, user_id, candidate_index,
                        reason_code, internal_reason, proposed_content, status, created_at
                    ) VALUES (
                        :review_id, :summary_id, :generation_id, :user_id, 0,
                        'CONTRADICTORY_CONTENT', '测试冲突',
                        CAST('{"overview": null}' AS jsonb), 'pending', :created_at
                    )
                    """
                ),
                {
                    "review_id": review_id,
                    "summary_id": summary_id,
                    "generation_id": generation_id,
                    "user_id": user_id,
                    "created_at": occurred_at,
                },
            )

    path = f"/api/v1/knowledge-summary-generations/{generation_id}/dismiss-review"
    response = await generation_client.post(
        path,
        headers=_auth(user_id),
        json={"review_id": str(review_id)},
    )
    assert response.status_code == 204

    async with conversation_session_factory() as session:
        async with session.begin():
            review_status = (
                await session.execute(
                    text(
                        "SELECT status FROM conversation.knowledge_summary_reviews "
                        "WHERE review_id = :review_id"
                    ),
                    {"review_id": review_id},
                )
            ).scalar_one()
            summary = (
                (
                    await session.execute(
                        text(
                            "SELECT review_state, version FROM conversation.knowledge_summaries "
                            "WHERE summary_id = :summary_id"
                        ),
                        {"summary_id": summary_id},
                    )
                )
                .mappings()
                .one()
            )
    assert review_status == "dismissed"
    assert summary["review_state"] == "clean"
    assert summary["version"] == 2


@pytest.mark.asyncio
async def test_manual_retry_reads_cancelled_job_and_rejects_deleted_source(
    generation_client: AsyncClient,
    conversation_session_factory,
) -> None:
    """手动重试必须看到 cancelled Job，并拒绝已删除 Thread 的来源。"""
    user_id = uuid4()
    occurred_at = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
    async with conversation_session_factory() as session:
        async with session.begin():
            thread_id, turn_id = await _complete_turn_with_checkpoint(
                session,
                user_id=user_id,
                occurred_at=occurred_at,
                question="请说明椭圆离心率。",
                answer="椭圆离心率 e=c/a，其中 a>c>0。",
                sequence_base=1,
                checkpoint="cancel-checkpoint",
            )
            generation_id = uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO conversation.knowledge_summary_generation_jobs (
                        generation_id, idempotency_key, client_request_id, user_id,
                        thread_id, turn_id, source_checkpoint_id, trigger, status,
                        last_error_code, primary_turn_occurred_at, created_at, updated_at,
                        completed_at
                    ) VALUES (
                        :generation_id, :idempotency_key, NULL, :user_id,
                        :thread_id, :turn_id, 'cancel-checkpoint', 'manual', 'cancelled',
                        'THREAD_DELETED', :occurred_at, :occurred_at, :occurred_at,
                        :occurred_at
                    )
                    """
                ),
                {
                    "generation_id": generation_id,
                    "idempotency_key": f"cancel:{generation_id}",
                    "user_id": user_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "occurred_at": occurred_at,
                },
            )
            await session.execute(
                text(
                    "UPDATE conversation.conversation_threads SET status = 'deleting' "
                    "WHERE thread_id = :thread_id"
                ),
                {"thread_id": thread_id},
            )

    path = f"/api/v1/conversations/{thread_id}/turns/{turn_id}/knowledge-summary-generations"
    response = await generation_client.post(
        path,
        headers=_auth(user_id),
        json={"client_request_id": "retry-after-delete", "force": False},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"
