"""记忆工具服务端实现（memory-rebuild §2.4 D1/D3 / §5.7 Phase 5）。

三个**只读**工具的落点，供对话域 Agent 在 answer 路径上按需调用：

- ``memory.search``：只对 index 注册表的 ``title``(name) / ``summary``(description) /
  ``aliases`` / ``keywords`` 四列做大小写不敏感子串匹配（ILIKE），**不碰向量、
  不碰正文**；排序固定为"命中列权重降序 → updated_at 降序 → memory_id 升序"，
  不引入模型打分，同一份数据任何时刻返回同一顺序（可预测、可断言）。
- ``memory.read``：按行分段返回活动版本正文，复用 MemoryService 的版本化读取与
  删除抑制（文档不存在/已删除/quarantine 一律映射 MEMORY_NOT_FOUND），
  出参带 version + checksum 供回答引用回查。
- ``memory.prime``：首轮注入用的每用户 ``memory_summary.md`` 摘要 + 注册表目录；
  summary 缺失/损坏/超限时返回空摘要 + ``degraded=true`` 并记录
  ``memory_prime_degraded``，**不报错**（§5.7）。

用户隔离：所有 SQL 都带 ``user_id`` 过滤，user_id 只来自认证上下文，
请求体不接受该字段（``extra="forbid"``）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.errors import MemoryNotFoundError
from backend.memory.contracts.results import (
    MemoryToolPrimeIndexEntry,
    MemoryToolPrimeResponse,
    MemoryToolReadRequest,
    MemoryToolReadResponse,
    MemoryToolSearchItem,
    MemoryToolSearchRequest,
    MemoryToolSearchResponse,
)
from backend.memory.persistence.index_entries import escape_like
from backend.memory.services.memory_service import MemoryService

logger = logging.getLogger("memory.tools")

#: ``memory_summary.md`` 文件名（§2.3 目标文件框架：位于用户目录根，不在 current/ 下）
PRIME_SUMMARY_FILENAME = "memory_summary.md"

#: §2.3：summary 首行必须恰好是 v1（无 front matter）；不符即视为损坏
PRIME_SUMMARY_SCHEMA_VERSION = "v1"

#: prime 的**独立小预算**（§2.4 D1「截断到独立小预算」）：正常 summary 只有 ≤350 词
#: 用户画像 + 稳定偏好 bullet + 主题路由，量级在千字以内；4000 字符是防御性上限，
#: 只截断 summary 正文并置 ``summary_truncated=true``，与 ``memory_context_token_budget``
#: 那套单文档预算无关（§2.4 D3：read 沿用旧预算族，prime 另设小预算）。
PRIME_SUMMARY_MAX_CHARS = 4000

#: 读取 summary 文件的硬上限：超过即按"超限"降级（§5.7），不做部分解析——
#: 只读 MAX+1 字节即可判定，绝不把超大文件整份读进内存。
PRIME_SUMMARY_MAX_BYTES = 1024 * 1024

#: prime 注册表目录的条数上限（review I-5）：`description` 单字段上限 2000 字符，
#: 主题很多的用户会让整个响应体（进而让 conversation 侧的快照/checkpoint）无界增长。
#: 这里给服务端一个确定性的**有界返回**：按 `memory_id` 升序取前 N 条，超限时置
#: `index_entries_truncated=true`（可判定信号，见 `MemoryToolPrimeResponse`），
#: 让上层知道"目录不是全量、需要走 memory.search 定位"。
#: 200 是防御性上限（正常用户的主题目录量级在几十条以内），不是产品配额；
#: 注入提示词的最终体积另由 conversation 侧的 token 预算裁剪兜底。
PRIME_INDEX_ENTRIES_MAX = 200


# ---------------------------------------------------------------------------
# search：SQL 构造（纯函数）与执行
# ---------------------------------------------------------------------------


def normalize_tool_queries(queries: list[str]) -> list[str]:
    """工具 query 规范化：去首尾空白、丢弃空串、按首次出现去重。

    只做空白处理，**不做 NFKC 折叠/分词**（§2.4 D3 要求"纯关键词、可预测"）：
    匹配是与 index 列原文逐字比较的子串关系，额外折叠会让"看起来一样"的
    查询匹配不上原始文本。
    """
    seen: set[str] = set()
    normalized: list[str] = []
    for query in queries:
        cleaned = query.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)
    return normalized


def _join_conditions(conditions: list[str], *, connective: str, indent: int) -> str:
    """用 connective 连接多条（可能多行）条件，续行缩进到同一列，便于阅读与 diff。"""
    pad = " " * indent
    blocks = [condition.replace("\n", "\n" + pad) for condition in conditions]
    return f"\n{pad}{connective} ".join(blocks)


def _name_hit(param: str) -> str:
    """name(title)/aliases 命中：数组列按元素匹配，任一元素包含 query 即命中该列。"""
    return (
        f"(title ILIKE {param} ESCAPE '\\' OR EXISTS (\n"
        f"SELECT 1 FROM unnest(aliases) AS al WHERE al ILIKE {param} ESCAPE '\\'))"
    )


def _keyword_hit(param: str) -> str:
    """keywords 命中（判别性检索词）。"""
    return f"EXISTS (SELECT 1 FROM unnest(keywords) AS kw WHERE kw ILIKE {param} ESCAPE '\\')"


def _row_hit(param: str) -> str:
    """单条 query 的候选条件：四列任一命中即入选（§2.4 D3① 匹配域）。"""
    return (
        f"(title ILIKE {param} ESCAPE '\\' OR summary ILIKE {param} ESCAPE '\\'\n"
        f"OR {_keyword_hit(param)}\n"
        f"OR EXISTS (SELECT 1 FROM unnest(aliases) AS al WHERE al ILIKE {param} ESCAPE '\\'))"
    )


def build_search_sql(
    *,
    user_id: UUID,
    queries: list[str],
    match_mode: str,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """构造 search 的 SQL 与绑定参数（纯函数，单测可直接断言文本与参数）。

    排序即 §2.4 D3① 的固定规则，由数据库完成，因此 ``LIMIT :limit`` 取到的
    一定是全序意义上的前 N 条；服务层再用同一规则做一次防御性重排（两者等价）。
    """
    params: dict[str, Any] = {"user_id": user_id, "limit": limit}
    row_hits: list[str] = []
    name_hits: list[str] = []
    keyword_hits: list[str] = []
    for index, query in enumerate(queries):
        param = f":q{index}"
        # escape_like 转义 \ % _ 三个元字符，配合 ESCAPE '\' 让子串匹配保持字面语义
        params[f"q{index}"] = f"%{escape_like(query)}%"
        row_hits.append(_row_hit(param))
        name_hits.append(_name_hit(param))
        keyword_hits.append(_keyword_hit(param))
    connective = "OR" if match_mode == "any" else "AND"
    sql = (
        "SELECT memory_id, title, summary, keywords, aliases, source_version, updated_at\n"
        "FROM memory_index_entries\n"
        "WHERE user_id = :user_id\n"
        "  AND (\n"
        f"      {_join_conditions(row_hits, connective=connective, indent=6)}\n"
        "  )\n"
        "ORDER BY\n"
        "  CASE\n"
        "    WHEN (\n"
        f"        {_join_conditions(name_hits, connective='OR', indent=8)}\n"
        "    ) THEN 2\n"
        "    WHEN (\n"
        f"        {_join_conditions(keyword_hits, connective='OR', indent=8)}\n"
        "    ) THEN 1\n"
        "    ELSE 0\n"
        "  END DESC,\n"
        "  updated_at DESC,\n"
        "  memory_id ASC\n"
        "LIMIT :limit"
    )
    return sql, params


async def fetch_search_rows(
    session: AsyncSession,
    *,
    user_id: UUID,
    queries: list[str],
    match_mode: str,
    limit: int,
) -> list[dict[str, Any]]:
    """执行 search SQL（模块级函数，单测 monkeypatch 本函数即可不起真实 PG）。"""
    sql, params = build_search_sql(
        user_id=user_id, queries=queries, match_mode=match_mode, limit=limit
    )
    result = await session.execute(text(sql), params)
    return [dict(row) for row in result.mappings().all()]


def _contains(haystack: str, needle: str) -> bool:
    """大小写不敏感子串判定（与 PG ILIKE 在 ASCII/CJK 上同语义）。"""
    return needle.casefold() in haystack.casefold()


def hit_weight(
    *,
    queries: list[str],
    name: str,
    aliases: list[str],
    keywords: list[str],
) -> int:
    """命中列权重（§2.4 D3① 排序第一步）：name/aliases=2 > keywords=1 > description=0。

    只要任一 query 命中更高权重的列就取该权重，与 SQL 的 CASE 完全一致。
    """
    if any(
        _contains(name, query) or any(_contains(alias, query) for alias in aliases)
        for query in queries
    ):
        return 2
    if any(_contains(keyword, query) for keyword in keywords for query in queries):
        return 1
    return 0


def tool_hit_sort_key(row: Mapping[str, Any], queries: list[str]) -> tuple[int, float, str]:
    """固定排序键：权重降序 → updated_at 降序 → memory_id 升序（全序、稳定）。

    与 SQL 的 ``ORDER BY`` 同规则：服务层重排只是一道防御（SQL 已按同样规则
    截断），任何情况下都不会改变结果顺序。
    """
    updated_at = row["updated_at"]
    if not isinstance(updated_at, datetime):  # pragma: no cover - 驱动返回 datetime
        updated_at = datetime.fromisoformat(str(updated_at))
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return (
        -hit_weight(
            queries=queries,
            name=str(row["title"]),
            aliases=[str(a) for a in (row.get("aliases") or [])],
            keywords=[str(k) for k in (row.get("keywords") or [])],
        ),
        -updated_at.timestamp(),
        str(row["memory_id"]),
    )


# ---------------------------------------------------------------------------
# prime：注册表目录投影
# ---------------------------------------------------------------------------


async def fetch_index_projection(
    session: AsyncSession, *, user_id: UUID, limit: int | None = None
) -> list[dict[str, Any]]:
    """prime 的目录投影：只取四列，**不含正文**（§2.4 D1）。

    事实源是实时 index 表（§2.4 D3：注入的目录只是提示，search 才是事实源），
    已删除记忆的索引行在 forget 同事务删除，因此这里无需额外过滤。
    排序固定 memory_id 升序，保证同一份注册表注入顺序稳定。

    ``limit`` 供 prime 做"有界返回"（review I-5）：调用方传 ``上限 + 1`` 就能在
    **不把整份注册表读进内存**的前提下判定是否超限；``None`` 表示不限制（既有语义）。
    """
    params: dict[str, Any] = {"user_id": user_id}
    limit_clause = ""
    if limit is not None:
        params["limit"] = limit
        limit_clause = " LIMIT :limit"
    result = await session.execute(
        text(
            "SELECT memory_id, title, summary, keywords FROM memory_index_entries "
            "WHERE user_id = :user_id ORDER BY memory_id ASC" + limit_clause
        ),
        params,
    )
    return [dict(row) for row in result.mappings().all()]


# ---------------------------------------------------------------------------
# prime：summary 文件读取与降级
# ---------------------------------------------------------------------------


class PrimeSummaryReader(Protocol):
    """``memory_summary.md`` 读取边界（Phase 7 落地写入者后可替换实现）。"""

    async def read(self, *, user_id: UUID) -> tuple[bytes | None, datetime | None]:
        """返回 (文件原始字节, 生成时间元信息)；文件缺失或不可读返回 (None, None)。"""
        ...


def _read_summary_file(root: Path, user_id: UUID) -> tuple[bytes | None, datetime | None]:
    """同步读取 summary 文件（ASYNC240：pathlib/os 调用不放在 async 函数里）。

    路径与 LocalMarkdownStore 的目录布局一致：``users/{shard}/{user_id}/memory_summary.md``。
    故意**不经 MarkdownStore**：summary 目前没有生产者、也没有 memory_documents 行与
    不可变版本（Phase 7 才写），能读到的只有这份平铺文件。
    """
    path = root / "users" / str(user_id)[:2] / str(user_id) / PRIME_SUMMARY_FILENAME
    try:
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            raw = handle.read(PRIME_SUMMARY_MAX_BYTES + 1)
    except OSError:
        # 缺失、权限不足、目录不存在都按"没有 summary"降级，不打断 prime
        return None, None
    return raw, datetime.fromtimestamp(stat.st_mtime, UTC)


class FilePrimeSummaryReader:
    """按 §2.3 文件框架读每用户 ``memory_summary.md`` 的默认实现。

    生成时间元信息取文件 mtime：§2.3 的 v1 格式没有 front matter，文件系统时间
    是当前唯一可得的"生成时间"；Phase 7 写入版本化 summary 后可换更权威的元数据。
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    async def read(self, *, user_id: UUID) -> tuple[bytes | None, datetime | None]:
        return await asyncio.to_thread(_read_summary_file, self._root, user_id)


