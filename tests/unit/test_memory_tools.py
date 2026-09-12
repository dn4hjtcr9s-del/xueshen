"""记忆工具服务端单元测试（memory-rebuild §2.4 D1/D3 / §5.7 Phase 5）。

覆盖范围与边界：
- search：四列匹配权重、any/all 两种模式、固定排序（含同权重时的 updated_at /
  memory_id 稳定序）、truncated 标志、**出参不含正文字段**、SQL 用户隔离；
- read：行切片（offset/max_lines）、truncated 语义、文档不存在 → MEMORY_NOT_FOUND；
- prime：summary 缺失/损坏/超限 → degraded=true 且不抛错、小预算截断、目录投影；
- 端到端（TestClient + 假会话工厂）：认证矩阵、请求体 extra="forbid"、跨用户隔离。

**没有真实 PostgreSQL**：`fetch_search_rows` / `fetch_index_projection` 由内存实现替换
（语义与 SQL 对齐、按 user_id 隔离），因此 ILIKE / unnest / ORDER BY 的**数据库侧**
行为不在这里断言——SQL 文本与纯排序函数在这里断言，真实库验证另见报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.memory.contracts.errors import MemoryNotFoundError
from backend.memory.contracts.results import (
    MemoryToolPrimeResponse,
    MemoryToolReadRequest,
    MemoryToolSearchRequest,
)
from backend.memory.services import memory_tools as tools
from backend.memory.services.memory_tools import (
    PRIME_INDEX_ENTRIES_MAX,
    PRIME_SUMMARY_MAX_BYTES,
    PRIME_SUMMARY_MAX_CHARS,
    FilePrimeSummaryReader,
    MemoryToolsService,
    build_search_sql,
    hit_weight,
    normalize_tool_queries,
    parse_prime_summary,
    slice_lines,
    tool_hit_sort_key,
)
from backend.settings import Settings
from tests.unit.api_fakes import build_test_app
from tests.unit.worker_fakes import FakeSessionFactory

USER_A = UUID("11111111-1111-1111-1111-111111111111")
USER_B = UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime(2026, 9, 10, 5, 0, tzinfo=UTC)
OLD = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)

_SEARCH_FIELDS = {
    "memory_id",
    "name",
    "description",
    "keywords",
    "version",
    "updated_at",
}


# ---------------------------------------------------------------------------
# 假实现：内存版 index 注册表 + 假会话工厂 + 假 MemoryService
# ---------------------------------------------------------------------------


def _entry(
    memory_id: str,
    *,
    title: str,
    summary: str = "",
    keywords: list[str] | None = None,
    aliases: list[str] | None = None,
    version: int = 1,
    updated_at: datetime = NOW,
) -> dict[str, Any]:
    """一条 memory_index_entries 行（列名与真实表一致）。"""
    return {
        "memory_id": memory_id,
        "title": title,
        "summary": summary,
        "keywords": keywords or [],
        "aliases": aliases or [],
        "source_version": version,
        "updated_at": updated_at,
    }


@dataclass
class FakeRegistry:
    """内存版 memory_index_entries：按 user_id 分桶，模拟真实表的行级隔离。"""

    by_user: dict[UUID, list[dict[str, Any]]] = field(default_factory=dict)

    def add(self, user_id: UUID, row: dict[str, Any]) -> None:
        self.by_user.setdefault(user_id, []).append(row)

    def rows_for(self, user_id: UUID) -> list[dict[str, Any]]:
        return list(self.by_user.get(user_id, []))


def _hit_columns(row: dict[str, Any], query: str) -> bool:
    """与 SQL 的 `_row_hit` 同语义：四列任一大小写不敏感子串命中。"""
    needle = query.casefold()
    haystacks = [
        str(row["title"]),
        str(row["summary"]),
        *[str(a) for a in row.get("aliases") or []],
        *[str(k) for k in row.get("keywords") or []],
    ]
    return any(needle in haystack.casefold() for haystack in haystacks)


def _install_registry(monkeypatch: Any, registry: FakeRegistry) -> list[int | None]:
    """用内存实现替换两个 SQL 边界：语义与 SQL 一致，且严格按 user_id 过滤。

    返回目录投影收到的 `limit` 实参序列（供"有界返回"断言：必须只取 上限+1 条，
    不能把整份注册表读进来）。
    """
    projection_limits: list[int | None] = []

    async def fake_fetch_search_rows(
        session: Any,
        *,
        user_id: UUID,
        queries: list[str],
        match_mode: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        def matched(row: dict[str, Any]) -> bool:
            hits = [_hit_columns(row, query) for query in queries]
            return any(hits) if match_mode == "any" else all(hits)

        rows = [row for row in registry.rows_for(user_id) if matched(row)]
        rows.sort(key=lambda row: tool_hit_sort_key(row, queries))
        return rows[:limit]

    async def fake_fetch_index_projection(
        session: Any, *, user_id: UUID, limit: int | None = None
    ) -> list[dict[str, Any]]:
        projection_limits.append(limit)
        rows = sorted(registry.rows_for(user_id), key=lambda row: str(row["memory_id"]))
        if limit is not None:
            rows = rows[:limit]
        return [
            {
                "memory_id": row["memory_id"],
                "title": row["title"],
                "summary": row["summary"],
                "keywords": row["keywords"],
            }
            for row in rows
        ]

    monkeypatch.setattr(tools, "fetch_search_rows", fake_fetch_search_rows)
    monkeypatch.setattr(tools, "fetch_index_projection", fake_fetch_index_projection)
    return projection_limits


class FakeMemoryService:
    """read_active_content 替身：按 (user_id, memory_id) 返回 (version, checksum, 正文)。"""

    def __init__(self, documents: dict[tuple[UUID, str], tuple[int, str, str]] | None = None):
        self.documents = documents or {}

    async def read_active_content(
        self, *, user_id: UUID, memory_id: str
    ) -> tuple[int, str, str] | None:
        return self.documents.get((user_id, memory_id))


class StaticSummaryReader:
    """固定 summary 输入的读取器替身（None 表示文件缺失）。"""

    def __init__(self, raw: bytes | None, generated_at: datetime | None = None) -> None:
        self.raw = raw
        self.generated_at = generated_at

    async def read(self, *, user_id: UUID) -> tuple[bytes | None, datetime | None]:
        return self.raw, self.generated_at


def _service(
    *,
    registry: FakeRegistry | None = None,
    memory_service: FakeMemoryService | None = None,
    summary_reader: Any = None,
) -> MemoryToolsService:
    return MemoryToolsService(
        session_factory=FakeSessionFactory(),  # type: ignore[arg-type]
        memory_service=memory_service or FakeMemoryService(),  # type: ignore[arg-type]
        summary_reader=summary_reader or StaticSummaryReader(None),
    )


# ---------------------------------------------------------------------------
# search：SQL 构造（纯函数）
# ---------------------------------------------------------------------------


def test_build_search_sql_scopes_user_and_escapes_metacharacters() -> None:
    sql, params = build_search_sql(user_id=USER_A, queries=["100%_x"], match_mode="any", limit=11)
    assert "WHERE user_id = :user_id" in sql
    assert params["user_id"] == USER_A
    assert params["limit"] == 11
    # LIKE 元字符必须转义，否则 % / _ 会变成通配符（配合 ESCAPE '\'）
    assert params["q0"] == "%100\\%\\_x%"
    # 4 处在 WHERE 的候选条件，3 处在 ORDER BY 的权重 CASE（name 2 + keyword 1）
    assert sql.count("ESCAPE '\\'") == 7
    # 四列都在匹配域内：title(name) / summary(description) / aliases / keywords
    for column in ("title ILIKE", "summary ILIKE", "unnest(aliases)", "unnest(keywords)"):
        assert column in sql
    # 不碰向量、不碰正文列：SQL 只读注册表四列 + 版本 + 时间
    assert "search_text" not in sql
    assert "embedding" not in sql


def test_build_search_sql_fixed_order_and_paging() -> None:
    sql, _params = build_search_sql(user_id=USER_A, queries=["椭圆"], match_mode="any", limit=6)
    assert "ORDER BY" in sql
    # 权重降序 → updated_at 降序 → memory_id 升序（全序，无并列不确定项）
    assert sql.index("THEN 2") < sql.index("THEN 1") < sql.index("ELSE 0")
    assert "END DESC" in sql
    assert "updated_at DESC" in sql
    assert "memory_id ASC" in sql
    assert "LIMIT :limit" in sql


def test_build_search_sql_match_mode_any_joins_with_or_all_joins_with_and() -> None:
    any_sql, _ = build_search_sql(
        user_id=USER_A, queries=["椭圆", "焦点"], match_mode="any", limit=10
    )
    all_sql, _ = build_search_sql(
        user_id=USER_A, queries=["椭圆", "焦点"], match_mode="all", limit=10
    )
    assert "      OR (title ILIKE :q1" in any_sql
    assert "      AND (title ILIKE :q1" in all_sql
    assert "      AND (title ILIKE :q1" not in any_sql


def test_normalize_tool_queries_strips_blanks_and_duplicates() -> None:
    assert normalize_tool_queries([" 椭圆 ", "椭圆", "", "   ", "焦点"]) == ["椭圆", "焦点"]


def test_hit_weight_orders_name_aliases_over_keywords_over_description() -> None:
    """四列匹配权重：name/aliases=2 > keywords=1 > 仅 description=0。"""
    kwargs: dict[str, Any] = {"keywords": ["判别词"], "aliases": ["ellipse"]}
    assert hit_weight(queries=["椭圆"], name="椭圆方程", **kwargs) == 2
    assert hit_weight(queries=["ELLIPSE"], name="圆锥曲线", **kwargs) == 2  # 大小写不敏感
    assert hit_weight(queries=["判别词"], name="圆锥曲线", **kwargs) == 1
    assert hit_weight(queries=["在描述里"], name="圆锥曲线", keywords=[], aliases=[]) == 0
    # name 未命中但 aliases 命中仍算最高权重
    assert hit_weight(queries=["椭圆"], name="圆锥曲线", keywords=[], aliases=["椭圆方程"]) == 2


def test_tool_hit_sort_key_is_total_and_stable() -> None:
    """同权重先 updated_at 降序，再 memory_id 升序，保证全序稳定。"""
    queries = ["椭圆"]
    rows = [
        _entry("mastery:b", title="椭圆", updated_at=OLD),
        _entry("mastery:a", title="椭圆", updated_at=OLD),
        _entry("mastery:c", title="椭圆", updated_at=NOW),
        _entry("mastery:d", title="无", summary="椭圆", updated_at=NOW),
        _entry("mastery:e", title="无", keywords=["椭圆"], updated_at=NOW),
    ]
    ordered = [
        row["memory_id"] for row in sorted(rows, key=lambda r: tool_hit_sort_key(r, queries))
    ]
    # 权重 2（c 最新 → a/b 同刻按 id）→ 权重 1（e）→ 权重 0（d）
    assert ordered == ["mastery:c", "mastery:a", "mastery:b", "mastery:e", "mastery:d"]


# ---------------------------------------------------------------------------
# search：服务行为
# ---------------------------------------------------------------------------


async def test_search_returns_fixed_order_and_truncated_flag(monkeypatch: Any) -> None:
    registry = FakeRegistry()
    for row in (
        _entry("mastery:新旧同权", title="椭圆", updated_at=OLD),
        _entry("mastery:描述命中", title="无", summary="椭圆相关内容", updated_at=NOW),
        _entry("mastery:最新", title="椭圆", updated_at=NOW),
        _entry("mastery:关键词命中", title="无", keywords=["椭圆"], updated_at=NOW),
    ):
        registry.add(USER_A, row)
    _install_registry(monkeypatch, registry)

    service = _service(registry=registry)
    response = await service.search(
        user_id=USER_A, request=MemoryToolSearchRequest(queries=["椭圆"], max_results=3)
    )
    assert [item.memory_id for item in response.items] == [
        "mastery:最新",
        "mastery:新旧同权",
        "mastery:关键词命中",
    ]
    assert response.truncated is True  # 命中 4 条 > max_results=3


async def test_search_truncated_false_when_all_hits_fit(monkeypatch: Any) -> None:
    registry = FakeRegistry()
    registry.add(USER_A, _entry("mastery:a", title="椭圆"))
    _install_registry(monkeypatch, registry)
    response = await _service(registry=registry).search(
        user_id=USER_A, request=MemoryToolSearchRequest(queries=["椭圆"], max_results=10)
    )
    assert response.truncated is False
    assert len(response.items) == 1


async def test_search_match_mode_all_requires_every_query(monkeypatch: Any) -> None:
    registry = FakeRegistry()
    registry.add(USER_A, _entry("mastery:both", title="椭圆", summary="焦点性质"))
    registry.add(USER_A, _entry("mastery:only-one", title="椭圆", summary="定义"))
    _install_registry(monkeypatch, registry)
    service = _service(registry=registry)

    any_response = await service.search(
        user_id=USER_A,
        request=MemoryToolSearchRequest(queries=["椭圆", "焦点"], match_mode="any"),
    )
    all_response = await service.search(
        user_id=USER_A,
        request=MemoryToolSearchRequest(queries=["椭圆", "焦点"], match_mode="all"),
    )
    assert [item.memory_id for item in any_response.items] == ["mastery:both", "mastery:only-one"]
    assert [item.memory_id for item in all_response.items] == ["mastery:both"]


async def test_search_response_has_no_document_body_fields(monkeypatch: Any) -> None:
    """强制"search 定位 → read 下沉"：出参只有注册表四列 + 版本 + 时间。"""
    registry = FakeRegistry()
    registry.add(
        USER_A,
        _entry("mastery:椭圆", title="椭圆", summary="圆锥曲线之一", keywords=["焦点"], version=3),
    )
    _install_registry(monkeypatch, registry)
    response = await _service(registry=registry).search(
        user_id=USER_A, request=MemoryToolSearchRequest(queries=["椭圆"])
    )
    payload = response.model_dump(mode="json")
    item = payload["items"][0]
    assert set(item) == _SEARCH_FIELDS
    # 正文字段一律不出现（含旧 /search 的 matched_excerpt / evidence_refs）
    for forbidden in ("content", "body", "matched_excerpt", "evidence_refs"):
        assert forbidden not in item
    assert item["name"] == "椭圆"
    assert item["description"] == "圆锥曲线之一"
    assert item["keywords"] == ["焦点"]
    assert item["version"] == 3
    assert payload["truncated"] is False


async def test_search_blank_queries_return_empty_without_touching_db(monkeypatch: Any) -> None:
    async def explode(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - 不应被调用
        raise AssertionError("空白 query 不应打库")

    monkeypatch.setattr(tools, "fetch_search_rows", explode)
    response = await _service().search(
        user_id=USER_A, request=MemoryToolSearchRequest(queries=["   "])
    )
    assert response.items == []
    assert response.truncated is False


async def test_search_isolates_users(monkeypatch: Any) -> None:
    """A 用户搜不到 B 用户的任何条目（服务层按认证 user_id 过滤）。"""
    registry = FakeRegistry()
    registry.add(USER_A, _entry("mastery:我的椭圆", title="椭圆"))
    registry.add(USER_B, _entry("mastery:他的椭圆", title="椭圆"))
    _install_registry(monkeypatch, registry)
    service = _service(registry=registry)

    mine = await service.search(user_id=USER_A, request=MemoryToolSearchRequest(queries=["椭圆"]))
    theirs = await service.search(user_id=USER_B, request=MemoryToolSearchRequest(queries=["椭圆"]))
    assert [item.memory_id for item in mine.items] == ["mastery:我的椭圆"]
    assert [item.memory_id for item in theirs.items] == ["mastery:他的椭圆"]


# ---------------------------------------------------------------------------
# read：行切片与服务行为
# ---------------------------------------------------------------------------


def test_slice_lines_offset_and_truncated() -> None:
    content = "v2\n---\n# 椭圆\n定义\n焦点"
    chunk, total, truncated = slice_lines(content, line_offset=0, max_lines=2)
    assert chunk == "v2\n---"
    assert total == 5
    assert truncated is True

    chunk, total, truncated = slice_lines(content, line_offset=2, max_lines=2)
    assert chunk == "# 椭圆\n定义"
    assert total == 5
    assert truncated is True

    chunk, _total, truncated = slice_lines(content, line_offset=3, max_lines=500)
    assert chunk == "定义\n焦点"
    assert truncated is False


def test_slice_lines_beyond_end_is_empty_and_not_truncated() -> None:
    chunk, total, truncated = slice_lines("a\nb", line_offset=10, max_lines=5)
    assert chunk == ""
    assert total == 2
    assert truncated is False


def test_slice_lines_empty_content() -> None:
    assert slice_lines("", line_offset=0, max_lines=200) == ("", 0, False)


async def test_read_returns_version_checksum_and_slice() -> None:
    body = "v2\n---\n# 椭圆\n定义"
    memory_service = FakeMemoryService({(USER_A, "mastery:椭圆"): (3, "c" * 64, body)})
    response = await _service(memory_service=memory_service).read(
        user_id=USER_A,
        request=MemoryToolReadRequest(memory_id="mastery:椭圆", line_offset=2, max_lines=1),
    )
    assert response.memory_id == "mastery:椭圆"
    assert response.version == 3
    assert response.checksum == "c" * 64
    assert response.content == "# 椭圆"
    assert response.line_offset == 2
    assert response.total_lines == 4
    assert response.truncated is True


async def test_read_missing_document_raises_domain_error() -> None:
    """不存在 / 已删除 / quarantine 一律 MEMORY_NOT_FOUND（不泄露存在性）。"""
    with pytest.raises(MemoryNotFoundError) as excinfo:
        await _service().read(
            user_id=USER_A, request=MemoryToolReadRequest(memory_id="mastery:不存在")
        )
    assert excinfo.value.code == "MEMORY_NOT_FOUND"
    assert excinfo.value.http_status == 404


async def test_read_isolates_users() -> None:
    memory_service = FakeMemoryService({(USER_A, "mastery:椭圆"): (1, "x" * 64, "正文")})
    with pytest.raises(MemoryNotFoundError):
        await _service(memory_service=memory_service).read(
            user_id=USER_B, request=MemoryToolReadRequest(memory_id="mastery:椭圆")
        )


# ---------------------------------------------------------------------------
# prime：summary 解析、降级与文件读取
# ---------------------------------------------------------------------------


def test_parse_prime_summary_valid_v1() -> None:
    body, schema_version, stamp, degraded, reason = parse_prime_summary(
        "v1\n## 用户画像\n偏好例题".encode(),
        generated_at=NOW,
    )
    assert degraded is False
    assert reason is None
    assert schema_version == "v1"
    assert stamp == NOW
    assert body.startswith("## 用户画像")
    assert "v1" not in body.splitlines()[0]


def test_parse_prime_summary_missing_is_degraded() -> None:
    body, schema_version, stamp, degraded, reason = parse_prime_summary(None, generated_at=None)
    assert (body, schema_version, stamp, degraded, reason) == ("", "v1", None, True, "missing")


def test_parse_prime_summary_corrupt_and_oversize_are_degraded() -> None:
    assert parse_prime_summary(b"v2\n\xe6\x97\xa7", generated_at=None)[3:] == (
        True,
        "schema_mismatch",
    )
    assert parse_prime_summary(b"\xff\xfe\x00", generated_at=None)[3:] == (True, "not_utf8")
    oversize = b"v1\n" + b"x" * PRIME_SUMMARY_MAX_BYTES
    assert parse_prime_summary(oversize, generated_at=None)[3:] == (True, "size_exceeded")


def test_file_prime_summary_reader_missing_and_present(tmp_path: Path) -> None:
    import asyncio

    root = tmp_path / "storage"
    reader = FilePrimeSummaryReader(root)
    assert asyncio.run(reader.read(user_id=USER_A)) == (None, None)

    path = root / "users" / str(USER_A)[:2] / str(USER_A) / "memory_summary.md"
    path.parent.mkdir(parents=True)
    path.write_bytes("v1\n## 用户画像\n喜欢例题".encode())
    raw, generated_at = asyncio.run(reader.read(user_id=USER_A))
    assert raw == "v1\n## 用户画像\n喜欢例题".encode()
    assert generated_at is not None


async def test_prime_missing_summary_is_degraded_without_error(monkeypatch: Any) -> None:
    """§5.7：summary 缺失不报错，返回空 prime + degraded=true + 目录投影。"""
    registry = FakeRegistry()
    registry.add(
        USER_A, _entry("mastery:椭圆", title="椭圆", summary="圆锥曲线之一", keywords=["焦点"])
    )
    registry.add(USER_B, _entry("mastery:他的", title="他的主题"))
    _install_registry(monkeypatch, registry)

    response = await _service(registry=registry).prime(user_id=USER_A)
    assert isinstance(response, MemoryToolPrimeResponse)
    assert response.summary == ""
    assert response.degraded is True
    assert response.summary_truncated is False
    assert response.generated_at is None
    assert response.schema_version == "v1"
    assert [entry.memory_id for entry in response.index_entries] == ["mastery:椭圆"]
    assert response.index_entries[0].keywords == ["焦点"]
    assert "content" not in response.index_entries[0].model_dump()


async def test_prime_summary_is_truncated_to_small_budget(monkeypatch: Any) -> None:
    _install_registry(monkeypatch, FakeRegistry())
    long_body = b"v1\n" + ("\xe5\xad\x97" * (PRIME_SUMMARY_MAX_CHARS + 50)).encode()
    response = await _service(summary_reader=StaticSummaryReader(long_body, NOW)).prime(
        user_id=USER_A
    )
    assert response.degraded is False
    assert response.summary_truncated is True
    assert len(response.summary) == PRIME_SUMMARY_MAX_CHARS


async def test_prime_valid_summary_passes_through(monkeypatch: Any) -> None:
    _install_registry(monkeypatch, FakeRegistry())
    summary = StaticSummaryReader("v1\n## 用户画像\n偏好例题驱动".encode(), NOW)
    response = await _service(summary_reader=summary).prime(user_id=USER_A)
    assert response.degraded is False
    assert response.summary == "## 用户画像\n偏好例题驱动"
    assert response.generated_at == NOW
    assert response.index_entries == []
    assert response.index_entries_truncated is False


def _fill_registry(registry: FakeRegistry, count: int) -> None:
    """灌入 count 条目录条目（memory_id 升序即注入序）。"""
    for index in range(count):
        registry.add(
            USER_A,
            _entry(
                f"mastery:{index:04d}",
                title=f"主题{index}",
                summary="条目描述",
                keywords=["关键词"],
            ),
        )


async def test_prime_index_entries_are_bounded_with_decidable_signal(monkeypatch: Any) -> None:
    """review I-5：主题数超过上限时响应**有界**，且用 `index_entries_truncated` 表达截断。

    契约里没有该字段时服务端只能全量返回（ADD-047 的旧行为）；这里锁死新语义：
    仍是按 memory_id 升序的**确定性前缀**，既有字段一个不少，截断事实可判定。
    """
    registry = FakeRegistry()
    _fill_registry(registry, PRIME_INDEX_ENTRIES_MAX + 5)
    limits = _install_registry(monkeypatch, registry)

    # 用合法 summary 隔离变量：此时 degraded 只可能来自目录截断
    summary = StaticSummaryReader("v1\n## 用户画像\n偏好例题驱动".encode(), NOW)
    response = await _service(registry=registry, summary_reader=summary).prime(user_id=USER_A)

    assert len(response.index_entries) == PRIME_INDEX_ENTRIES_MAX
    assert response.index_entries_truncated is True
    # 目录被截断同样算"内容不完整"：既有 degraded 信号一并置位
    assert response.degraded is True
    assert response.summary_truncated is False
    # 确定性前缀：保留的是前 MAX 条，不是随意丢弃
    assert [entry.memory_id for entry in response.index_entries[:3]] == [
        "mastery:0000",
        "mastery:0001",
        "mastery:0002",
    ]
    assert response.index_entries[-1].memory_id == f"mastery:{PRIME_INDEX_ENTRIES_MAX - 1:04d}"
    # 有界读取：只多取一条判定超限，绝不把整份注册表读进内存
    assert limits == [PRIME_INDEX_ENTRIES_MAX + 1]


async def test_prime_index_entries_at_cap_is_not_marked_truncated(monkeypatch: Any) -> None:
    """恰好等于上限不算截断：边界正例，避免"永远报截断"的假信号。"""
    registry = FakeRegistry()
    _fill_registry(registry, PRIME_INDEX_ENTRIES_MAX)
    _install_registry(monkeypatch, registry)

    summary = StaticSummaryReader("v1\n## 用户画像\n偏好例题驱动".encode(), NOW)
    response = await _service(registry=registry, summary_reader=summary).prime(user_id=USER_A)

    assert len(response.index_entries) == PRIME_INDEX_ENTRIES_MAX
    assert response.index_entries_truncated is False
    assert response.degraded is False


# ---------------------------------------------------------------------------
# HTTP 端点（TestClient + 假会话工厂；不连 PostgreSQL）
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "development",
        "dev_auth_enabled": True,
        "memory_storage_root": str(tmp_path / "storage"),
    }
    base.update(overrides)
    return Settings(**base)


def _client(
    tmp_path: Path, monkeypatch: Any, memory_service: FakeMemoryService | None = None, **kwargs: Any
) -> TestClient:
    app, *_ = build_test_app(
        _settings(tmp_path, **kwargs),
        monkeypatch=monkeypatch,
        memory_service=memory_service or FakeMemoryService(),  # type: ignore[arg-type]
    )
    return TestClient(app)


def _auth(user_id: UUID = USER_A, **extra: str) -> dict[str, str]:
    return {"X-Dev-User-Id": str(user_id), **extra}


def test_prime_endpoint_missing_summary_is_200_degraded(tmp_path: Path, monkeypatch: Any) -> None:
    registry = FakeRegistry()
    registry.add(USER_A, _entry("mastery:椭圆", title="椭圆", summary="圆锥曲线之一"))
    _install_registry(monkeypatch, registry)
    client = _client(tmp_path, monkeypatch)

    response = client.post("/api/v1/internal/memory/tool/prime", json={}, headers=_auth(USER_A))
    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    assert body["summary"] == ""
    assert body["schema_version"] == "v1"
    assert body["generated_at"] is None
    assert body["summary_truncated"] is False
    assert [entry["memory_id"] for entry in body["index_entries"]] == ["mastery:椭圆"]
    assert body["index_entries_truncated"] is False
    # 既有字段一个不少（review I-5：新增字段只追加，既有形状不变）
    assert set(body) == {
        "summary",
        "schema_version",
        "generated_at",
        "index_entries",
        "summary_truncated",
        "degraded",
        "index_entries_truncated",
    }


def test_prime_endpoint_reads_summary_file_from_storage_root(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _install_registry(monkeypatch, FakeRegistry())
    client = _client(tmp_path, monkeypatch)
    path = (
        Path(_settings(tmp_path).memory_storage_root)
        / "users"
        / str(USER_A)[:2]
        / str(USER_A)
        / "memory_summary.md"
    )
    path.parent.mkdir(parents=True)
    path.write_bytes("v1\n## 稳定偏好\n先看例题".encode())

    body = client.post("/api/v1/internal/memory/tool/prime", json={}, headers=_auth(USER_A)).json()
    assert body["degraded"] is False
    assert body["summary"] == "## 稳定偏好\n先看例题"


def test_search_endpoint_returns_items_without_body(tmp_path: Path, monkeypatch: Any) -> None:
    registry = FakeRegistry()
    registry.add(
        USER_A, _entry("mastery:椭圆", title="椭圆", summary="圆锥曲线之一", keywords=["焦点"])
    )
    _install_registry(monkeypatch, registry)
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/v1/internal/memory/tool/search",
        json={"queries": ["椭圆"], "match_mode": "any", "max_results": 10},
        headers=_auth(USER_A),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is False
    assert set(body["items"][0]) == _SEARCH_FIELDS


def test_search_endpoint_isolates_users(tmp_path: Path, monkeypatch: Any) -> None:
    registry = FakeRegistry()
    registry.add(USER_A, _entry("mastery:我的椭圆", title="椭圆"))
    registry.add(USER_B, _entry("mastery:他的椭圆", title="椭圆"))
    _install_registry(monkeypatch, registry)
    client = _client(tmp_path, monkeypatch)

    mine = client.post(
        "/api/v1/internal/memory/tool/search",
        json={"queries": ["椭圆"]},
        headers=_auth(USER_A),
    ).json()
    theirs = client.post(
        "/api/v1/internal/memory/tool/search",
        json={"queries": ["椭圆"]},
        headers=_auth(USER_B),
    ).json()
    assert [item["memory_id"] for item in mine["items"]] == ["mastery:我的椭圆"]
    assert [item["memory_id"] for item in theirs["items"]] == ["mastery:他的椭圆"]


def test_read_endpoint_slices_and_404s(tmp_path: Path, monkeypatch: Any) -> None:
    body = "v2\n---\n# 椭圆\n定义\n焦点"
    client = _client(
        tmp_path,
        monkeypatch,
        memory_service=FakeMemoryService({(USER_A, "mastery:椭圆"): (7, "a" * 64, body)}),
    )
    response = client.post(
        "/api/v1/internal/memory/tool/read",
        json={"memory_id": "mastery:椭圆", "line_offset": 2, "max_lines": 2},
        headers=_auth(USER_A),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == 7
    assert payload["checksum"] == "a" * 64
    assert payload["content"] == "# 椭圆\n定义"
    assert payload["line_offset"] == 2
    assert payload["total_lines"] == 5
    assert payload["truncated"] is True

    missing = client.post(
        "/api/v1/internal/memory/tool/read",
        json={"memory_id": "mastery:不存在"},
        headers=_auth(USER_A),
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "MEMORY_NOT_FOUND"


def test_tool_endpoints_reject_user_id_in_body(tmp_path: Path, monkeypatch: Any) -> None:
    """user_id 只能来自认证上下文：请求体夹带一律 422 REQUEST_EXTRA_FIELD。"""
    _install_registry(monkeypatch, FakeRegistry())
    client = _client(tmp_path, monkeypatch)
    for path, payload in (
        ("/api/v1/internal/memory/tool/search", {"queries": ["椭圆"], "user_id": str(USER_B)}),
        ("/api/v1/internal/memory/tool/read", {"memory_id": "learner", "user_id": str(USER_B)}),
        ("/api/v1/internal/memory/tool/prime", {"user_id": str(USER_B)}),
    ):
        response = client.post(path, json=payload, headers=_auth(USER_A))
        assert response.status_code == 422, path
        assert response.json()["error"]["code"] == "REQUEST_EXTRA_FIELD", path


def test_tool_endpoints_require_read_scope(tmp_path: Path, monkeypatch: Any) -> None:
    _install_registry(monkeypatch, FakeRegistry())
    client = _client(tmp_path, monkeypatch, dev_auth_allow_scope_override=True)
    # 无凭证 → 401
    assert client.post("/api/v1/internal/memory/tool/prime", json={}).status_code == 401
    # actor 不在只读白名单 → 403
    forbidden = client.post(
        "/api/v1/internal/memory/tool/prime",
        json={},
        headers=_auth(**{"X-Dev-Actor-Type": "knowledge_graph_ui", "X-Dev-Scopes": "memory:read"}),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "AUTH_FORBIDDEN"
    # 内部 Agent（conversation_agent + memory:read）可用
    agent = client.post(
        "/api/v1/internal/memory/tool/prime",
        json={},
        headers=_auth(**{"X-Dev-Actor-Type": "conversation_agent", "X-Dev-Scopes": "memory:read"}),
    )
    assert agent.status_code == 200


def test_legacy_plural_alias_still_serves(tmp_path: Path, monkeypatch: Any) -> None:
    """brief 里的复数路径作为隐藏兼容别名保留（不进 OpenAPI 快照）。"""
    _install_registry(monkeypatch, FakeRegistry())
    client = _client(tmp_path, monkeypatch)
    response = client.post("/api/v1/internal/memories/tool/prime", json={}, headers=_auth(USER_A))
    assert response.status_code == 200
    assert response.json()["degraded"] is True
    schema = client.app.openapi()  # type: ignore[attr-defined]
    assert "/api/v1/internal/memory/tool/prime" in schema["paths"]
    assert "/api/v1/internal/memories/tool/prime" not in schema["paths"]


def test_search_request_validation_bounds(tmp_path: Path, monkeypatch: Any) -> None:
    _install_registry(monkeypatch, FakeRegistry())
    client = _client(tmp_path, monkeypatch)
    too_many = client.post(
        "/api/v1/internal/memory/tool/search",
        json={"queries": [f"q{i}" for i in range(9)]},
        headers=_auth(USER_A),
    )
    assert too_many.status_code == 422
    empty = client.post(
        "/api/v1/internal/memory/tool/search", json={"queries": []}, headers=_auth(USER_A)
    )
    assert empty.status_code == 422
    oversized_result = client.post(
        "/api/v1/internal/memory/tool/search",
        json={"queries": ["椭圆"], "max_results": 51},
        headers=_auth(USER_A),
    )
    assert oversized_result.status_code == 422
    long_line_read = client.post(
        "/api/v1/internal/memory/tool/read",
        json={"memory_id": "learner", "max_lines": 501},
        headers=_auth(USER_A),
    )
    assert long_line_read.status_code == 422


def test_unknown_user_gets_empty_prime(tmp_path: Path, monkeypatch: Any) -> None:
    """没有任何注册表行的用户也返回空目录而不是报错。"""
    _install_registry(monkeypatch, FakeRegistry())
    client = _client(tmp_path, monkeypatch)
    body = client.post("/api/v1/internal/memory/tool/prime", json={}, headers=_auth(uuid4())).json()
    assert body["index_entries"] == []
    assert body["degraded"] is True
