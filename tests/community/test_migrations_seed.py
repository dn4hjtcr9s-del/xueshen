"""Community 迁移与板块 seed 测试（方案 §7.1/§14.2，v1.6 冻结）。

- 迁移幂等：重复 upgrade head 不产生副作用；
- 板块 seed：固定 UUID、冻结文案、sort_order，与 contracts/domain.py 常量一致；
- 迁移表结构：§7.1–§7.7 的表、索引与约束存在。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from backend.community.contracts.domain import BOARDS_SEED


def test_migration_is_idempotent(community_session_factory) -> None:
    """重复 upgrade head 幂等（§14.2：迁移必须可重复执行）。"""
    command.upgrade(Config("community_alembic.ini"), "head")


async def test_boards_seed_matches_frozen_contract(community_session_factory) -> None:
    """§7.1 冻结：固定 UUID、名称/描述/sort_order 与 domain.py 常量一致。"""
    async with community_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT board_id, slug, name, description, sort_order, status "
                        "FROM community_boards ORDER BY sort_order"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == len(BOARDS_SEED)
    for row, (board_id, slug, name, description, sort_order) in zip(rows, BOARDS_SEED, strict=True):
        assert str(row["board_id"]) == board_id
        assert row["slug"] == slug
        assert row["name"] == name
        assert row["description"] == description
        assert row["sort_order"] == sort_order
        assert row["status"] == "active"


async def test_boards_seed_rerun_does_not_drift(community_session_factory) -> None:
    """ON CONFLICT DO UPDATE 重放不改变冻结字段（幂等 seed，§7.1）。"""
    command.upgrade(Config("community_alembic.ini"), "head")
    async with community_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT slug, name, description, sort_order FROM community_boards "
                        "WHERE slug = 'linear-algebra'"
                    )
                )
            )
            .mappings()
            .one()
        )
    _, slug, name, description, sort_order = BOARDS_SEED[0]
    assert row["slug"] == slug
    assert row["name"] == name
    assert row["description"] == description
    assert row["sort_order"] == sort_order


async def test_core_tables_exist(community_session_factory) -> None:
    """§7.1–§7.7 全部表存在。"""
    tables = {
        "community_boards",
        "community_posts",
        "community_replies",
        "community_post_likes",
        "community_outbox",
        "community_idempotency_requests",
        "community_notifications",
    }
    async with community_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            )
            .scalars()
            .all()
        )
    assert tables.issubset(set(rows))


async def test_outbox_idempotency_key_unique(community_session_factory) -> None:
    """§7.5：outbox 幂等键唯一约束生效（重复插入必须失败）。"""
    insert = text(
        "INSERT INTO community_outbox "
        "(event_id, event_type, aggregate_type, aggregate_id, user_id, payload, idempotency_key) "
        "VALUES (:a, :t, :ag, :aid, :u, '{}'::jsonb, :k)"
    )
    params = {
        "a": uuid4(),
        "t": "community.post_created",
        "ag": "post",
        "aid": str(uuid4()),
        "u": uuid4(),
        "k": "community:community.post_created:x",
    }
    async with community_session_factory() as session:
        await session.execute(insert, params)
        await session.commit()
        with pytest.raises(Exception) as excinfo:
            await session.execute(insert, {**params, "a": uuid4()})
            await session.commit()
        assert "duplicate key" in str(excinfo.value)


async def test_boards_seed_uuid_derivation(community_session_factory) -> None:
    """§7.1：seed UUID 与 UUIDv5(namespace, community-board:{slug}) 派生一致。"""
    from backend.community.contracts.domain import board_id_for_slug

    async with community_session_factory() as session:
        rows = (
            (await session.execute(text("SELECT board_id, slug FROM community_boards")))
            .mappings()
            .all()
        )
    for row in rows:
        assert row["board_id"] == board_id_for_slug(row["slug"])