def parse_prime_summary(
    raw: bytes | None, *, generated_at: datetime | None
) -> tuple[str, str, datetime | None, bool, str | None]:
    """解析 summary 文件，返回 (正文, schema_version, 生成时间, 是否降级, 降级原因)。

    §5.7 明确"summary 缺失、损坏、超限时按现有降级策略返回空 prime/旧摘要"，
    因此这里**任何异常形态都不抛错**：缺失、超硬上限、非 UTF-8、首行不是 v1
    一律返回空正文 + degraded。当前没有 summary 生产者，也就没有"旧摘要"可回退；
    Phase 7 写入版本号之后可在此接入上一版本回退。
    """
    schema_version = PRIME_SUMMARY_SCHEMA_VERSION
    if raw is None:
        return "", schema_version, None, True, "missing"
    if len(raw) > PRIME_SUMMARY_MAX_BYTES:
        return "", schema_version, None, True, "size_exceeded"
    try:
        text_content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "", schema_version, None, True, "not_utf8"
    first_line, separator, rest = text_content.partition("\n")
    # strip 只用于容忍 CRLF 与行尾空白（"v1\r" 仍视为合法标记），标记本身必须恰好是 v1
    if first_line.strip() != PRIME_SUMMARY_SCHEMA_VERSION:
        return "", schema_version, None, True, "schema_mismatch"
    body = rest if separator else ""
    return body.strip("\n"), schema_version, generated_at, False, None


