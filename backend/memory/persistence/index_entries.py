"""memory_index_entries 检索仓储（规格 §12.1 / §12.2 / §13.5）。

只负责 pg_trgm 候选召回；§12.2 综合排序分在 services/search_service.py
中以确定性 Python 计算，便于单元测试。已删除记忆在 forget 同事务删除索引行，
隔离（quarantine）版本不进入索引表，因此候选查询无需再过滤。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def escape_like(value: str) -> str:
    """LIKE 模式转义（配合 ESCAPE '\\'）。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_candidates(
    session: AsyncSession,
    *,
    user_id: UUID,
    query: str,
    topic_keys: list[str],
    memory_types: list[str],
    min_similarity: float,
    limit: int,
) -> list[dict[str, Any]]:
    """候选召回：similarity 达阈值，或精确 topic_key/标题，或标题前缀命中。

    精确/前缀命中允许低于阈值（§12.1：精确 topic_key/topic_title 匹配优先），
    是否入候选的最终判定由服务层按 §12.2 再执行。
    """
    result = await session.execute(
        text(
            r"""
            SELECT memory_id, memory_type, topic_key, title, summary, keywords,
                   evidence_refs, confidence, source_version, updated_at,
                   similarity(search_text, :query) AS similarity
            FROM memory_index_entries
            WHERE user_id = :user_id
              AND (cardinality(CAST(:topic_keys AS text[])) = 0
                   OR topic_key = ANY(CAST(:topic_keys AS text[])))
              AND (cardinality(CAST(:memory_types AS text[])) = 0
                   OR memory_type = ANY(CAST(:memory_types AS text[])))
              AND (
                    similarity(search_text, :query) >= :min_similarity
                    OR topic_key = :query
                    OR title = :query
                    OR title LIKE :title_prefix ESCAPE '\'
                  )
            ORDER BY similarity DESC, updated_at DESC, memory_id ASC
            LIMIT :limit
            """
        ),
        {
            "user_id": user_id,
            "query": query,
            "topic_keys": topic_keys,
            "memory_types": memory_types,
            "min_similarity": min_similarity,
            "title_prefix": escape_like(query) + "%",
            "limit": limit,
        },
    )
    return [dict(r) for r in result.mappings().all()]
