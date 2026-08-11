"""总结记忆检索服务（规格 §12.1–§12.3 / §13.5）。

- 中文策略：查询与标题比较前做 NFKC 规范化 + 空白折叠（§12.1）；
  不使用英文分词冒充中文检索。
- 排序：§12.2 确定性加分公式（精确 topic_key +100 / 精确规范化标题 +90 /
  标题前缀 +70 / trigram similarity × 60 / 明确 topic filter +40 /
  最近 30 天更新 +0～10），相似度阈值 0.20 才入候选。
- 综合排序分只服务 search_summary；自动 merge 判断不走本模块（§12.2 末段）。
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.errors import InvalidPayloadError
from backend.memory.contracts.results import MemorySearchHit, MemorySearchRequest
from backend.memory.persistence import index_entries as index_repo
from backend.settings import Settings

#: §12.2 排序常量
SIMILARITY_THRESHOLD = 0.20
SCORE_EXACT_TOPIC_KEY = 100.0
SCORE_EXACT_TITLE = 90.0
SCORE_PREFIX_TITLE = 70.0
SCORE_SIMILARITY_WEIGHT = 60.0
SCORE_TOPIC_FILTER = 40.0
SCORE_RECENCY_MAX = 10.0
RECENCY_WINDOW_DAYS = 30

#: 单次召回的候选上限（综合排序与分页在内存中完成，图谱规模下有界）
MAX_CANDIDATES = 200


def normalize_search_query(query: str) -> str:
    """NFKC 规范化 + 空白折叠（§12.1 中文策略）。"""
    return " ".join(unicodedata.normalize("NFKC", query).split())


def recency_score(updated_at: datetime, now: datetime) -> float:
    """最近 30 天更新 +0～10：按年龄线性衰减（§12.2）。"""
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - updated_at).total_seconds() / 86400)
    if age_days >= RECENCY_WINDOW_DAYS:
        return 0.0
    return SCORE_RECENCY_MAX * (1 - age_days / RECENCY_WINDOW_DAYS)


def is_candidate(*, topic_key: str | None, title: str, similarity: float, query: str) -> bool:
    """§12.2 候选判定：similarity 达阈值，或精确/前缀命中（§12.1 精确匹配优先）。"""
    normalized_title = normalize_search_query(title)
    return (
        similarity >= SIMILARITY_THRESHOLD
        or (topic_key is not None and topic_key == query)
        or normalized_title == query
        or normalized_title.startswith(query)
    )


def score_candidate(
    *,
    topic_key: str | None,
    title: str,
    similarity: float,
    query: str,
    topic_filter: bool,
    updated_at: datetime,
    now: datetime,
) -> float:
    """§12.2 综合排序分。精确标题命中时不再重复计前缀分。"""
    normalized_title = normalize_search_query(title)
    score = 0.0
    if topic_key is not None and topic_key == query:
        score += SCORE_EXACT_TOPIC_KEY
    if normalized_title == query:
        score += SCORE_EXACT_TITLE
    elif normalized_title.startswith(query):
        score += SCORE_PREFIX_TITLE
    score += similarity * SCORE_SIMILARITY_WEIGHT
    if topic_filter:
        score += SCORE_TOPIC_FILTER
    score += recency_score(updated_at, now)
    return score


def matched_excerpt(summary: str, query: str, *, window: int = 40) -> str | None:
    """确定性摘录：summary 中首个命中位置前后 window 字符；未命中返回 None。"""
    index = summary.find(query)
    if index < 0:
        return None
    start = max(0, index - window)
    end = min(len(summary), index + len(query) + window)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(summary) else ""
    return f"{prefix}{summary[start:end]}{suffix}"


def _sort_key_tuple(score: float, updated_at: datetime, memory_id: str) -> tuple[float, float, str]:
    """排序键：score 降序、updated_at 降序、memory_id 升序（全部转为升序比较）。"""
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return (-score, -updated_at.timestamp(), memory_id)


class SearchService:
    """search_summary 用检索服务（§12）；依赖注入 settings/session_factory。"""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory

    async def search(
        self,
        *,
        user_id: UUID,
        request: MemorySearchRequest,
        cursor_sort_key: list[Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[list[MemorySearchHit], list[Any] | None, bool]:
        """返回 (hits, next_sort_key, has_more)；next_sort_key 供 §19.9 cursor 使用。

        排序分含时间衰减项，为保证翻页期间排序稳定，评分时刻嵌入
        sort_key 第 4 个元素，后续页沿用同一时刻评分。
        """
        if cursor_sort_key is not None and len(cursor_sort_key) == 4:
            now = datetime.fromtimestamp(float(cursor_sort_key[3]), UTC)
        now = now or datetime.now(UTC)
        query = normalize_search_query(request.query)
        if not query:
            raise InvalidPayloadError("query 规范化后为空", field="query")
        topic_filter = bool(request.topic_keys)
        async with self._session_factory() as session:
            rows = await index_repo.search_candidates(
                session,
                user_id=user_id,
                query=query,
                topic_keys=sorted(request.topic_keys),
                memory_types=sorted(request.memory_types),
                min_similarity=SIMILARITY_THRESHOLD,
                limit=MAX_CANDIDATES,
            )
        ranked: list[tuple[tuple[float, float, str], float, dict[str, Any]]] = []
        for row in rows:
            similarity = float(row["similarity"])
            title = str(row["title"])
            topic_key = row.get("topic_key")
            if not is_candidate(
                topic_key=topic_key, title=title, similarity=similarity, query=query
            ):
                continue
            score = score_candidate(
                topic_key=topic_key,
                title=title,
                similarity=similarity,
                query=query,
                topic_filter=topic_filter,
                updated_at=row["updated_at"],
                now=now,
            )
            ranked.append(
                (_sort_key_tuple(score, row["updated_at"], str(row["memory_id"])), score, row)
            )
        ranked.sort(key=lambda item: item[0])
        if cursor_sort_key is not None:
            boundary = (
                -float(cursor_sort_key[0]),
                -datetime.fromisoformat(str(cursor_sort_key[1])).timestamp(),
                str(cursor_sort_key[2]),
            )
            ranked = [item for item in ranked if item[0] > boundary]
        page = ranked[: request.limit]
        has_more = len(ranked) > request.limit
        hits = [
            MemorySearchHit(
                memory_id=str(row["memory_id"]),
                memory_type=row["memory_type"],
                topic_key=row.get("topic_key"),
                title=str(row["title"]),
                summary=str(row["summary"]),
                matched_excerpt=matched_excerpt(str(row["summary"]), query),
                evidence_refs=[str(r) for r in (row.get("evidence_refs") or [])][:100],
                version=int(row["source_version"]),
                updated_at=row["updated_at"],
                confidence=(
                    float(row["confidence"]) if row.get("confidence") is not None else None
                ),
                score=score,
            )
            for _key, score, row in page
        ]
        next_sort_key: list[Any] | None = None
        if has_more and page:
            _key, last_score, last_row = page[-1]
            # 第 4 个元素固定评分时刻，后续页沿用同一时刻，保证翻页期间排序稳定
            next_sort_key = [
                last_score,
                last_row["updated_at"].isoformat(),
                str(last_row["memory_id"]),
                now.timestamp(),
            ]
        return hits, next_sort_key, has_more
