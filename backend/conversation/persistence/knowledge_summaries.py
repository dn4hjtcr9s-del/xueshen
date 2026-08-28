"""知识总结的 Conversation 数据访问层（知识总结方案 §7、§15）。

所有用户可见查询都显式绑定 user_id，且只读取结构化的 summary、review、
duplicate 和消息级 source 表；本模块绝不解析 Generation Job JSON 作为查询事实源。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.conversation.contracts.knowledge_summary import KnowledgeSummaryContent
from backend.conversation.knowledge_summary.normalization import state_hash_v1


async def get_active_summary(
    session: AsyncSession, *, user_id: UUID, summary_id: UUID
) -> dict[str, Any] | None:
    """读取一张当前用户 active 总结，并计算结构化的有效 review_state。"""
    result = await session.execute(
        text(
            """
            SELECT s.*,
                   CASE
                     WHEN EXISTS (
                       SELECT 1 FROM conversation.knowledge_summary_reviews r
                       WHERE r.summary_id = s.summary_id
                         AND r.user_id = :user_id
                         AND r.status = 'pending'
                     ) THEN 'conflict'
                     WHEN EXISTS (
                       SELECT 1
                       FROM conversation.knowledge_summary_duplicate_candidates d
                       WHERE d.user_id = :user_id
                         AND d.status = 'pending'
                         AND (d.summary_id = s.summary_id
                              OR d.possible_target_summary_id = s.summary_id)
                     ) THEN 'possible_duplicate'
                     ELSE 'clean'
                   END AS effective_review_state
            FROM conversation.knowledge_summaries s
            WHERE s.summary_id = :summary_id
              AND s.user_id = :user_id
              AND s.status = 'active'
            """
        ),
        {"summary_id": summary_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def list_summaries(
    session: AsyncSession,
    *,
    user_id: UUID,
    query_canonical: str | None,
    query_raw: str | None,
    topic_group: str | None,
    section_types: Sequence[str],
    review_state: str | None,
    sort: str,
    last_keys: dict[str, Any] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """按 §15.1 的 exact、substring、trigram 规则读取 summary keyset 页。"""
    params: dict[str, Any] = {
        "user_id": user_id,
        "topic_group": topic_group,
        "review_state": review_state,
        "limit": limit,
    }
    base_filters = ["s.user_id = :user_id", "s.status = 'active'"]
    if topic_group is not None:
        base_filters.append("s.normalized_topic_group = :topic_group")
    base_filters.extend(_section_nonempty_filters(section_types))

    if query_canonical is None or query_raw is None:
        base_sql = _summary_base_sql("FALSE", "FALSE", "0::double precision", "FALSE")
        query_filter = "TRUE"
    else:
        params.update(
            {
                "query_canonical": query_canonical,
                "query_raw": query_raw,
                "canonical_pattern": _ilike_pattern(query_canonical),
                "raw_pattern": _ilike_pattern(query_raw),
            }
        )
        base_sql = _summary_base_sql(
            """EXISTS (
                    SELECT 1 FROM conversation.knowledge_summary_aliases a
                    WHERE a.summary_id = s.summary_id
                      AND a.user_id = :user_id
                      AND a.normalized_alias = :query_canonical
                )""",
            """EXISTS (
                    SELECT 1 FROM conversation.knowledge_summary_aliases a
                    WHERE a.summary_id = s.summary_id
                      AND a.user_id = :user_id
                      AND a.normalized_alias ILIKE :canonical_pattern ESCAPE '\\'
                )""",
            """COALESCE((
                    SELECT MAX(similarity(a.normalized_alias, :query_canonical))
                    FROM conversation.knowledge_summary_aliases a
                    WHERE a.summary_id = s.summary_id
                      AND a.user_id = :user_id
                ), 0)""",
            """(
                    s.normalized_topic_title ILIKE :canonical_pattern ESCAPE '\\'
                    OR s.normalized_topic_group ILIKE :canonical_pattern ESCAPE '\\'
                    OR s.search_text ILIKE :raw_pattern ESCAPE '\\'
                )""",
        )
        query_filter = "(exact_rank > 0 OR substring_hit = 1 OR query_trigram_score >= 0.30)"

    pagination, pagination_params = _summary_keyset_clause(sort, last_keys)
    params.update(pagination_params)
    order_by = _summary_order_by(sort)
    base_where = " AND ".join(base_filters)
    sql = f"""
        WITH base AS (
            {base_sql}
            WHERE {base_where}
        ), scored AS (
            SELECT base.*,
                   CASE
                     WHEN normalized_topic_title = :query_canonical THEN 3
                     WHEN alias_exact THEN 2
                     WHEN normalized_topic_group = :query_canonical THEN 1
                     ELSE 0
                   END AS exact_rank,
                   CASE
                     WHEN direct_substring_hit OR alias_substring_hit THEN 1
                     ELSE 0
                   END AS substring_hit,
                   ROUND(
                     GREATEST(
                       similarity(normalized_topic_title, :query_canonical),
                       similarity(normalized_topic_group, :query_canonical),
                       similarity(search_text, :query_raw),
                       alias_similarity
                     )::numeric,
                     5
                   ) AS query_trigram_score
            FROM base
        )
        SELECT *
        FROM scored
        WHERE {query_filter}
          AND (CAST(:review_state AS text) IS NULL
               OR effective_review_state = CAST(:review_state AS text))
          {pagination}
        ORDER BY {order_by}
        LIMIT :limit
    """
    if query_canonical is None or query_raw is None:
        # PostgreSQL 不应对 NULL 调用 similarity；无搜索时提供满足 scored CTE 的常量。
        sql = sql.replace(":query_canonical", "NULL::text").replace(":query_raw", "NULL::text")
        sql = sql.replace(
            """ROUND(
                     GREATEST(
                       similarity(normalized_topic_title, NULL::text),
                       similarity(normalized_topic_group, NULL::text),
                       similarity(search_text, NULL::text),
                       alias_similarity
                     )::numeric,
                     5
                   )""",
            "0::numeric",
        )
    result = await session.execute(text(sql), params)
    return [dict(row) for row in result.mappings()]


async def list_topic_groups(
    session: AsyncSession,
    *,
    user_id: UUID,
    query_canonical: str | None,
    last_keys: dict[str, Any] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """按 §15.2 聚合当前用户 active summary 的大主题，并做稳定 keyset 分页。"""
    params: dict[str, Any] = {"user_id": user_id, "limit": limit}
    query_filter = "TRUE"
    if query_canonical is not None:
        params.update(
            {
                "query_canonical": query_canonical,
                "canonical_pattern": _ilike_pattern(query_canonical),
            }
        )
        query_filter = """(
            s.normalized_topic_group = :query_canonical
            OR s.normalized_topic_group ILIKE :canonical_pattern ESCAPE '\\'
            OR similarity(s.normalized_topic_group, :query_canonical) >= 0.30
        )"""
    pagination = ""
    if last_keys is not None:
        params.update(
            {
                "last_updated_at": last_keys["updated_at"],
                "last_key": last_keys["key"],
            }
        )
        pagination = "AND (updated_at, key) < (:last_updated_at, :last_key)"
    result = await session.execute(
        text(
            f"""
            WITH grouped AS (
                SELECT
                    s.normalized_topic_group AS key,
                    (array_agg(
                        s.topic_group_title
                        ORDER BY s.updated_at DESC, s.summary_id ASC
                    ))[1] AS title,
                    COUNT(*)::integer AS summary_count,
                    MAX(s.updated_at) AS updated_at
                FROM conversation.knowledge_summaries s
                WHERE s.user_id = :user_id
                  AND s.status = 'active'
                  AND s.normalized_topic_group <> ''
                  AND {query_filter}
                GROUP BY s.normalized_topic_group
            )
            SELECT *
            FROM grouped
            WHERE TRUE {pagination}
            ORDER BY updated_at DESC, key ASC
            LIMIT :limit
            """
        ),
        params,
    )
    return [dict(row) for row in result.mappings()]


async def get_stats(session: AsyncSession, *, user_id: UUID) -> dict[str, Any]:
    """按 §15.3 返回当前用户的 active、待处理与可用来源统计。"""
    result = await session.execute(
        text(
            """
            WITH active AS (
                SELECT *
                FROM conversation.knowledge_summaries
                WHERE user_id = :user_id AND status = 'active'
            ), pending_summary_ids AS (
                SELECT DISTINCT r.summary_id
                FROM conversation.knowledge_summary_reviews r
                JOIN active s ON s.summary_id = r.summary_id
                WHERE r.user_id = :user_id AND r.status = 'pending'
                UNION
                SELECT DISTINCT CASE
                    WHEN d.summary_id = s.summary_id THEN d.summary_id
                    ELSE d.possible_target_summary_id
                END
                FROM conversation.knowledge_summary_duplicate_candidates d
                JOIN active s
                  ON s.summary_id = d.summary_id
                  OR s.summary_id = d.possible_target_summary_id
                WHERE d.user_id = :user_id AND d.status = 'pending'
            )
            SELECT
                (SELECT COUNT(*) FROM active)::integer AS active_count,
                (SELECT COUNT(*) FROM active
                 WHERE updated_at >= now() - interval '168 hours')::integer AS updated_last_7_days,
                (SELECT COUNT(*) FROM pending_summary_ids)::integer AS pending_review_count,
                COALESCE((SELECT SUM(available_source_count) FROM active), 0)::integer
                    AS available_source_count
            """
        ),
        {"user_id": user_id},
    )
    row = result.mappings().one()
    return dict(row)


async def count_pending_reviews(session: AsyncSession, *, user_id: UUID, summary_id: UUID) -> int:
    """返回详情页所需的全部 pending review 数量，不受展示上限影响。"""
    result = await session.execute(
        text(
            """
            SELECT COUNT(*)
            FROM conversation.knowledge_summary_reviews
            WHERE user_id = :user_id AND summary_id = :summary_id AND status = 'pending'
            """
        ),
        {"user_id": user_id, "summary_id": summary_id},
    )
    return int(result.scalar_one())


async def list_pending_reviews(
    session: AsyncSession, *, user_id: UUID, summary_id: UUID, limit: int
) -> list[dict[str, Any]]:
    """读取最近 pending review；来源 Turn 从关联 Generation 的结构化列获得。"""
    result = await session.execute(
        text(
            """
            SELECT r.review_id, r.generation_id, r.reason_code, r.proposed_content,
                   g.turn_id AS source_turn_id, r.created_at
            FROM conversation.knowledge_summary_reviews r
            JOIN conversation.knowledge_summary_generation_jobs g
              ON g.generation_id = r.generation_id
            WHERE r.user_id = :user_id
              AND r.summary_id = :summary_id
              AND r.status = 'pending'
            ORDER BY r.created_at DESC, r.review_id DESC
            LIMIT :limit
            """
        ),
        {"user_id": user_id, "summary_id": summary_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings()]


async def list_possible_duplicates(
    session: AsyncSession, *, user_id: UUID, summary_id: UUID, limit: int
) -> list[dict[str, Any]]:
    """读取当前卡两端的重复关系；标题始终映射到当前卡的对端总结。"""
    result = await session.execute(
        text(
            """
            SELECT
                d.duplicate_id,
                d.summary_id,
                d.possible_target_summary_id,
                CASE WHEN d.summary_id = :summary_id
                     THEN target.topic_group_title ELSE origin.topic_group_title END
                    AS topic_group_title,
                CASE WHEN d.summary_id = :summary_id
                     THEN target.topic_title ELSE origin.topic_title END AS topic_title,
                d.match_score,
                d.status,
                d.created_at
            FROM conversation.knowledge_summary_duplicate_candidates d
            JOIN conversation.knowledge_summaries origin ON origin.summary_id = d.summary_id
            JOIN conversation.knowledge_summaries target
              ON target.summary_id = d.possible_target_summary_id
            WHERE d.user_id = :user_id
              AND (d.summary_id = :summary_id OR d.possible_target_summary_id = :summary_id)
            ORDER BY d.updated_at DESC, d.duplicate_id DESC
            LIMIT :limit
            """
        ),
        {"user_id": user_id, "summary_id": summary_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings()]


async def list_source_turns(
    session: AsyncSession,
    *,
    user_id: UUID,
    summary_id: UUID,
    last_keys: dict[str, Any] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """先按 Turn 聚合，再按 §15.5 的 occurred_at/turn_id keyset 分页。"""
    params: dict[str, Any] = {
        "user_id": user_id,
        "summary_id": summary_id,
        "limit": limit,
        "cursor_is_null": last_keys is None,
        "last_occurred_at": None if last_keys is None else last_keys["occurred_at"],
        "last_turn_id": None if last_keys is None else last_keys["turn_id"],
    }
    result = await session.execute(
        text(
            """
            WITH source_turns AS (
                SELECT
                    ks.turn_id,
                    (array_agg(ks.thread_id ORDER BY ks.thread_id))[1] AS thread_id,
                    MIN(ks.message_occurred_at) AS occurred_at
                FROM conversation.knowledge_summary_sources ks
                WHERE ks.summary_id = :summary_id
                  AND ks.user_id = :user_id
                GROUP BY ks.turn_id
            ), paged_turns AS (
                SELECT *
                FROM source_turns
                WHERE :cursor_is_null
                   OR (occurred_at, turn_id) < (:last_occurred_at, :last_turn_id)
                ORDER BY occurred_at DESC, turn_id DESC
                LIMIT :limit
            )
            SELECT
                pt.turn_id,
                pt.thread_id,
                pt.occurred_at,
                CASE WHEN BOOL_OR(ks.status = 'available') THEN 'available'
                     ELSE 'unavailable' END AS status,
                ARRAY_AGG(
                    ks.message_id
                    ORDER BY ks.message_occurred_at ASC, ks.message_sequence ASC, ks.message_id ASC
                ) AS support_message_ids,
                ARRAY_AGG(
                    ks.message_role
                    ORDER BY ks.message_occurred_at ASC, ks.message_sequence ASC, ks.message_id ASC
                ) AS support_roles,
                CASE WHEN BOOL_OR(ks.status = 'available') THEN question.content ELSE NULL END
                    AS question_content
            FROM paged_turns pt
            JOIN conversation.knowledge_summary_sources ks
              ON ks.summary_id = :summary_id
             AND ks.user_id = :user_id
             AND ks.turn_id = pt.turn_id
            LEFT JOIN LATERAL (
                SELECT m.content
                FROM conversation.conversation_messages m
                WHERE m.thread_id = pt.thread_id
                  AND m.turn_id = pt.turn_id
                  AND m.role = 'user'
                  AND m.status != 'deleted'
                ORDER BY m.sequence ASC, m.message_id ASC
                LIMIT 1
            ) question ON TRUE
            GROUP BY pt.turn_id, pt.thread_id, pt.occurred_at, question.content
            ORDER BY pt.occurred_at DESC, pt.turn_id DESC
            """
        ),
        params,
    )
    return [dict(row) for row in result.mappings()]


async def claim_enqueue_failed_turns(
    session: AsyncSession,
    *,
    now: datetime,
    batch_size: int = 50,
) -> list[dict[str, Any]]:
    """锁定并返回已到修复时间的 enqueue_failed Turn（§14.1）。"""
    result = await session.execute(
        text(
            """
            SELECT *
            FROM conversation.conversation_turns
            WHERE knowledge_summary_enqueue_status = 'enqueue_failed'
              AND knowledge_summary_enqueue_next_attempt_at <= :now
            ORDER BY knowledge_summary_enqueue_next_attempt_at ASC, turn_id ASC
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"now": now, "limit": batch_size},
    )
    return [dict(row) for row in result.mappings()]


