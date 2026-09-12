"""memory_dangling_links 集成测试（memory-rebuild §3.2 / §5.9④ / 决议表 E 组）。

两级制的判定全部写在 SQL 里（``ON CONFLICT`` + 幂等 ``CASE`` + ``status`` 过滤），
假 session 只能断言语句形状，**行为必须打真实 PostgreSQL**：

- 0010 迁移真的建出表、UNIQUE、CHECK（含 jsonb 数组上限）；
- 两个不同批次各出现一次 → ``sighting_batches == 2`` 且进入候选；只出现 1 批的不进；
- 同一批次重跑（幂等重试）与并发重跑都不涨计数；
- ``dismissed`` 冻结、``promoted`` 退出候选；
- 跨用户隔离：A 的悬空链接在 B 的任何查询里都不可见。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.persistence import dangling_links as repo

USER_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

BATCH_1 = UUID("11111111-1111-4111-8111-111111111111")
BATCH_2 = UUID("22222222-2222-4222-8222-222222222222")
BATCH_3 = UUID("33333333-3333-4333-8333-333333333333")


def _sighting(target: str, *source_ids: str, target_key: str | None = None) -> dict[str, Any]:
    return {
        "target": target,
        "target_key": target_key if target_key is not None else target,
        "source_memory_ids": list(source_ids),
    }


async def _record(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID = USER_A,
    batch_id: UUID,
    sightings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """一个批次登记一次（独立事务提交，模拟 consolidation 的提交边界）。"""
    async with session_factory() as session:
        async with session.begin():
            return await repo.record_sightings(
                session,
                user_id=user_id,
                batch_operation_id=batch_id,
                sightings=sightings,
            )


async def _fetch(
    session_factory: async_sessionmaker[AsyncSession], *, user_id: UUID, target_key: str
) -> dict[str, Any] | None:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT * FROM memory_dangling_links "
                "WHERE user_id = :user_id AND target_key = :target_key"
            ),
            {"user_id": user_id, "target_key": target_key},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def _count(
    session_factory: async_sessionmaker[AsyncSession], *, user_id: UUID, status: str | None = None
) -> int:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT COUNT(*) FROM memory_dangling_links WHERE user_id = :user_id "
                "AND (CAST(:status AS text) IS NULL OR status = :status)"
            ),
            {"user_id": user_id, "status": status},
        )
        return int(result.scalar_one())


async def _set_status(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    target_key: str,
    status: str,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE memory_dangling_links SET status = :status "
                    "WHERE user_id = :user_id AND target_key = :target_key"
                ),
                {"status": status, "user_id": user_id, "target_key": target_key},
            )


async def _expect_integrity_error(
    session_factory: async_sessionmaker[AsyncSession], sql: str, params: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            async with session.begin():
                await session.execute(text(sql), params)


# ---------------------------------------------------------------------------
# 迁移：表与约束真的生效
# ---------------------------------------------------------------------------

_RAW_INSERT = (
    "INSERT INTO memory_dangling_links ("
    "link_id, user_id, target, target_key, status, sighting_batches, source_memory_ids"
    ") VALUES ("
    ":link_id, :user_id, :target, :target_key, :status, :batches, CAST(:sources AS jsonb)"
    ")"
)


async def test_migration_creates_table_with_unique_and_checks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """0010 建表真的生效：可插入，UNIQUE/CHECK 全部拦得住坏数据。"""
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'memory_dangling_links'"
            )
        )
        columns = {row[0]: row[1] for row in result.all()}
    assert columns["link_id"] == "uuid"
    assert columns["target_key"] == "character varying"
    assert columns["sighting_batches"] == "integer"
    assert columns["source_memory_ids"] == "jsonb"
    assert columns["first_seen_at"] == "timestamp with time zone"

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_RAW_INSERT),
                {
                    "link_id": uuid4(),
                    "user_id": USER_A,
                    "target": "椭圆",
                    "target_key": "椭圆",
                    "status": "candidate",
                    "batches": 1,
                    "sources": '["mastery:a"]',
                },
            )

    # UNIQUE (user_id, target_key)
    await _expect_integrity_error(
        session_factory,
        _RAW_INSERT,
        {
            "link_id": uuid4(),
            "user_id": USER_A,
            "target": "椭圆（重复）",
            "target_key": "椭圆",
            "status": "candidate",
            "batches": 1,
            "sources": "[]",
        },
    )
    # 不同的 user_id 同 target_key 合法（用户隔离维度在唯一键里）
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(_RAW_INSERT),
                {
                    "link_id": uuid4(),
                    "user_id": USER_B,
                    "target": "椭圆",
                    "target_key": "椭圆",
                    "status": "candidate",
                    "batches": 1,
                    "sources": "[]",
                },
            )
    # CHECK status / sighting_batches / source_memory_ids 必须是数组且有上限
    for bad in (
        {"status": "bogus", "batches": 1, "sources": "[]"},
        {"status": "candidate", "batches": -1, "sources": "[]"},
        {"status": "candidate", "batches": 1, "sources": '{"not": "array"}'},
        {
            "status": "candidate",
            "batches": 1,
            "sources": "["
            + ",".join(f'"{i}"' for i in range(repo.MAX_SOURCE_MEMORY_IDS + 1))
            + "]",
        },
    ):
        await _expect_integrity_error(
            session_factory,
            _RAW_INSERT,
            {
                "link_id": uuid4(),
                "user_id": USER_A,
                "target": "坏行",
                "target_key": f"坏行-{bad['status']}-{bad['batches']}-{len(bad['sources'])}",
                **bad,
            },
        )


# ---------------------------------------------------------------------------
# 两级制：1 批只进候选区，≥2 批才进 list_candidates
# ---------------------------------------------------------------------------


async def test_two_batches_reach_threshold_and_one_batch_does_not(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await _record(
        session_factory,
        batch_id=BATCH_1,
        sightings=[_sighting("椭圆", "mastery:a"), _sighting("抛物线", "mastery:a")],
    )
    assert {row["target_key"]: row["sighting_batches"] for row in first} == {"椭圆": 1, "抛物线": 1}

    # 批次 1 之后：两个目标都只是候选，未达建档门槛
    async with session_factory() as session:
        assert await repo.list_candidates(session, user_id=USER_A) == []
        assert len(await repo.list_user_links(session, user_id=USER_A)) == 2

    # 批次 2：只有"椭圆"再次出现 → 达到 2 个批次
    await _record(
        session_factory,
        batch_id=BATCH_2,
        sightings=[_sighting("椭圆", "mastery:b")],
    )

    row = await _fetch(session_factory, user_id=USER_A, target_key="椭圆")
    assert row is not None
    assert row["sighting_batches"] == 2
    assert row["status"] == "candidate"
    assert row["first_batch_operation_id"] == BATCH_1  # 来源批次可回溯（§5.9 验收）
    assert row["last_batch_operation_id"] == BATCH_2
    assert list(row["source_memory_ids"]) == ["mastery:a", "mastery:b"]
    assert row["promoted_memory_id"] is None

    async with session_factory() as session:
        candidates = await repo.list_candidates(session, user_id=USER_A)
        assert [item["target_key"] for item in candidates] == ["椭圆"]
        assert candidates[0]["sighting_batches"] == 2
        # min_batches 可调：3 批门槛下仍不是候选
        assert await repo.list_candidates(session, user_id=USER_A, min_batches=3) == []
        assert await repo.list_candidates(session, user_id=USER_A, min_batches=1) != []


async def test_cross_batch_key_is_normalized_so_variants_share_one_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """target_key 入库前规范化：全角写法与半角写法落在同一行（计数累计到 2）。"""
    await _record(session_factory, batch_id=BATCH_1, sightings=[_sighting("Ｅllipse", "mastery:a")])
    await _record(session_factory, batch_id=BATCH_2, sightings=[_sighting("Ellipse", "mastery:b")])
    row = await _fetch(session_factory, user_id=USER_A, target_key="Ellipse")
    assert row is not None and row["sighting_batches"] == 2
    # 展示文本保持首次出现的写法
    assert row["target"] == "Ellipse"
    assert await _count(session_factory, user_id=USER_A) == 1


# ---------------------------------------------------------------------------
# 幂等：同批重复 / 同批重跑 / 并发重跑
# ---------------------------------------------------------------------------


async def test_duplicate_sightings_in_same_batch_count_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rows = await _record(
        session_factory,
        batch_id=BATCH_1,
        sightings=[
            _sighting("椭圆", "mastery:a"),
            _sighting("椭圆", "mastery:b"),
            _sighting("抛物线", "mastery:a"),
        ],
    )
    assert {row["target_key"]: row["sighting_batches"] for row in rows} == {"椭圆": 1, "抛物线": 1}
    ellipse = await _fetch(session_factory, user_id=USER_A, target_key="椭圆")
    assert ellipse is not None
    # 同批多篇文档只是来源变多，批次数仍是 1
    assert ellipse["sighting_batches"] == 1
    assert list(ellipse["source_memory_ids"]) == ["mastery:a", "mastery:b"]


async def test_same_batch_rerun_does_not_increment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """幂等键 = last_batch_operation_id：同一批次重跑只合并来源，不 +1。"""
    await _record(session_factory, batch_id=BATCH_1, sightings=[_sighting("椭圆", "mastery:a")])
    before = await _fetch(session_factory, user_id=USER_A, target_key="椭圆")
    assert before is not None and before["sighting_batches"] == 1

    # 重跑 1：同一批、同一文档
    await _record(session_factory, batch_id=BATCH_1, sightings=[_sighting("椭圆", "mastery:a")])
    # 重跑 2：同一批、新增来源文档
    await _record(session_factory, batch_id=BATCH_1, sightings=[_sighting("椭圆", "mastery:c")])

    after = await _fetch(session_factory, user_id=USER_A, target_key="椭圆")
    assert after is not None
    assert after["sighting_batches"] == 1  # 不涨
    assert after["last_batch_operation_id"] == BATCH_1
    assert list(after["source_memory_ids"]) == ["mastery:a", "mastery:c"]  # 来源仍合并
    assert after["last_seen_at"] >= before["last_seen_at"]

    # 换一个批次才 +1
    await _record(session_factory, batch_id=BATCH_2, sightings=[_sighting("椭圆", "mastery:d")])
    again = await _fetch(session_factory, user_id=USER_A, target_key="椭圆")
    assert again is not None and again["sighting_batches"] == 2


async def test_concurrent_same_batch_reruns_do_not_increment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """并发重跑同一批次也不涨：判定与累加在同一条 upsert 里，无需额外加锁。"""
    sightings = [[_sighting("椭圆", f"mastery:doc{i}")] for i in range(4)]
    await asyncio.gather(
        *(_record(session_factory, batch_id=BATCH_1, sightings=item) for item in sightings)
    )
    row = await _fetch(session_factory, user_id=USER_A, target_key="椭圆")
    assert row is not None
    assert row["sighting_batches"] == 1
    assert sorted(row["source_memory_ids"]) == [f"mastery:doc{i}" for i in range(4)]


async def test_concurrent_distinct_batches_each_count(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await asyncio.gather(
        _record(session_factory, batch_id=BATCH_1, sightings=[_sighting("椭圆", "mastery:a")]),
        _record(session_factory, batch_id=BATCH_2, sightings=[_sighting("椭圆", "mastery:b")]),
    )
    row = await _fetch(session_factory, user_id=USER_A, target_key="椭圆")
    assert row is not None and row["sighting_batches"] == 2


# ---------------------------------------------------------------------------
# dismissed / promoted 冻结累计
# ---------------------------------------------------------------------------


async def test_dismissed_row_stops_accumulating(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _record(session_factory, batch_id=BATCH_1, sightings=[_sighting("椭圆", "mastery:a")])
    await _set_status(session_factory, user_id=USER_A, target_key="椭圆", status="dismissed")

    updated = await _record(
        session_factory, batch_id=BATCH_2, sightings=[_sighting("椭圆", "mastery:b")]
    )
    assert updated == []  # dismissed 行不进入 DO UPDATE，也不在返回值里
    row = await _fetch(session_factory, user_id=USER_A, target_key="椭圆")
    assert row is not None
    assert row["status"] == "dismissed"
    assert row["sighting_batches"] == 1
    assert row["last_batch_operation_id"] == BATCH_1
    assert list(row["source_memory_ids"]) == ["mastery:a"]

    async with session_factory() as session:
        assert await repo.list_candidates(session, user_id=USER_A, min_batches=1) == []
        dismissed = await repo.list_user_links(session, user_id=USER_A, status="dismissed")
        assert [item["target_key"] for item in dismissed] == ["椭圆"]
        assert await repo.list_user_links(session, user_id=USER_A, status="candidate") == []


async def test_mark_promoted_removes_candidate_and_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _record(session_factory, batch_id=BATCH_1, sightings=[_sighting("椭圆", "mastery:a")])
    await _record(session_factory, batch_id=BATCH_2, sightings=[_sighting("椭圆", "mastery:b")])
    async with session_factory() as session:
        assert len(await repo.list_candidates(session, user_id=USER_A)) == 1

    async with session_factory() as session:
        async with session.begin():
            assert (
                await repo.mark_promoted(
                    session, user_id=USER_A, target_key="椭圆", memory_id="mastery:椭圆"
                )
                is True
            )
    # 幂等重试：同一 (target_key, memory_id) 再调仍是 True（夜间批重试不误判失败）
    async with session_factory() as session:
        async with session.begin():
            assert (
                await repo.mark_promoted(
                    session, user_id=USER_A, target_key="椭圆", memory_id="mastery:椭圆"
                )
                is True
            )
    # 不存在 / 已 promoted 到别的 memory_id → False 且不改动
    async with session_factory() as session:
        async with session.begin():
            assert (
                await repo.mark_promoted(
                    session, user_id=USER_A, target_key="不存在", memory_id="mastery:x"
                )
                is False
            )
            assert (
                await repo.mark_promoted(
                    session, user_id=USER_A, target_key="椭圆", memory_id="mastery:别的"
                )
                is False
            )

    row = await _fetch(session_factory, user_id=USER_A, target_key="椭圆")
    assert row is not None
    assert row["status"] == "promoted"
    assert row["promoted_memory_id"] == "mastery:椭圆"
    async with session_factory() as session:
        assert await repo.list_candidates(session, user_id=USER_A) == []
        assert [
            item["target_key"] for item in await repo.list_user_links(session, user_id=USER_A)
        ] == ["椭圆"]

    # promoted 行同样不再累积（已正式建档，不需要再攒批次）
    updated = await _record(
        session_factory, batch_id=BATCH_3, sightings=[_sighting("椭圆", "mastery:c")]
    )
    assert updated == []
    row = await _fetch(session_factory, user_id=USER_A, target_key="椭圆")
    assert row is not None and row["sighting_batches"] == 2


async def test_mark_promoted_refuses_dismissed_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _record(session_factory, batch_id=BATCH_1, sightings=[_sighting("椭圆", "mastery:a")])
    await _set_status(session_factory, user_id=USER_A, target_key="椭圆", status="dismissed")
    async with session_factory() as session:
        async with session.begin():
            assert (
                await repo.mark_promoted(
                    session, user_id=USER_A, target_key="椭圆", memory_id="mastery:椭圆"
                )
                is False
            )
    row = await _fetch(session_factory, user_id=USER_A, target_key="椭圆")
    assert row is not None and row["status"] == "dismissed"
    assert row["promoted_memory_id"] is None


# ---------------------------------------------------------------------------
# 用户隔离与上限
# ---------------------------------------------------------------------------


async def test_user_isolation_across_all_queries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _record(session_factory, batch_id=BATCH_1, sightings=[_sighting("椭圆", "mastery:a")])
    await _record(session_factory, batch_id=BATCH_2, sightings=[_sighting("椭圆", "mastery:a")])

    async with session_factory() as session:
        # B 用户看不到 A 的任何行，也不能借 mark_promoted 改 A 的状态
        assert await repo.list_user_links(session, user_id=USER_B) == []
        assert await repo.list_candidates(session, user_id=USER_B, min_batches=1) == []
        assert await _count(session_factory, user_id=USER_B) == 0
    async with session_factory() as session:
        async with session.begin():
            assert (
                await repo.mark_promoted(
                    session, user_id=USER_B, target_key="椭圆", memory_id="mastery:椭圆"
                )
                is False
            )
    # A 的行未被 B 的调用影响
    row = await _fetch(session_factory, user_id=USER_A, target_key="椭圆")
    assert row is not None
    assert row["status"] == "candidate" and row["sighting_batches"] == 2


async def test_source_memory_ids_capped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _record(
        session_factory,
        batch_id=BATCH_1,
        sightings=[
            _sighting("椭圆", *[f"mastery:m{i}" for i in range(repo.MAX_SOURCE_MEMORY_IDS + 50)])
        ],
    )
    row = await _fetch(session_factory, user_id=USER_A, target_key="椭圆")
    assert row is not None
    assert len(row["source_memory_ids"]) == repo.MAX_SOURCE_MEMORY_IDS


async def test_invalid_target_is_rejected_before_sql(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """非法目标（控制字符）在 Python 侧被拒，不产生半截写入。"""
    with pytest.raises(repo.DanglingLinkError):
        await _record(session_factory, batch_id=BATCH_1, sightings=[_sighting("椭\u200b圆")])
    assert await _count(session_factory, user_id=USER_A) == 0