# ---------------------------------------------------------------------------
# read：行切片
# ---------------------------------------------------------------------------


def slice_lines(content: str, *, line_offset: int, max_lines: int) -> tuple[str, int, bool]:
    """按行切片，返回 (片段, 总行数, truncated)。

    行 = ``str.splitlines()`` 的元素（行尾换行不算内容），片段用 "\\n" 重新拼接；
    因此整篇返回时内容与原文只可能相差末尾换行。``truncated`` 表示"后面还有行"。
    """
    lines = content.splitlines()
    total_lines = len(lines)
    chunk = lines[line_offset : line_offset + max_lines]
    truncated = line_offset + len(chunk) < total_lines
    return "\n".join(chunk), total_lines, truncated


# ---------------------------------------------------------------------------
# MemoryToolsService
# ---------------------------------------------------------------------------


class MemoryToolsService:
    """记忆工具服务；只读，依赖注入 session_factory / memory_service / summary_reader。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        memory_service: MemoryService,
        summary_reader: PrimeSummaryReader,
    ) -> None:
        self._session_factory = session_factory
        self._memory_service = memory_service
        self._summary_reader = summary_reader

    async def search(
        self, *, user_id: UUID, request: MemoryToolSearchRequest
    ) -> MemoryToolSearchResponse:
        """纯关键词定位（§2.4 D3①）：返回注册表条目，绝不返回正文。"""
        queries = normalize_tool_queries(request.queries)
        if not queries:
            # 全为空白 query：没有可匹配的关键词，直接空结果（不报错、不打库）
            return MemoryToolSearchResponse(items=[], truncated=False)
        # 多取一条用于判断"还有更多命中"，从而给出 truncated
        async with self._session_factory() as session:
            rows = await fetch_search_rows(
                session,
                user_id=user_id,
                queries=queries,
                match_mode=request.match_mode,
                limit=request.max_results + 1,
            )
        rows.sort(key=lambda row: tool_hit_sort_key(row, queries))
        items = [
            MemoryToolSearchItem(
                memory_id=str(row["memory_id"]),
                name=str(row["title"]),
                description=str(row["summary"]),
                keywords=[str(k) for k in (row.get("keywords") or [])],
                version=int(row["source_version"]),
                updated_at=row["updated_at"],
            )
            for row in rows[: request.max_results]
        ]
        return MemoryToolSearchResponse(items=items, truncated=len(rows) > request.max_results)

    async def read(
        self, *, user_id: UUID, request: MemoryToolReadRequest
    ) -> MemoryToolReadResponse:
        """版本化读取 + 删除抑制（§2.4 D3②）：读不到与不属于本用户同一处理。"""
        loaded = await self._memory_service.read_active_content(
            user_id=user_id, memory_id=request.memory_id
        )
        if loaded is None:
            # 不存在 / 已删除 / quarantine 一律 MEMORY_NOT_FOUND，不泄露跨用户存在性
            raise MemoryNotFoundError("记忆不存在")
        version, checksum, content = loaded
        chunk, total_lines, truncated = slice_lines(
            content, line_offset=request.line_offset, max_lines=request.max_lines
        )
        return MemoryToolReadResponse(
            memory_id=request.memory_id,
            version=version,
            checksum=checksum,
            content=chunk,
            line_offset=request.line_offset,
            total_lines=total_lines,
            truncated=truncated,
        )

    async def prime(self, *, user_id: UUID) -> MemoryToolPrimeResponse:
        """首轮注入输入（§2.4 D1）：summary（可降级为空）+ 注册表目录。

        目录**有界返回**（review I-5）：多取一条判定超限，超限时只返回按 memory_id
        升序的前 :data:`PRIME_INDEX_ENTRIES_MAX` 条，并置 ``index_entries_truncated``
        作为可判定信号——绝不静默丢条目。既有字段的形状与语义都不变。
        """
        raw, generated_at = await self._summary_reader.read(user_id=user_id)
        body, schema_version, stamp, degraded, reason = parse_prime_summary(
            raw, generated_at=generated_at
        )
        if degraded:
            # §5.7 要求记录 memory_prime_degraded；用户 id 不入日志（隐私约定）
            logger.warning("memory_prime_degraded: reason=%s", reason)
        summary_truncated = len(body) > PRIME_SUMMARY_MAX_CHARS
        if summary_truncated:
            body = body[:PRIME_SUMMARY_MAX_CHARS]
        async with self._session_factory() as session:
            rows = await fetch_index_projection(
                session, user_id=user_id, limit=PRIME_INDEX_ENTRIES_MAX + 1
            )
        index_entries_truncated = len(rows) > PRIME_INDEX_ENTRIES_MAX
        if index_entries_truncated:
            logger.warning("memory_prime_index_truncated: kept=%d", PRIME_INDEX_ENTRIES_MAX)
            rows = rows[:PRIME_INDEX_ENTRIES_MAX]
        return MemoryToolPrimeResponse(
            summary=body,
            schema_version=schema_version,
            generated_at=stamp,
            index_entries=[
                MemoryToolPrimeIndexEntry(
                    memory_id=str(row["memory_id"]),
                    name=str(row["title"]),
                    description=str(row["summary"]),
                    keywords=[str(k) for k in (row.get("keywords") or [])],
                )
                for row in rows
            ],
            summary_truncated=summary_truncated,
            # 目录被条数上限截断时同样置 degraded：服务端"内容不完整"的信号保持
            # 单一入口，节点侧据此发 memory_prime_degraded（见 review I-5）。
            degraded=degraded or index_entries_truncated,
            index_entries_truncated=index_entries_truncated,
        )
