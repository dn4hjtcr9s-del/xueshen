"""记忆工具 SQL 与版本化读取的集成测试（memory-rebuild §2.4 D3 / §5.7 Phase 5）。

单元测试用假会话覆盖服务编排，这里补**只有真实 PostgreSQL + 真实文件存储才能证明**的部分：

- 四列 ILIKE 子串匹配（title/summary/aliases/keywords）与大小写不敏感；
- any / all 两种 match_mode；
- 固定排序（命中列权重降序 → updated_at 降序 → memory_id 升序），且 SQL 顺序与
  Python `tool_hit_sort_key` 完全一致；LIMIT 取到的是全序前 N 条（truncated 才可信）；
- LIKE 元字符（% _）按字面匹配，不当通配符；
- 用户隔离：任何查询都带 user_id，跨用户读不到；
- memory.read 走**活动版本**而不是 current/ 物化副本，已删除（quarantine）读不到。

运行：`scripts/ci-local.sh backend-integration`，或显式注入 memory_test：
    DATABASE_URL=postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test \
    uv run pytest tests/integration/test_memory_tools_sql.py -q
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.persistence import documents as docs_repo
from backend.memory.services.memory_service import MemoryService
from backend.memory.services.memory_tools import (
    fetch_index_projection,
    fetch_search_rows,
    tool_hit_sort_key,
)
from backend.memory.storage.local_markdown import LocalMarkdownStore

USER_A = UUID("aaaaaaaa-0000-0000-0000-0000000000a1")
USER_B = UUID("bbbbbbbb-0000-0000-0000-0000000000b2")
NOW = datetime(2026, 9, 10, 5, 0, tzinfo=UTC)
DAY = timedelta(days=1)

#: (user, memory_id, title=name, summary=description, keywords, aliases, version, updated_at)
SEED = [
    (USER_A, "mastery:ellipse", "椭圆", "圆锥曲线之一", ["焦点"], ["ellipse"], 3, NOW),
    (USER_A, "mastery:focus", "焦点", "椭圆的焦点性质", [], [], 2, NOW - DAY),
    # 同权重同 updated_at：只能靠 memory_id 升序决出全序
    (USER_A, "mastery:b-tie", "椭圆方程", "并列项", [], [], 1, NOW - 2 * DAY),
    (USER_A, "mastery:a-tie", "椭圆方程", "并列项", [], [], 1, NOW - 2 * DAY),
    # 权重 1（只有 keywords 命中）与权重 0（只有 description 命中）
    (USER_A, "mastery:kw", "无关标题", "无关描述", ["椭圆"], [], 4, NOW),
    (USER_A, "mastery:desc", "无关标题", "椭圆相关描述", [], [], 5, NOW),
    # LIKE 元字符按字面匹配
    (USER_A, "mastery:pct", "100%_命中", "字面百分号", [], [], 1, NOW),
    # 别名大小写：query 用小写英文
    (USER_A, "mastery:case", "圆锥曲线", "大小写用例", [], ["Ellipse"], 1, NOW),
    # 另一个用户：任何查询都不应返回
    (USER_B, "mastery:other", "椭圆", "别人的记忆", ["焦点"], ["ellipse"], 9, NOW),
]


async def _seed(session: AsyncSession) -> None:
    for user_id, memory_id, title, summary, keywords, aliases, version, updated_at in SEED:
        topic_key = memory_id.removeprefix("mastery:")
        await session.execute(
            text(
                "INSERT INTO memory_documents (user_id, memory_id, memory_type, topic_key,"
                " topic_title, logical_path) VALUES (:u, :m, 'mastery', :k, :title, :p)"
            ),
            {
                "u": user_id,
                "m": memory_id,
                "k": topic_key,
                "title": title,
                "p": f"mastery/{topic_key}.md",
            },
        )
        await session.execute(
            text(
                "INSERT INTO memory_index_entries (user_id, memory_id, source_version,"
                " memory_type, topic_key, title, summary, keywords, aliases, search_text,"
                " updated_at) VALUES (:u, :m, :v, 'mastery', :k, :title, :summary,"
                " CAST(:keywords AS text[]), CAST(:aliases AS text[]), :search_text,"
                " :updated_at)"
            ),
            {
                "u": user_id,
                "m": memory_id,
                "v": version,
                "k": topic_key,
                "title": title,
                "summary": summary,
                "keywords": keywords,
                "aliases": aliases,
                "search_text": f"{title} {summary}",
                "updated_at": updated_at,
            },
        )
    await session.commit()


async def _search(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    queries: list[str],
    match_mode: str = "any",
    limit: int = 50,
) -> list[dict[str, object]]:
    async with factory() as session:
        return await fetch_search_rows(
            session,
            user_id=user_id,
            queries=queries,
            match_mode=match_mode,
            limit=limit,
        )


async def _ids(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    queries: list[str],
    **kwargs: object,
) -> list[str]:
    rows = await _search(factory, user_id=user_id, queries=queries, **kwargs)  # type: ignore[arg-type]
    return [str(row["memory_id"]) for row in rows]


@pytest.fixture()
async def seeded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        await _seed(session)


async def test_search_matches_four_registry_columns(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    """四列都参与匹配：title / summary / aliases / keywords（正文列 search_text 不参与）。"""
    rows = await _search(session_factory, user_id=USER_A, queries=["椭圆"])
    assert {str(row["memory_id"]) for row in rows} == {
        "mastery:ellipse",  # title 命中
        "mastery:a-tie",  # title 命中
        "mastery:b-tie",  # title 命中
        "mastery:kw",  # keywords 命中
        "mastery:desc",  # description 命中
        "mastery:focus",  # description 命中
    }
    # aliases 命中（小写 query 命中大写别名 → ILIKE 大小写不敏感）
    assert await _ids(session_factory, user_id=USER_A, queries=["ellipse"]) == [
        "mastery:case",
        "mastery:ellipse",
    ]


async def test_search_match_mode_any_and_all(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    any_ids = await _ids(
        session_factory, user_id=USER_A, queries=["椭圆", "焦点"], match_mode="any"
    )
    all_ids = await _ids(
        session_factory, user_id=USER_A, queries=["椭圆", "焦点"], match_mode="all"
    )
    assert set(all_ids) == {"mastery:ellipse", "mastery:focus"}
    assert set(all_ids) < set(any_ids)


async def test_search_fixed_order_is_total_and_matches_python_key(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    """权重降序 → updated_at 降序 → memory_id 升序；SQL 与 Python 排序键完全一致。"""
    rows = await _search(session_factory, user_id=USER_A, queries=["椭圆"])
    sql_order = [str(row["memory_id"]) for row in rows]
    assert sql_order == [
        "mastery:ellipse",  # 权重 2，NOW
        "mastery:a-tie",  # 权重 2，NOW-2d，同刻按 memory_id 升序
        "mastery:b-tie",  # 权重 2，NOW-2d
        "mastery:kw",  # 权重 1
        "mastery:desc",  # 权重 0，NOW
        "mastery:focus",  # 权重 0，NOW-1d
    ]
    python_order = [
        str(row["memory_id"])
        for row in sorted(rows, key=lambda row: tool_hit_sort_key(row, ["椭圆"]))
    ]
    assert python_order == sql_order


async def test_search_limit_takes_full_order_prefix_for_truncated(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    """服务层用 max_results + 1 判定 truncated，因此 LIMIT 必须是全序前 N 条。"""
    page = await _ids(session_factory, user_id=USER_A, queries=["椭圆"], limit=3)
    assert page == ["mastery:ellipse", "mastery:a-tie", "mastery:b-tie"]
    four = await _ids(session_factory, user_id=USER_A, queries=["椭圆"], limit=4)
    assert len(four) == 4  # 第 4 条存在 → 服务层据此置 truncated=true


async def test_search_escapes_like_metacharacters(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    """% / _ 按字面匹配：裸 % 不能变成"匹配全部"。"""
    assert await _ids(session_factory, user_id=USER_A, queries=["100%"]) == ["mastery:pct"]
    assert await _ids(session_factory, user_id=USER_A, queries=["%"]) == ["mastery:pct"]
    assert await _ids(session_factory, user_id=USER_A, queries=["0%_"]) == ["mastery:pct"]


async def test_search_and_projection_are_user_scoped(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    assert await _ids(session_factory, user_id=USER_B, queries=["椭圆"]) == ["mastery:other"]
    assert "mastery:other" not in await _ids(session_factory, user_id=USER_A, queries=["椭圆"])
    async with session_factory() as session:
        projection = await fetch_index_projection(session, user_id=USER_A)
    assert {str(row["memory_id"]) for row in projection} == {
        str(row[1]) for row in SEED if row[0] == USER_A
    }
    assert all(set(row) == {"memory_id", "title", "summary", "keywords"} for row in projection)
    assert [str(row["memory_id"]) for row in projection] == sorted(
        str(row["memory_id"]) for row in projection
    )


async def test_projection_limit_is_bounded_deterministic_prefix(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    """review I-5：目录投影的 LIMIT 由 SQL 执行，且取的是 memory_id 升序的确定性前缀。

    prime 用「上限 + 1」判定截断——超出部分绝不能进内存；顺序必须与不带 LIMIT 时一致，
    否则"保留前 N 条"的语义在 Python 侧无法复现。
    """
    async with session_factory() as session:
        full = await fetch_index_projection(session, user_id=USER_A)
        limited = await fetch_index_projection(session, user_id=USER_A, limit=3)
        one_more = await fetch_index_projection(session, user_id=USER_A, limit=4)
    assert len(limited) == 3
    assert [str(row["memory_id"]) for row in limited] == [str(row["memory_id"]) for row in full][:3]
    # 多取一条即可判定"还有更多"，不必把整份注册表读出来
    assert len(one_more) == 4


# ---------------------------------------------------------------------------
# memory.read：活动版本 + 删除抑制
# ---------------------------------------------------------------------------


async def _write_active_version(
    *,
    memory_service: MemoryService,
    store: LocalMarkdownStore,
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    memory_id: str,
    content: str,
) -> None:
    """写一个不可变活动版本（不走 graph，直接落存储 + 文档指针）。"""
    stored = await store.write_immutable_version(
        user_id=user_id, memory_id=memory_id, version=1, content=content.encode()
    )
    topic_key = memory_id.removeprefix("mastery:")
    async with session_factory() as session:
        async with session.begin():
            await docs_repo.upsert_document(
                session,
                user_id=user_id,
                memory_id=memory_id,
                memory_type="mastery",
                topic_key=topic_key,
                topic_title=topic_key,
                logical_path=f"mastery/{topic_key}.md",
            )
            await docs_repo.set_active_version(
                session,
                user_id=user_id,
                memory_id=memory_id,
                active_version=1,
                active_storage_key=stored.storage_key,
                active_checksum=stored.checksum,
            )


async def test_read_uses_active_version_not_current_materialization(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    memory_service: MemoryService,
) -> None:
    """§2.4 D3②：读活动版本；current/ 物化副本被篡改也不影响读路径。"""
    memory_id = "mastery:椭圆"
    await _write_active_version(
        memory_service=memory_service,
        store=store,
        session_factory=session_factory,
        user_id=USER_A,
        memory_id=memory_id,
        content="v1\n# 椭圆\n活动版本正文",
    )
    # 篡改 current/ 物化副本：读路径不得使用它
    await store.materialize_current(
        user_id=USER_A, memory_id=memory_id, content="被篡改的副本".encode()
    )
    loaded = await memory_service.read_active_content(user_id=USER_A, memory_id=memory_id)
    assert loaded is not None
    version, checksum, content = loaded
    assert version == 1
    assert content == "v1\n# 椭圆\n活动版本正文"
    assert checksum != "" and len(checksum) == 64


async def test_read_suppressed_after_delete(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    memory_service: MemoryService,
) -> None:
    """已删除（tombstone + quarantine）文档读不到：与 get_learner/get_mastery 同语义。"""
    memory_id = "mastery:待删除"
    await _write_active_version(
        memory_service=memory_service,
        store=store,
        session_factory=session_factory,
        user_id=USER_A,
        memory_id=memory_id,
        content="v1\n# 待删除\n正文",
    )
    assert await memory_service.read_active_content(user_id=USER_A, memory_id=memory_id) is not None

    deleted_at = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            await docs_repo.tombstone_document(
                session,
                user_id=USER_A,
                memory_id=memory_id,
                deleted_version=1,
                deleted_at=deleted_at,
                tombstone_until=deleted_at + timedelta(days=30),
            )
    await store.move_to_quarantine(
        user_id=USER_A,
        memory_id=memory_id,
        deleted_version=1,
        deleted_at_epoch=int(deleted_at.timestamp()),
    )
    assert await memory_service.read_active_content(user_id=USER_A, memory_id=memory_id) is None


async def test_read_active_content_is_user_scoped(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    memory_service: MemoryService,
) -> None:
    memory_id = "mastery:私有"
    await _write_active_version(
        memory_service=memory_service,
        store=store,
        session_factory=session_factory,
        user_id=USER_A,
        memory_id=memory_id,
        content="v1\n# 私有\n正文",
    )
    assert await memory_service.read_active_content(user_id=USER_B, memory_id=memory_id) is None
