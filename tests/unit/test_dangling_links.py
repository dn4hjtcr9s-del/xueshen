"""悬空链接两级制单元测试（memory-rebuild §3.2 / §5.9④ / 决议表 E 组）。

单测只覆盖**不需要数据库**的两层：

1. 纯函数（``normalize_link_target`` / ``merge_batch_sightings`` /
   ``resolve_link_target`` / ``link_namespace`` / ``document_dangling_sightings``）；
2. ``record_sightings`` 等 SQL 的**入参与语句契约**（假 session 断言 SQL 文本与绑定
   参数：批内去重只发一条语句、幂等键、恒带 user_id）。

真正的计数/幂等/隔离语义由 ``tests/integration/test_dangling_links.py`` 用真实
PostgreSQL 验证——那部分逻辑写在 SQL 里（``ON CONFLICT`` + ``CASE``），假 session
断言不了行为，只能断言形状。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from backend.memory.persistence import dangling_links as repo
from backend.memory.persistence.dangling_links import (
    MAX_SOURCE_MEMORY_IDS,
    DanglingLinkError,
    document_dangling_sightings,
    link_namespace,
    merge_batch_sightings,
    normalize_link_target,
    resolve_link_target,
)

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
BATCH_ID = UUID("22222222-2222-2222-2222-222222222222")

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0010_memory_dangling_links.py"
)


# ---------------------------------------------------------------------------
# 假 session：断言 SQL 与绑定参数（不连库）
# ---------------------------------------------------------------------------


class _FakeMappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self._rows)


class _FakeSession:
    """只记录 ``(sql, params)``；每次 execute 返回同一批预置行。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._rows = rows or []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        self.calls.append((str(statement), dict(params or {})))
        return _FakeResult(self._rows)


def _sighting(target: str, *source_ids: str, target_key: str | None = None) -> dict[str, Any]:
    return {
        "target": target,
        "target_key": target_key if target_key is not None else target,
        "source_memory_ids": list(source_ids),
    }


# ---------------------------------------------------------------------------
# target_key 规范化：以 normalize_topic_title 的真实语义为准
# ---------------------------------------------------------------------------


def test_normalize_strips_surrounding_whitespace() -> None:
    assert normalize_link_target("  椭圆  ") == "椭圆"
    # 真实语义：控制字符检查发生在 strip **之前**，所以首尾的 \t\n 也会被拒绝
    with pytest.raises(DanglingLinkError):
        normalize_link_target("  椭圆\t\n")


def test_normalize_folds_fullwidth_via_nfkc() -> None:
    # NFKC 把全角折成半角：全角 E / 全角 a,b 都会变成 ASCII
    assert normalize_link_target("Ｅllipse") == "Ellipse"
    assert normalize_link_target("ａｂ") == "ab"
    # 全角空格（U+3000）在 NFKC 下变 ASCII 空格，再被 strip 掉
    assert normalize_link_target("\u3000椭圆\u3000") == "椭圆"


def test_normalize_does_not_change_case() -> None:
    """大小写敏感是真实语义：Ellipse 与 ellipse 是两个不同的键（不是 bug）。"""
    assert normalize_link_target("Ellipse") != normalize_link_target("ellipse")
    assert normalize_link_target("Mastery:椭圆") == "Mastery:椭圆"


def test_normalize_keeps_inner_whitespace() -> None:
    """与 topic_key_from_title 不同：空白**不**折叠成 '-'，内部空格原样保留。"""
    assert normalize_link_target("椭 圆") == "椭 圆"


def test_normalize_rejects_control_characters() -> None:
    with pytest.raises(DanglingLinkError):
        normalize_link_target("椭圆\n抛物线")
    # 零宽空格（U+200B）属于控制/格式字符，同样拒绝
    with pytest.raises(DanglingLinkError):
        normalize_link_target("椭\u200b圆")