async def update_knowledge_summary_enqueue_status(
    session: AsyncSession,
    *,
    turn_id: UUID,
    status: str,
    attempts_delta: int = 0,
    next_attempt_at: datetime | None = None,
) -> None:
    """更新 Turn 的 enqueue 状态；attempts 增加 attempts_delta。"""
    await session.execute(
        text(
            """
            UPDATE conversation.conversation_turns
            SET knowledge_summary_enqueue_status = :status,
                knowledge_summary_enqueue_attempts =
                    GREATEST(knowledge_summary_enqueue_attempts + :attempts_delta, 0),
                knowledge_summary_enqueue_next_attempt_at = :next_attempt_at,
                updated_at = :now
            WHERE turn_id = :turn_id
            """
        ),
        {
            "turn_id": turn_id,
            "status": status,
            "attempts_delta": attempts_delta,
            "next_attempt_at": next_attempt_at,
            "now": datetime.now(UTC),
        },
    )


async def get_runtime_control(session: AsyncSession) -> dict[str, Any] | None:
    """读取全局自动生成熔断状态，供后续 Worker 与运维接口复用。"""
    result = await session.execute(
        text(
            """
            SELECT * FROM conversation.knowledge_summary_runtime_control
            WHERE control_key = 'global'
            """
        )
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def list_tombstone_turns(
    session: AsyncSession, *, user_id: UUID, turn_id: UUID
) -> list[dict[str, Any]]:
    """读取旧 Turn 的 tombstone 索引，供 Phase 4 同步生成抑制使用。"""
    result = await session.execute(
        text(
            """
            SELECT tombstone_id, source_occurred_at, created_at
            FROM conversation.knowledge_summary_tombstone_turns
            WHERE user_id = :user_id AND turn_id = :turn_id
            ORDER BY source_occurred_at DESC, tombstone_id ASC
            """
        ),
        {"user_id": user_id, "turn_id": turn_id},
    )
    return [dict(row) for row in result.mappings()]


async def get_summary_for_mutation(
    session: AsyncSession, *, user_id: UUID, summary_id: UUID
) -> dict[str, Any] | None:
    """锁定当前用户的一张总结，不预先排除 deleted 行以支持删除幂等。"""
    result = await session.execute(
        text(
            """
            SELECT s.*
            FROM conversation.knowledge_summaries s
            WHERE s.summary_id = :summary_id AND s.user_id = :user_id
            FOR UPDATE
            """
        ),
        {"summary_id": summary_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def get_tombstone_for_deleted_summary(
    session: AsyncSession, *, user_id: UUID, summary_id: UUID
) -> dict[str, Any] | None:
    """按原 summary ID 查询墓碑，供物理清理后的 DELETE 幂等裁决使用。"""
    result = await session.execute(
        text(
            """
            SELECT *
            FROM conversation.knowledge_summary_tombstones
            WHERE user_id = :user_id AND deleted_summary_id = :summary_id
            ORDER BY deleted_at DESC, tombstone_id DESC
            LIMIT 1
            """
        ),
        {"summary_id": summary_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def has_active_identity_conflict(
    session: AsyncSession,
    *,
    user_id: UUID,
    summary_id: UUID,
    normalized_topic_group: str,
    normalized_topic_title: str,
) -> bool:
    """检查编辑后的规范化身份是否碰撞另一张 active 总结。"""
    result = await session.execute(
        text(
            """
            SELECT 1
            FROM conversation.knowledge_summaries
            WHERE user_id = :user_id
              AND normalized_topic_group = :normalized_topic_group
              AND normalized_topic_title = :normalized_topic_title
              AND summary_id <> :summary_id
              AND status = 'active'
            LIMIT 1
            """
        ),
        {
            "user_id": user_id,
            "summary_id": summary_id,
            "normalized_topic_group": normalized_topic_group,
            "normalized_topic_title": normalized_topic_title,
        },
    )
    return result.first() is not None


async def list_summary_aliases(
    session: AsyncSession, *, user_id: UUID, summary_id: UUID
) -> list[dict[str, Any]]:
    """读取带时间和 ID 的 alias 快照，供 tombstone 按冻结规则选取最新 20 条。"""
    result = await session.execute(
        text(
            """
            SELECT normalized_alias, alias_id, created_at
            FROM conversation.knowledge_summary_aliases
            WHERE user_id = :user_id AND summary_id = :summary_id
            ORDER BY created_at DESC, alias_id DESC
            """
        ),
        {"user_id": user_id, "summary_id": summary_id},
    )
    return [dict(row) for row in result.mappings()]


async def upsert_summary_alias(
    session: AsyncSession,
    *,
    alias_id: UUID,
    user_id: UUID,
    summary_id: UUID,
    normalized_topic_group: str,
    display_alias: str,
    normalized_alias: str,
) -> None:
    """写入用户标题 alias；三元唯一键允许跨大主题保留历史标题。"""
    await session.execute(
        text(
            """
            INSERT INTO conversation.knowledge_summary_aliases (
                alias_id, summary_id, user_id, normalized_topic_group,
                display_alias, normalized_alias, created_by
            ) VALUES (
                :alias_id, :summary_id, :user_id, :normalized_topic_group,
                :display_alias, :normalized_alias, 'user'
            )
            ON CONFLICT (summary_id, normalized_topic_group, normalized_alias) DO NOTHING
            """
        ),
        {
            "alias_id": alias_id,
            "summary_id": summary_id,
            "user_id": user_id,
            "normalized_topic_group": normalized_topic_group,
            "display_alias": display_alias,
            "normalized_alias": normalized_alias,
        },
    )


async def update_summary_snapshot(
    session: AsyncSession,
    *,
    summary_id: UUID,
    user_id: UUID,
    topic_group_title: str,
    topic_title: str,
    normalized_topic_group: str,
    normalized_topic_title: str,
    content: dict[str, Any],
    search_text: str,
    protected_sections: list[str],
    version: int,
    content_hash: str,
    state_hash: str,
    review_state: str | None = None,
) -> None:
    """原子更新用户编辑后的当前总结快照。调用方已持有 summary 行锁。"""
    if review_state is not None:
        sql = """
            UPDATE conversation.knowledge_summaries
            SET topic_group_title = :topic_group_title,
                topic_title = :topic_title,
                normalized_topic_group = :normalized_topic_group,
                normalized_topic_title = :normalized_topic_title,
                content = CAST(:content AS jsonb),
                search_text = :search_text,
                protected_sections = :protected_sections,
                version = :version,
                content_hash = :content_hash,
                state_hash = :state_hash,
                review_state = :review_state,
                updated_at = now()
            WHERE summary_id = :summary_id AND user_id = :user_id AND status = 'active'
        """
    else:
        sql = """
            UPDATE conversation.knowledge_summaries
            SET topic_group_title = :topic_group_title,
                topic_title = :topic_title,
                normalized_topic_group = :normalized_topic_group,
                normalized_topic_title = :normalized_topic_title,
                content = CAST(:content AS jsonb),
                search_text = :search_text,
                protected_sections = :protected_sections,
                version = :version,
                content_hash = :content_hash,
                state_hash = :state_hash,
                updated_at = now()
            WHERE summary_id = :summary_id AND user_id = :user_id AND status = 'active'
        """
    params: dict[str, Any] = {
        "summary_id": summary_id,
        "user_id": user_id,
        "topic_group_title": topic_group_title,
        "topic_title": topic_title,
        "normalized_topic_group": normalized_topic_group,
        "normalized_topic_title": normalized_topic_title,
        "content": json.dumps(content, ensure_ascii=False),
        "search_text": search_text,
        "protected_sections": protected_sections,
        "version": version,
        "content_hash": content_hash,
        "state_hash": state_hash,
    }
    if review_state is not None:
        params["review_state"] = review_state
    await session.execute(text(sql), params)


async def insert_revision(
    session: AsyncSession,
    *,
    revision_id: UUID,
    summary_id: UUID,
    user_id: UUID,
    version: int,
    base_version: int,
    mutation_type: str,
    actor_type: str,
    topic_group_title: str,
    topic_title: str,
    content: dict[str, Any],
    protected_sections: list[str],
    content_hash: str,
    changed_sections: list[str],
) -> None:
    """写入不可变 Revision；必须与当前 summary 更新处于同一事务。"""
    await session.execute(
        text(
            """
            INSERT INTO conversation.knowledge_summary_revisions (
                revision_id, summary_id, user_id, version, base_version, mutation_type,
                actor_type, topic_group_title, topic_title, content, protected_sections,
                content_hash, changed_sections, source_ids
            ) VALUES (
                :revision_id, :summary_id, :user_id, :version, :base_version, :mutation_type,
                :actor_type, :topic_group_title, :topic_title, CAST(:content AS jsonb),
                :protected_sections, :content_hash, :changed_sections, '{}'
            )
            """
        ),
        {
            "revision_id": revision_id,
            "summary_id": summary_id,
            "user_id": user_id,
            "version": version,
            "base_version": base_version,
            "mutation_type": mutation_type,
            "actor_type": actor_type,
            "topic_group_title": topic_group_title,
            "topic_title": topic_title,
            "content": json.dumps(content, ensure_ascii=False),
            "protected_sections": protected_sections,
            "content_hash": content_hash,
            "changed_sections": changed_sections,
        },
    )


async def list_tombstone_turn_rows(
    session: AsyncSession, *, user_id: UUID, summary_id: UUID
) -> list[dict[str, Any]]:
    """按来源 Turn 聚合稳定发生时间，供删除事务复制到最小墓碑索引。"""
    result = await session.execute(
        text(
            """
            SELECT ks.turn_id, t.created_at AS source_occurred_at
            FROM conversation.knowledge_summary_sources ks
            JOIN conversation.conversation_turns t ON t.turn_id = ks.turn_id
            WHERE ks.user_id = :user_id AND ks.summary_id = :summary_id
            GROUP BY ks.turn_id, t.created_at
            ORDER BY ks.turn_id ASC
            """
        ),
        {"user_id": user_id, "summary_id": summary_id},
    )
    return [dict(row) for row in result.mappings()]


async def insert_tombstone(
    session: AsyncSession,
    *,
    tombstone_id: UUID,
    user_id: UUID,
    deleted_summary_id: UUID,
    normalized_topic_group: str,
    normalized_topic_title: str,
    normalized_aliases: list[str],
    latest_source_occurred_at: Any | None,
) -> None:
    """写入不含正文的最小墓碑；调用方确保同一 active 删除仅执行一次。"""
    await session.execute(
        text(
            """
            INSERT INTO conversation.knowledge_summary_tombstones (
                tombstone_id, user_id, deleted_summary_id, normalized_topic_group,
                normalized_topic_title, normalized_aliases, deleted_at,
                latest_source_occurred_at
            ) VALUES (
                :tombstone_id, :user_id, :deleted_summary_id, :normalized_topic_group,
                :normalized_topic_title, :normalized_aliases, now(),
                :latest_source_occurred_at
            )
            """
        ),
        {
            "tombstone_id": tombstone_id,
            "user_id": user_id,
            "deleted_summary_id": deleted_summary_id,
            "normalized_topic_group": normalized_topic_group,
            "normalized_topic_title": normalized_topic_title,
            "normalized_aliases": normalized_aliases,
            "latest_source_occurred_at": latest_source_occurred_at,
        },
    )


async def insert_tombstone_turns(
    session: AsyncSession,
    *,
    tombstone_id: UUID,
    user_id: UUID,
    turn_rows: Sequence[dict[str, Any]],
) -> None:
    """批量复制 distinct 来源 Turn，且不保存消息正文或消息 ID。"""
    for row in turn_rows:
        await session.execute(
            text(
                """
                INSERT INTO conversation.knowledge_summary_tombstone_turns (
                    tombstone_id, user_id, turn_id, source_occurred_at
                ) VALUES (:tombstone_id, :user_id, :turn_id, :source_occurred_at)
                """
            ),
            {
                "tombstone_id": tombstone_id,
                "user_id": user_id,
                "turn_id": row["turn_id"],
                "source_occurred_at": row["source_occurred_at"],
            },
        )


async def resolve_reviews_for_deleted_summary(
    session: AsyncSession, *, user_id: UUID, summary_id: UUID
) -> None:
    """删除总结时关闭仍待处理的冲突建议，避免 deleted 卡继续显示待处理状态。"""
    await session.execute(
        text(
            """
            UPDATE conversation.knowledge_summary_reviews
            SET status = 'resolved', resolved_at = now()
            WHERE user_id = :user_id AND summary_id = :summary_id AND status = 'pending'
            """
        ),
        {"user_id": user_id, "summary_id": summary_id},
    )


async def resolve_duplicates_for_deleted_summary(
    session: AsyncSession, *, user_id: UUID, summary_id: UUID
) -> None:
    """按保留的业务方向标记重复关系的系统生命周期终态。"""
    await session.execute(
        text(
            """
            UPDATE conversation.knowledge_summary_duplicate_candidates
            SET status = 'resolved',
                resolution_reason = CASE
                    WHEN summary_id = :summary_id THEN 'summary_deleted'
                    ELSE 'target_deleted'
                END,
                resolved_at = now(),
                updated_at = now()
            WHERE user_id = :user_id
              AND status = 'pending'
              AND (summary_id = :summary_id OR possible_target_summary_id = :summary_id)
            """
        ),
        {"user_id": user_id, "summary_id": summary_id},
    )


async def mark_summary_deleted(
    session: AsyncSession, *, user_id: UUID, summary_id: UUID, version: int
) -> None:
    """软删除当前总结；墓碑与 delete Revision 必须已在同一事务准备完成。"""
    await session.execute(
        text(
            """
            UPDATE conversation.knowledge_summaries
            SET status = 'deleted', version = :version, deleted_at = now(), updated_at = now()
            WHERE summary_id = :summary_id AND user_id = :user_id AND status = 'active'
            """
        ),
        {"user_id": user_id, "summary_id": summary_id, "version": version},
    )


async def cancel_generation_jobs_for_thread(session: AsyncSession, *, thread_id: UUID) -> None:
    """会话删除时仅取消尚未处理的知识总结 Job；processing 由 fencing 自行退出。"""
    await session.execute(
        text(
            """
            UPDATE conversation.knowledge_summary_generation_jobs
            SET status = 'cancelled', last_error_code = 'THREAD_DELETED',
                completed_at = now(), updated_at = now()
            WHERE thread_id = :thread_id AND status IN ('pending', 'retry_wait')
            """
        ),
        {"thread_id": thread_id},
    )


async def mark_sources_unavailable_for_thread(
    session: AsyncSession, *, thread_id: UUID
) -> list[UUID]:
    """将会话的消息级来源设为不可用，并返回受影响 summary ID 的稳定顺序。"""
    result = await session.execute(
        text(
            """
            WITH affected AS (
                SELECT DISTINCT summary_id
                FROM conversation.knowledge_summary_sources
                WHERE thread_id = :thread_id AND status = 'available'
            ), changed AS (
                UPDATE conversation.knowledge_summary_sources
                SET status = 'unavailable', unavailable_at = now()
                WHERE thread_id = :thread_id AND status = 'available'
                RETURNING summary_id
            )
            SELECT summary_id FROM affected ORDER BY summary_id ASC
            """
        ),
        {"thread_id": thread_id},
    )
    return [row["summary_id"] for row in result.mappings()]


async def lock_and_recalculate_source_counts(
    session: AsyncSession, *, summary_ids: Sequence[UUID]
) -> None:
    """按 summary ID 升序加锁并重算三类来源计数，禁止依赖递增/递减猜测。"""
    for summary_id in sorted(set(summary_ids), key=str):
        await session.execute(
            text(
                """
                SELECT summary_id
                FROM conversation.knowledge_summaries
                WHERE summary_id = :summary_id
                FOR UPDATE
                """
            ),
            {"summary_id": summary_id},
        )
        await session.execute(
            text(
                """
                UPDATE conversation.knowledge_summaries s
                SET source_count = counts.source_count,
                    available_source_count = counts.available_source_count,
                    source_message_count = counts.source_message_count,
                    updated_at = now()
                FROM (
                    SELECT
                        COUNT(DISTINCT turn_id)::integer AS source_count,
                        COUNT(DISTINCT turn_id) FILTER (WHERE status = 'available')::integer
                            AS available_source_count,
                        COUNT(*)::integer AS source_message_count
                    FROM conversation.knowledge_summary_sources
                    WHERE summary_id = :summary_id
                ) AS counts
                WHERE s.summary_id = :summary_id
                """
            ),
            {"summary_id": summary_id},
        )


def _summary_base_sql(
    alias_exact: str,
    alias_substring_hit: str,
    alias_similarity: str,
    direct_substring_hit: str,
) -> str:
    """生成列表查询共用 CTE，集中保持 review_state 的结构化计算。"""
    return f"""
        SELECT
            s.*,
            {alias_exact} AS alias_exact,
            {alias_substring_hit} AS alias_substring_hit,
            {alias_similarity} AS alias_similarity,
            {direct_substring_hit} AS direct_substring_hit,
            CASE
              WHEN EXISTS (
                SELECT 1 FROM conversation.knowledge_summary_reviews r
                WHERE r.summary_id = s.summary_id
                  AND r.user_id = :user_id
                  AND r.status = 'pending'
              ) THEN 'conflict'
              WHEN EXISTS (
                SELECT 1
                FROM conversation.knowledge_summary_duplicate_candidates d
                WHERE d.user_id = :user_id
                  AND d.status = 'pending'
                  AND (d.summary_id = s.summary_id
                       OR d.possible_target_summary_id = s.summary_id)
              ) THEN 'possible_duplicate'
              ELSE 'clean'
            END AS effective_review_state
        FROM conversation.knowledge_summaries s
    """


def _section_nonempty_filters(section_types: Sequence[str]) -> list[str]:
    """生成由固定章节白名单驱动的 OR 条件，不拼接外部自由 SQL。"""
    filters: list[str] = []
    for section in section_types:
        if section == "overview":
            filters.append(
                "(s.content -> 'overview') IS NOT NULL AND s.content -> 'overview' <> 'null'::jsonb"
            )
        else:
            filters.append(
                f"jsonb_array_length(COALESCE(s.content -> '{section}', '[]'::jsonb)) > 0"
            )
    return ["(" + " OR ".join(filters) + ")"] if filters else []


def _summary_keyset_clause(
    sort: str, last_keys: dict[str, Any] | None
) -> tuple[str, dict[str, Any]]:
    """为三种冻结排序生成可重放的 after 条件。"""
    if last_keys is None:
        return "", {}
    if sort == "relevance_desc":
        return (
            """AND (
                exact_rank < :last_exact_rank
                OR (exact_rank = :last_exact_rank AND substring_hit < :last_substring_hit)
                OR (exact_rank = :last_exact_rank AND substring_hit = :last_substring_hit
                    AND query_trigram_score < :last_query_trigram_score)
                OR (exact_rank = :last_exact_rank AND substring_hit = :last_substring_hit
                    AND query_trigram_score = :last_query_trigram_score
                    AND updated_at < :last_updated_at)
                OR (exact_rank = :last_exact_rank AND substring_hit = :last_substring_hit
                    AND query_trigram_score = :last_query_trigram_score
                    AND updated_at = :last_updated_at AND summary_id > :last_summary_id)
            )""",
            {
                "last_exact_rank": last_keys["exact_rank"],
                "last_substring_hit": last_keys["substring_hit"],
                "last_query_trigram_score": last_keys["query_trigram_score"],
                "last_updated_at": last_keys["updated_at"],
                "last_summary_id": last_keys["summary_id"],
            },
        )
    if sort == "updated_desc":
        return (
            "AND (updated_at, summary_id) < (:last_updated_at, :last_summary_id)",
            {
                "last_updated_at": last_keys["updated_at"],
                "last_summary_id": last_keys["summary_id"],
            },
        )
    return (
        """AND (
            normalized_topic_group > :last_normalized_topic_group
            OR (normalized_topic_group = :last_normalized_topic_group
                AND normalized_topic_title > :last_normalized_topic_title)
            OR (normalized_topic_group = :last_normalized_topic_group
                AND normalized_topic_title = :last_normalized_topic_title
                AND summary_id > :last_summary_id)
        )""",
        {
            "last_normalized_topic_group": last_keys["normalized_topic_group"],
            "last_normalized_topic_title": last_keys["normalized_topic_title"],
            "last_summary_id": last_keys["summary_id"],
        },
    )


def _summary_order_by(sort: str) -> str:
    """返回 §15.1 的固定排序，不接受外部字段名。"""
    if sort == "relevance_desc":
        return (
            "exact_rank DESC, substring_hit DESC, query_trigram_score DESC, "
            "updated_at DESC, summary_id ASC"
        )
    if sort == "updated_desc":
        return "updated_at DESC, summary_id DESC"
    return "normalized_topic_group ASC, normalized_topic_title ASC, summary_id ASC"


def _ilike_pattern(value: str) -> str:
    """转义 PostgreSQL ILIKE 的通配符，确保 substring 搜索按用户原始文字匹配。"""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ---------------------------------------------------------------------------
# Phase 3 Generation Worker 持久化入口
# ---------------------------------------------------------------------------


async def get_generation_source_rows(
    session: AsyncSession, *, thread_id: UUID, turn_id: UUID, user_id: UUID
) -> dict[str, Any] | None:
    """读取 Worker 构建 input_manifest 所需的线程、Turn 和主消息事实。"""
    result = await session.execute(
        text(
            """
            SELECT
                t.turn_id, t.thread_id, t.user_id, t.status AS turn_status,
                t.user_message_id, t.assistant_message_id, t.source_checkpoint_id,
                th.status AS thread_status,
                u.message_id AS user_message_id_actual, u.role AS user_role,
                u.sequence AS user_sequence, u.content AS user_content,
                u.content_hash AS user_content_hash, u.status AS user_status,
                u.eligible_for_context AS user_eligible_for_context,
                u.occurred_at AS user_occurred_at,
                a.message_id AS assistant_message_id_actual, a.role AS assistant_role,
                a.sequence AS assistant_sequence, a.content AS assistant_content,
                a.content_hash AS assistant_content_hash, a.status AS assistant_status,
                a.eligible_for_context AS assistant_eligible_for_context,
                a.occurred_at AS assistant_occurred_at
            FROM conversation.conversation_turns t
            JOIN conversation.conversation_threads th ON th.thread_id = t.thread_id
            JOIN conversation.conversation_messages u ON u.message_id = t.user_message_id
            LEFT JOIN conversation.conversation_messages a ON a.message_id = t.assistant_message_id
            WHERE t.turn_id = :turn_id AND t.thread_id = :thread_id AND t.user_id = :user_id
              AND th.user_id = :user_id
            """
        ),
        {"thread_id": thread_id, "turn_id": turn_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def list_context_source_messages(
    session: AsyncSession,
    *,
    thread_id: UUID,
    before_sequence: int,
    limit: int,
) -> list[dict[str, Any]]:
    """取当前用户消息前最近连续的可用上下文候选，调用方按 token 预算截断。"""
    result = await session.execute(
        text(
            """
            SELECT message_id, turn_id, role, sequence, content, content_hash, occurred_at
            FROM conversation.conversation_messages
            WHERE thread_id = :thread_id AND sequence < :before_sequence
              AND status = 'completed' AND eligible_for_context = true
            ORDER BY sequence DESC
            LIMIT :limit
            """
        ),
        {"thread_id": thread_id, "before_sequence": before_sequence, "limit": limit},
    )
    return [dict(row) for row in result.mappings()]


async def get_previous_conversation_summary(
    session: AsyncSession, *, thread_id: UUID, before_sequence: int
) -> dict[str, Any] | None:
    """读取主来源前最近一条 Conversation Summary，仅用于主题消歧。"""
    result = await session.execute(
        text(
            """
            SELECT sequence, content
            FROM conversation.conversation_summaries
            WHERE thread_id = :thread_id AND sequence < :before_sequence
            ORDER BY sequence DESC
            LIMIT 1
            """
        ),
        {"thread_id": thread_id, "before_sequence": before_sequence},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def recall_summary_candidates(
    session: AsyncSession,
    *,
    user_id: UUID,
    topic_group: str,
    topic_title: str,
    aliases: Sequence[str],
) -> list[dict[str, Any]]:
    """按 §11.2 使用 SQL exact/trigram 召回并稳定排序候选总结。"""
    result = await session.execute(
        text(
            """
            WITH terms AS (
                SELECT CAST(:topic_title AS text) AS term
                UNION ALL
                SELECT unnest(CAST(:aliases AS text[]))
            ), scored AS (
                SELECT
                    s.summary_id, s.user_id, s.topic_group_title, s.topic_title,
                    s.normalized_topic_group, s.normalized_topic_title, s.content,
                    s.protected_sections, s.version, s.state_hash, s.review_state,
                    s.updated_at,
                    MAX(
                        CASE
                            WHEN s.normalized_topic_title = c.term THEN 3
                            WHEN a.normalized_alias = c.term THEN 2
                            ELSE 0
                        END
                    ) AS exact_kind,
                    MAX(GREATEST(
                        similarity(s.normalized_topic_title, c.term),
                        COALESCE((
                            SELECT MAX(similarity(a2.normalized_alias, c.term))
                            FROM conversation.knowledge_summary_aliases a2
                            WHERE a2.summary_id = s.summary_id
                        ), 0)
                    )) AS title_score,
                    similarity(s.normalized_topic_group, :topic_group) AS group_score
                FROM conversation.knowledge_summaries s
                CROSS JOIN terms c
                LEFT JOIN conversation.knowledge_summary_aliases a
                  ON a.summary_id = s.summary_id
                WHERE s.user_id = :user_id AND s.status = 'active'
                GROUP BY s.summary_id, s.user_id, s.topic_group_title, s.topic_title,
                         s.normalized_topic_group, s.normalized_topic_title, s.content,
                         s.protected_sections, s.version, s.state_hash, s.review_state,
                         s.updated_at
            )
            SELECT *,
                   (0.85 * title_score + 0.15 * group_score) AS final_score
            FROM scored
            WHERE exact_kind > 0
               OR (group_score >= 0.35
                   AND (0.85 * title_score + 0.15 * group_score) >= 0.35)
               OR (group_score < 0.35
                   AND (0.85 * title_score + 0.15 * group_score) >= 0.55)
            ORDER BY exact_kind DESC, final_score DESC, updated_at DESC, summary_id ASC
            LIMIT 5
            """
        ),
        {
            "user_id": user_id,
            "topic_group": topic_group,
            "topic_title": topic_title,
            "aliases": list(aliases),
        },
    )
    return [dict(row) for row in result.mappings()]


async def find_tombstone_match(
    session: AsyncSession,
    *,
    user_id: UUID,
    normalized_topic_group: str,
    normalized_topic_title: str,
    normalized_aliases: Sequence[str],
) -> dict[str, Any] | None:
    """按冻结 tombstone 规则返回 exact/high/ambiguous 的最高优先级匹配。"""
    candidate_terms = sorted({normalized_topic_title, *normalized_aliases})
    result = await session.execute(
        text(
            """
            WITH scored AS (
                SELECT t.*,
                       CASE
                         WHEN t.normalized_topic_group = :group_name
                          AND t.normalized_topic_title = :title_name THEN 2
                         WHEN t.normalized_topic_title = ANY(:candidate_terms)
                           OR t.normalized_aliases && :candidate_terms THEN 1
                         ELSE 0
                       END AS exact_kind,
                       GREATEST(
                         similarity(t.normalized_topic_title, :title_name),
                         COALESCE((
                           SELECT MAX(similarity(alias_value, :title_name))
                           FROM unnest(t.normalized_aliases) AS alias_value
                         ), 0)
                       ) AS title_score,
                       similarity(t.normalized_topic_group, :group_name) AS group_score
                FROM conversation.knowledge_summary_tombstones t
                WHERE t.user_id = :user_id
            ), ranked AS (
                SELECT *, (0.85 * title_score + 0.15 * group_score) AS final_score
                FROM scored
            )
            SELECT *,
                   CASE
                     WHEN exact_kind > 0 THEN 'exact'
                     WHEN final_score >= 0.90 THEN 'high'
                     ELSE 'ambiguous'
                   END AS match_kind
            FROM ranked
            WHERE exact_kind > 0 OR final_score >= 0.60
            ORDER BY exact_kind DESC, final_score DESC, deleted_at DESC, tombstone_id DESC
            LIMIT 1
            """
        ),
        {
            "user_id": user_id,
            "group_name": normalized_topic_group,
            "title_name": normalized_topic_title,
            "candidate_terms": candidate_terms,
        },
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def insert_or_get_source_ids(
    session: AsyncSession,
    *,
    summary_id: UUID,
    user_id: UUID,
    generation_id: UUID,
    trigger: str,
    source_checkpoint_id: str,
    source_rows: Sequence[dict[str, Any]],
) -> dict[UUID, UUID]:
    """幂等写入消息级 source，并返回 message_id → source_id 映射。"""
    for row in source_rows:
        await session.execute(
            text(
                """
                INSERT INTO conversation.knowledge_summary_sources (
                    source_id, summary_id, user_id, thread_id, turn_id, message_id,
                    message_role, source_checkpoint_id, first_generation_id, first_trigger,
                    status, message_occurred_at, message_sequence
                ) VALUES (
                    :source_id, :summary_id, :user_id, :thread_id, :turn_id, :message_id,
                    :message_role, :source_checkpoint_id, :generation_id, :trigger,
                    'available', :message_occurred_at, :message_sequence
                )
                ON CONFLICT (summary_id, message_id) DO NOTHING
                """
            ),
            {
                "source_id": uuid4(),
                "summary_id": summary_id,
                "user_id": user_id,
                "thread_id": row["thread_id"],
                "turn_id": row["turn_id"],
                "message_id": row["message_id"],
                "message_role": row["role"],
                "source_checkpoint_id": source_checkpoint_id,
                "generation_id": generation_id,
                "trigger": trigger,
                "message_occurred_at": row["occurred_at"],
                "message_sequence": row["sequence"],
            },
        )
    result = await session.execute(
        text(
            """
            SELECT source_id, message_id
            FROM conversation.knowledge_summary_sources
            WHERE summary_id = :summary_id AND message_id = ANY(:message_ids)
            """
        ),
        {"summary_id": summary_id, "message_ids": [row["message_id"] for row in source_rows]},
    )
    return {row["message_id"]: row["source_id"] for row in result.mappings()}


async def create_summary_snapshot(
    session: AsyncSession,
    *,
    summary_id: UUID,
    user_id: UUID,
    topic_group_title: str,
    topic_title: str,
    normalized_topic_group: str,
    normalized_topic_title: str,
    content: dict[str, Any],
    search_text: str,
    protected_sections: list[str],
    content_hash: str,
    state_hash: str,
    review_state: str,
    generation_id: UUID,
    source_ids: Sequence[UUID],
) -> None:
    """创建一张 AI 总结快照；计数由消息级 source 事实计算，不由调用方猜测。"""
    await session.execute(
        text(
            """
            INSERT INTO conversation.knowledge_summaries (
                summary_id, user_id, topic_group_title, topic_title,
                normalized_topic_group, normalized_topic_title, status, review_state,
                content_schema_version, content, search_text, protected_sections, version,
                source_count, available_source_count, source_message_count,
                content_hash, state_hash, last_generation_id, last_generated_at
            ) VALUES (
                :summary_id, :user_id, :topic_group_title, :topic_title,
                :normalized_topic_group, :normalized_topic_title, 'active', :review_state,
                1, CAST(:content AS jsonb), :search_text, :protected_sections, 1,
                0, 0, 0, :content_hash, :state_hash, :generation_id, now()
            )
            """
        ),
        {
            "summary_id": summary_id,
            "user_id": user_id,
            "topic_group_title": topic_group_title,
            "topic_title": topic_title,
            "normalized_topic_group": normalized_topic_group,
            "normalized_topic_title": normalized_topic_title,
            "review_state": review_state,
            "content": json.dumps(content, ensure_ascii=False),
            "search_text": search_text,
            "protected_sections": protected_sections,
            "content_hash": content_hash,
            "state_hash": state_hash,
            "generation_id": generation_id,
        },
    )
    await _recalculate_source_counts_for_one(session, summary_id=summary_id)


async def update_generation_summary_snapshot(
    session: AsyncSession,
    *,
    summary_id: UUID,
    user_id: UUID,
    content: dict[str, Any],
    search_text: str,
    protected_sections: list[str],
    version: int,
    content_hash: str,
    state_hash: str,
    review_state: str,
    generation_id: UUID,
) -> None:
    """更新自动合并快照，并保留当前 generation 投影供详情审计。"""
    await session.execute(
        text(
            """
            UPDATE conversation.knowledge_summaries
            SET content = CAST(:content AS jsonb), search_text = :search_text,
                protected_sections = :protected_sections, version = :version,
                content_hash = :content_hash, state_hash = :state_hash,
                review_state = :review_state, last_generation_id = :generation_id,
                last_generated_at = now(), updated_at = now()
            WHERE summary_id = :summary_id AND user_id = :user_id AND status = 'active'
            """
        ),
        {
            "summary_id": summary_id,
            "user_id": user_id,
            "content": json.dumps(content, ensure_ascii=False),
            "search_text": search_text,
            "protected_sections": protected_sections,
            "version": version,
            "content_hash": content_hash,
            "state_hash": state_hash,
            "review_state": review_state,
            "generation_id": generation_id,
        },
    )
    await _recalculate_source_counts_for_one(session, summary_id=summary_id)


async def lock_summary_rows(
    session: AsyncSession, *, user_id: UUID, summary_ids: Sequence[UUID]
) -> list[dict[str, Any]]:
    """按 UUID 字符串升序锁定目标总结，遵守 §13.2 行锁顺序。"""
    locked: list[dict[str, Any]] = []
    for summary_id in sorted(set(summary_ids), key=str):
        result = await session.execute(
            text(
                """
                SELECT * FROM conversation.knowledge_summaries
                WHERE summary_id = :summary_id AND user_id = :user_id AND status = 'active'
                FOR UPDATE
                """
            ),
            {"summary_id": summary_id, "user_id": user_id},
        )
        row = result.mappings().first()
        if row is not None:
            locked.append(dict(row))
    return locked


async def insert_generation_revision(
    session: AsyncSession,
    *,
    revision_id: UUID,
    summary_id: UUID,
    user_id: UUID,
    version: int,
    base_version: int,
    mutation_type: str,
    topic_group_title: str,
    topic_title: str,
    content: dict[str, Any],
    protected_sections: list[str],
    content_hash: str,
    changed_sections: list[str],
    source_ids: Sequence[UUID],
    generation_id: UUID,
) -> None:
    """写入带 generation/source 审计引用的不可变自动 Revision。"""
    await session.execute(
        text(
            """
            INSERT INTO conversation.knowledge_summary_revisions (
                revision_id, summary_id, user_id, version, base_version, mutation_type,
                actor_type, topic_group_title, topic_title, content, protected_sections,
                content_hash, changed_sections, source_ids, generation_id
            ) VALUES (
                :revision_id, :summary_id, :user_id, :version, :base_version, :mutation_type,
                'model', :topic_group_title, :topic_title, CAST(:content AS jsonb),
                :protected_sections, :content_hash, :changed_sections, :source_ids, :generation_id
            )
            """
        ),
        {
            "revision_id": revision_id,
            "summary_id": summary_id,
            "user_id": user_id,
            "version": version,
            "base_version": base_version,
            "mutation_type": mutation_type,
            "topic_group_title": topic_group_title,
            "topic_title": topic_title,
            "content": json.dumps(content, ensure_ascii=False),
            "protected_sections": protected_sections,
            "content_hash": content_hash,
            "changed_sections": changed_sections,
            "source_ids": list(source_ids),
            "generation_id": generation_id,
        },
    )


async def upsert_generation_alias(
    session: AsyncSession,
    *,
    alias_id: UUID,
    summary_id: UUID,
    user_id: UUID,
    normalized_topic_group: str,
    display_alias: str,
    normalized_alias: str,
    created_by: str = "model",
) -> None:
    """保留模型识别到的历史标题 alias，不覆盖既有 alias。"""
    await session.execute(
        text(
            """
            INSERT INTO conversation.knowledge_summary_aliases (
                alias_id, summary_id, user_id, normalized_topic_group,
                display_alias, normalized_alias, created_by
            ) VALUES (
                :alias_id, :summary_id, :user_id, :normalized_topic_group,
                :display_alias, :normalized_alias, :created_by
            )
            ON CONFLICT (summary_id, normalized_topic_group, normalized_alias) DO NOTHING
            """
        ),
        {
            "alias_id": alias_id,
            "summary_id": summary_id,
            "user_id": user_id,
            "normalized_topic_group": normalized_topic_group,
            "display_alias": display_alias,
            "normalized_alias": normalized_alias,
            "created_by": created_by,
        },
    )


async def insert_duplicate_candidate(
    session: AsyncSession,
    *,
    duplicate_id: UUID,
    generation_id: UUID,
    summary_id: UUID,
    possible_target_summary_id: UUID,
    user_id: UUID,
    match_score: float,
) -> dict[str, Any]:
    """按业务方向创建或复用重复关系，并仅更新最新生成证据字段。"""
    if summary_id == possible_target_summary_id:
        raise ValueError("可能重复关系不允许两端相同")
    await session.execute(
        text(
            """
            INSERT INTO conversation.knowledge_summary_duplicate_candidates (
                duplicate_id, generation_id, summary_id, possible_target_summary_id,
                user_id, match_score
            ) VALUES (
                :duplicate_id, :generation_id, :summary_id, :possible_target_summary_id,
                :user_id, :match_score
            )
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "duplicate_id": duplicate_id,
            "generation_id": generation_id,
            "summary_id": summary_id,
            "possible_target_summary_id": possible_target_summary_id,
            "user_id": user_id,
            "match_score": match_score,
        },
    )
    await session.execute(
        text(
            """
            UPDATE conversation.knowledge_summary_duplicate_candidates
            SET generation_id = :generation_id, match_score = :match_score, updated_at = now()
            WHERE user_id = :user_id
              AND LEAST(summary_id, possible_target_summary_id)
                    = LEAST(:summary_id, :possible_target_summary_id)
              AND GREATEST(summary_id, possible_target_summary_id)
                    = GREATEST(:summary_id, :possible_target_summary_id)
            """
        ),
        {
            "generation_id": generation_id,
            "match_score": match_score,
            "user_id": user_id,
            "summary_id": summary_id,
            "possible_target_summary_id": possible_target_summary_id,
        },
    )
    result = await session.execute(
        text(
            """
            SELECT *
            FROM conversation.knowledge_summary_duplicate_candidates
            WHERE user_id = :user_id
              AND LEAST(summary_id, possible_target_summary_id)
                    = LEAST(:summary_id, :possible_target_summary_id)
              AND GREATEST(summary_id, possible_target_summary_id)
                    = GREATEST(:summary_id, :possible_target_summary_id)
            """
        ),
        {
            "user_id": user_id,
            "summary_id": summary_id,
            "possible_target_summary_id": possible_target_summary_id,
        },
    )
    row = result.mappings().one()
    return dict(row)


async def list_pending_duplicate_counterpart_ids(
    session: AsyncSession, *, user_id: UUID, summary_id: UUID
) -> list[UUID]:
    """读取仍 pending 的重复关系对端，供删除事务按行锁刷新其快照。"""
    result = await session.execute(
        text(
            """
            SELECT CASE
                     WHEN summary_id = :summary_id THEN possible_target_summary_id
                     ELSE summary_id
                   END AS counterpart_summary_id
            FROM conversation.knowledge_summary_duplicate_candidates
            WHERE user_id = :user_id
              AND status = 'pending'
              AND (summary_id = :summary_id OR possible_target_summary_id = :summary_id)
            ORDER BY counterpart_summary_id ASC
            """
        ),
        {"user_id": user_id, "summary_id": summary_id},
    )
    return [UUID(str(row["counterpart_summary_id"])) for row in result.mappings()]


async def compute_effective_review_state(
    session: AsyncSession, *, user_id: UUID, summary_id: UUID
) -> str:
    """从 pending review/duplicate 事实计算当前应持久化的 review_state。"""
    result = await session.execute(
        text(
            """
            SELECT CASE
              WHEN EXISTS (
                SELECT 1 FROM conversation.knowledge_summary_reviews r
                WHERE r.summary_id = :summary_id AND r.user_id = :user_id AND r.status = 'pending'
              ) THEN 'conflict'
              WHEN EXISTS (
                SELECT 1 FROM conversation.knowledge_summary_duplicate_candidates d
                WHERE d.user_id = :user_id AND d.status = 'pending'
                  AND (d.summary_id = :summary_id OR d.possible_target_summary_id = :summary_id)
              ) THEN 'possible_duplicate'
              ELSE 'clean'
            END AS review_state
            """
        ),
        {"user_id": user_id, "summary_id": summary_id},
    )
    return str(result.scalar_one())


async def insert_review_and_mark_conflict(
    session: AsyncSession,
    *,
    review_id: UUID,
    generation_id: UUID,
    summary: dict[str, Any],
    candidate_index: int,
    reason_code: str,
    internal_reason: str,
    proposed_content: dict[str, Any],
    generation_id_for_revision: UUID,
) -> None:
    """写入结构化 review，并以 Revision 记录 review_state 变化。"""
    current_content = KnowledgeSummaryContent.model_validate(summary["content"])
    next_version = int(summary["version"]) + 1
    next_state_hash = state_hash_v1(
        topic_group_title=summary["topic_group_title"],
        topic_title=summary["topic_title"],
        content_hash=str(summary["content_hash"]),
        protected_sections=summary["protected_sections"],
        review_state="conflict",
    )
    await session.execute(
        text(
            """
            INSERT INTO conversation.knowledge_summary_reviews (
                review_id, generation_id, summary_id, user_id, candidate_index,
                reason_code, internal_reason, proposed_content
            ) VALUES (
                :review_id, :generation_id, :summary_id, :user_id, :candidate_index,
                :reason_code, :internal_reason, CAST(:proposed_content AS jsonb)
            )
            ON CONFLICT (generation_id, candidate_index, summary_id, reason_code) DO NOTHING
            """
        ),
        {
            "review_id": review_id,
            "generation_id": generation_id,
            "summary_id": summary["summary_id"],
            "user_id": summary["user_id"],
            "candidate_index": candidate_index,
            "reason_code": reason_code,
            "internal_reason": internal_reason,
            "proposed_content": json.dumps(proposed_content, ensure_ascii=False),
        },
    )
    await update_generation_summary_snapshot(
        session,
        summary_id=summary["summary_id"],
        user_id=summary["user_id"],
        content=current_content.model_dump(mode="json"),
        search_text=summary["search_text"],
        protected_sections=list(summary["protected_sections"]),
        version=next_version,
        content_hash=str(summary["content_hash"]),
        state_hash=next_state_hash,
        review_state="conflict",
        generation_id=generation_id_for_revision,
    )
    await insert_generation_revision(
        session,
        revision_id=uuid4(),
        summary_id=summary["summary_id"],
        user_id=summary["user_id"],
        version=next_version,
        base_version=int(summary["version"]),
        mutation_type="review_flagged",
        topic_group_title=summary["topic_group_title"],
        topic_title=summary["topic_title"],
        content=current_content.model_dump(mode="json"),
        protected_sections=list(summary["protected_sections"]),
        content_hash=str(summary["content_hash"]),
        changed_sections=[],
        source_ids=[],
        generation_id=generation_id_for_revision,
    )


async def _recalculate_source_counts_for_one(session: AsyncSession, *, summary_id: UUID) -> None:
    """根据消息级事实重算一张总结的来源计数。"""
    await session.execute(
        text(
            """
            UPDATE conversation.knowledge_summaries s
            SET source_count = counts.source_count,
                available_source_count = counts.available_source_count,
                source_message_count = counts.source_message_count,
                updated_at = now()
            FROM (
                SELECT COUNT(DISTINCT turn_id)::integer AS source_count,
                       COUNT(DISTINCT turn_id) FILTER (WHERE status = 'available')::integer
                           AS available_source_count,
                       COUNT(*)::integer AS source_message_count
                FROM conversation.knowledge_summary_sources
                WHERE summary_id = :summary_id
            ) counts
            WHERE s.summary_id = :summary_id
            """
        ),
        {"summary_id": summary_id},
    )


async def insert_source_rows_with_ids(
    session: AsyncSession,
    *,
    summary_id: UUID,
    user_id: UUID,
    generation_id: UUID,
    trigger: str,
    source_checkpoint_id: str,
    source_rows: Sequence[dict[str, Any]],
    source_ids_by_message: dict[UUID, UUID],
) -> dict[UUID, UUID]:
    """使用调用方已分配的 source_id 写入来源，便于新总结 content 同步引用 ID。"""
    for row in source_rows:
        await session.execute(
            text(
                """
                INSERT INTO conversation.knowledge_summary_sources (
                    source_id, summary_id, user_id, thread_id, turn_id, message_id,
                    message_role, source_checkpoint_id, first_generation_id, first_trigger,
                    status, message_occurred_at, message_sequence
                ) VALUES (
                    :source_id, :summary_id, :user_id, :thread_id, :turn_id, :message_id,
                    :message_role, :source_checkpoint_id, :generation_id, :trigger,
                    'available', :message_occurred_at, :message_sequence
                )
                ON CONFLICT (summary_id, message_id) DO NOTHING
                """
            ),
            {
                "source_id": source_ids_by_message[row["message_id"]],
                "summary_id": summary_id,
                "user_id": user_id,
                "thread_id": row["thread_id"],
                "turn_id": row["turn_id"],
                "message_id": row["message_id"],
                "message_role": row["role"],
                "source_checkpoint_id": source_checkpoint_id,
                "generation_id": generation_id,
                "trigger": trigger,
                "message_occurred_at": row["occurred_at"],
                "message_sequence": row["sequence"],
            },
        )
    return source_ids_by_message


async def get_source_ids_by_messages(
    session: AsyncSession, *, summary_id: UUID, message_ids: Sequence[UUID]
) -> dict[UUID, UUID]:
    """读取一张总结已有的消息级 source ID。"""
    if not message_ids:
        return {}
    result = await session.execute(
        text(
            """
            SELECT message_id, source_id
            FROM conversation.knowledge_summary_sources
            WHERE summary_id = :summary_id AND message_id = ANY(:message_ids)
            """
        ),
        {"summary_id": summary_id, "message_ids": list(message_ids)},
    )
    return {row["message_id"]: row["source_id"] for row in result.mappings()}


async def get_source_sort_keys(
    session: AsyncSession, *, summary_id: UUID, source_ids: Sequence[UUID | str]
) -> dict[UUID, tuple[datetime, int, UUID]]:
    """读取来源时间排序键，供合并时裁剪每条目的最新 100 条来源。"""
    normalized_ids = [UUID(str(source_id)) for source_id in source_ids]
    if not normalized_ids:
        return {}
    result = await session.execute(
        text(
            """
            SELECT source_id, message_occurred_at, message_sequence
            FROM conversation.knowledge_summary_sources
            WHERE summary_id = :summary_id AND source_id = ANY(:source_ids)
            """
        ),
        {"summary_id": summary_id, "source_ids": normalized_ids},
    )
    return {
        UUID(str(row["source_id"])): (
            row["message_occurred_at"],
            int(row["message_sequence"]),
            UUID(str(row["source_id"])),
        )
        for row in result.mappings()
    }