def test_normalize_rejects_empty_and_overlong() -> None:
    with pytest.raises(DanglingLinkError):
        normalize_link_target("   ")
    with pytest.raises(DanglingLinkError):
        normalize_link_target("a" * (repo.MAX_TARGET_LENGTH + 1))
    assert normalize_link_target("a" * repo.MAX_TARGET_LENGTH) == "a" * repo.MAX_TARGET_LENGTH


def test_max_source_memory_ids_matches_migration_check() -> None:
    """仓储常量与 0010 迁移 CHECK 是两处独立真相，这里防漂移（同 0009 的教训）。"""
    spec = importlib.util.spec_from_file_location("migration_0010", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.MAX_SOURCE_MEMORY_IDS == MAX_SOURCE_MEMORY_IDS


# ---------------------------------------------------------------------------
# 批内合并：同一 target_key 一批只算 1 次
# ---------------------------------------------------------------------------


def test_merge_same_target_key_once_per_batch() -> None:
    merged = merge_batch_sightings(
        [
            _sighting("椭圆", "mastery:a"),
            _sighting("椭圆", "mastery:b"),
            _sighting("椭圆", "mastery:a"),
        ]
    )
    assert len(merged) == 1
    assert merged[0]["target_key"] == "椭圆"
    # 来源文档合并去重且保序
    assert merged[0]["source_memory_ids"] == ["mastery:a", "mastery:b"]


def test_merge_distinct_targets_kept_separately() -> None:
    merged = merge_batch_sightings([_sighting("椭圆"), _sighting("抛物线")])
    assert [item["target_key"] for item in merged] == ["椭圆", "抛物线"]


def test_merge_uses_normalized_key_so_fullwidth_variants_collapse() -> None:
    merged = merge_batch_sightings(
        [_sighting("Ｅllipse", "mastery:a"), _sighting("Ellipse", "mastery:b")]
    )
    assert len(merged) == 1
    # target 展示文本取首次出现的写法，不随后续批次漂移
    assert merged[0]["target"] == "Ellipse"
    assert merged[0]["source_memory_ids"] == ["mastery:a", "mastery:b"]


def test_merge_caps_and_dedupes_source_ids() -> None:
    merged = merge_batch_sightings(
        [_sighting("椭圆", *[f"mastery:m{i}" for i in range(MAX_SOURCE_MEMORY_IDS + 20)])]
    )
    assert len(merged[0]["source_memory_ids"]) == MAX_SOURCE_MEMORY_IDS


def test_merge_rejects_malformed_entries() -> None:
    with pytest.raises(DanglingLinkError):
        merge_batch_sightings([{"target_key": "椭圆"}])  # 缺 target
    with pytest.raises(DanglingLinkError):
        merge_batch_sightings([{"target": "椭圆"}])  # 缺 target_key
    with pytest.raises(DanglingLinkError):
        merge_batch_sightings([{"target": "椭圆", "target_key": "椭圆", "source_memory_ids": "x"}])
    with pytest.raises(DanglingLinkError):
        merge_batch_sightings([{"target": "椭圆", "target_key": "椭圆", "source_memory_ids": [1]}])
    with pytest.raises(DanglingLinkError):
        merge_batch_sightings([_sighting("椭\u200b圆")])


# ---------------------------------------------------------------------------
# 解析：memory_id → name → aliases（§3.2）
# ---------------------------------------------------------------------------


def test_link_namespace_priority_memory_id_then_name_then_aliases() -> None:
    entries = [
        {
            "memory_id": "mastery:ellipse",
            "title": "椭圆",
            "aliases": ["ellipse", "椭圆形"],
        },
        {
            "memory_id": "mastery:parabola",
            "title": "抛物线",
            # 别名与另一文档的正式名撞车：正式名（name）优先级更高
            "aliases": ["椭圆"],
        },
    ]
    known = link_namespace(entries)
    assert known["mastery:ellipse"] == "mastery:ellipse"  # memory_id 精确
    assert known["椭圆"] == "mastery:ellipse"  # name 胜过别名
    assert known["ellipse"] == "mastery:ellipse"
    assert known["椭圆形"] == "mastery:ellipse"
    assert known["抛物线"] == "mastery:parabola"


def test_link_namespace_memory_id_wins_over_name_collision() -> None:
    # 极端撞车：A 的 name 恰好等于 B 的 memory_id → memory_id 精确优先
    entries = [
        {"memory_id": "mastery:a", "title": "mastery:b", "aliases": []},
        {"memory_id": "mastery:b", "title": "b", "aliases": []},
    ]
    known = link_namespace(entries)
    assert known["mastery:b"] == "mastery:b"


def test_link_namespace_skips_invalid_keys_and_rows_without_memory_id() -> None:
    known = link_namespace(
        [
            {"memory_id": "mastery:a", "title": "椭\u200b圆", "aliases": ["", None, "ok"]},
            {"title": "无 memory_id 的行", "aliases": ["x"]},
        ]
    )
    assert known == {"mastery:a": "mastery:a", "ok": "mastery:a"}


def test_resolve_link_target_returns_none_for_dangling_and_invalid() -> None:
    known = {"椭圆": "mastery:ellipse"}
    assert resolve_link_target(" 椭圆 ", known=known) == "mastery:ellipse"
    assert resolve_link_target("抛物线", known=known) is None  # 悬空（合法）
    assert resolve_link_target("椭\u200b圆", known=known) is None  # 非法也不抛错
    assert resolve_link_target("   ", known=known) is None
    assert resolve_link_target("Ellipse", known=known) is None  # 大小写敏感


# ---------------------------------------------------------------------------
# 文档级收集：直接产出 record_sightings 的入参
# ---------------------------------------------------------------------------


def test_document_dangling_sightings_filters_and_dedupes() -> None:
    known = {"椭圆": "mastery:ellipse", "mastery:ellipse": "mastery:ellipse"}
    sightings = document_dangling_sightings(
        memory_id="mastery:learner",
        links=["椭圆", "抛物线", "抛物线", "椭\u200b圆", "  "],
        known=known,
    )
    assert sightings == [
        {
            "target": "抛物线",
            "target_key": "抛物线",
            "source_memory_ids": ["mastery:learner"],
        }
    ]
    # 产物可直接喂给 record_sightings（形状一致）
    assert merge_batch_sightings(sightings)[0]["target_key"] == "抛物线"


# ---------------------------------------------------------------------------
# SQL 契约（假 session）
# ---------------------------------------------------------------------------


async def test_record_sightings_dedupes_batch_into_single_statement() -> None:
    """同批重复出现只登记 1 行：2 条同 key 的 entry → 只发 1 条 SQL。"""
    row = {"target": "椭圆", "target_key": "椭圆", "status": "candidate", "sighting_batches": 1}
    session = _FakeSession(rows=[row])
    result = await repo.record_sightings(
        session,  # type: ignore[arg-type]
        user_id=USER_ID,
        batch_operation_id=BATCH_ID,
        sightings=[_sighting("椭圆", "mastery:a"), _sighting("椭圆", "mastery:b")],
    )
    assert len(session.calls) == 1
    sql, params = session.calls[0]
    assert "INSERT INTO memory_dangling_links" in sql
    assert "ON CONFLICT (user_id, target_key) DO UPDATE" in sql
    assert params["user_id"] == USER_ID
    assert params["batch_operation_id"] == BATCH_ID
    # 入参已按 key 合并成一条，来源去重
    assert '"target_key": "椭圆"' in params["sightings"]
    assert '"mastery:a"' in params["sightings"] and '"mastery:b"' in params["sightings"]
    assert result == [row]


async def test_record_sightings_sql_carries_idempotency_guard_and_status_filter() -> None:
    """幂等键与"只累计 candidate"必须写在 SQL 里（行为由集成测试验证）。"""
    session = _FakeSession()
    await repo.record_sightings(
        session,  # type: ignore[arg-type]
        user_id=USER_ID,
        batch_operation_id=BATCH_ID,
        sightings=[_sighting("椭圆")],
    )
    sql = session.calls[0][0]
    # 幂等：last_batch_operation_id 等于本批 → CASE 取 0（不 +1）
    assert "memory_dangling_links.last_batch_operation_id = EXCLUDED.last_batch_operation_id" in sql
    assert "THEN 0" in sql and "ELSE 1" in sql
    # dismissed / promoted 不参与累计
    assert "WHERE memory_dangling_links.status = 'candidate'" in sql
    # 来源文档合并有上限
    assert f"LIMIT {MAX_SOURCE_MEMORY_IDS}" in sql


async def test_record_sightings_skips_sql_for_empty_input() -> None:
    session = _FakeSession()
    assert (
        await repo.record_sightings(
            session,  # type: ignore[arg-type]
            user_id=USER_ID,
            batch_operation_id=BATCH_ID,
            sightings=[],
        )
        == []
    )
    assert session.calls == []


async def test_list_candidates_binds_user_and_threshold() -> None:
    session = _FakeSession()
    await repo.list_candidates(session, user_id=USER_ID, min_batches=2, limit=7)  # type: ignore[arg-type]
    sql, params = session.calls[0]
    assert "FROM memory_dangling_links" in sql
    assert "status = 'candidate'" in sql
    assert "sighting_batches >= :min_batches" in sql
    assert params == {"user_id": USER_ID, "min_batches": 2, "limit": 7}


async def test_list_user_links_binds_user_and_optional_status() -> None:
    session = _FakeSession()
    await repo.list_user_links(session, user_id=USER_ID)  # type: ignore[arg-type]
    sql, params = session.calls[0]
    assert "WHERE user_id = :user_id" in sql
    assert params == {"user_id": USER_ID, "status": None}
    await repo.list_user_links(session, user_id=USER_ID, status="promoted")  # type: ignore[arg-type]
    assert session.calls[1][1]["status"] == "promoted"


async def test_mark_promoted_sql_only_touches_own_candidate_rows() -> None:
    session = _FakeSession()
    promoted = await repo.mark_promoted(
        session,  # type: ignore[arg-type]
        user_id=USER_ID,
        target_key="  椭圆 ",
        memory_id="mastery:椭圆",
    )
    sql, params = session.calls[0]
    assert "UPDATE memory_dangling_links" in sql
    assert "WHERE user_id = :user_id" in sql
    assert "AND target_key = :target_key" in sql
    # 只从 candidate 迁移；同一 memory_id 的重复调用同样放行（重试幂等）
    assert "status = 'candidate'" in sql
    assert "status = 'promoted' AND promoted_memory_id = :memory_id" in sql
    # target_key 入库前规范化
    assert params["target_key"] == "椭圆"
    assert params["memory_id"] == "mastery:椭圆"
    assert params["user_id"] == USER_ID
    # 假 session 不是 CursorResult → exec_rowcount 返回 0 → False（真实行为见集成测试）
    assert promoted is False


async def test_every_query_binds_user_id() -> None:
    """用户隔离硬要求：四个持久化 API 的语句与参数都必须带 user_id。"""
    session = _FakeSession()
    await repo.record_sightings(
        session,  # type: ignore[arg-type]
        user_id=USER_ID,
        batch_operation_id=BATCH_ID,
        sightings=[_sighting("椭圆")],
    )
    await repo.list_candidates(session, user_id=USER_ID)  # type: ignore[arg-type]
    await repo.list_user_links(session, user_id=USER_ID)  # type: ignore[arg-type]
    await repo.mark_promoted(
        session,  # type: ignore[arg-type]
        user_id=USER_ID,
        target_key="椭圆",
        memory_id="mastery:椭圆",
    )
    assert len(session.calls) == 4
    for sql, params in session.calls:
        assert "user_id" in sql
        assert params["user_id"] == USER_ID
