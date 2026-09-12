"""KG 双路更新写路径单元测试（memory-rebuild §3.5① / §5.9）。

覆盖：
- flag 关闭 → 零副作用、``skipped``；
- 无 KG 映射 → ``skipped``/``no_graph_mapping`` 且不报错；
- 幂等键对同一 ``(batch, memory_id, version)`` 稳定（纯函数断言）；
- 投影证据的 ``direction``/``strength`` 映射正确；
- 依赖注入的假 ``apply_projection`` 抛错 → 返回 ``failed`` 且不向外抛。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest

from backend.memory.services import kg_dual_write
from backend.memory.services.kg_dual_write import (
    DualWriteOutcome,
    after_consolidation,
    dual_write_idempotency_key,
    node_idempotency_key,
    projection_evidence,
    projection_operation_id,
)
from backend.settings import Settings

USER_ID = UUID("00000000-0000-4000-8000-0000000000aa")
BATCH_ID = UUID("00000000-0000-4000-8000-0000000000bb")
BATCH_OPERATION_ID = UUID("00000000-0000-4000-8000-0000000000cc")
NOW = datetime(2026, 9, 12, 3, 30, tzinfo=UTC)
LOGGER = logging.getLogger("test.kg_dual_write")


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test"}
    base.update(overrides)
    return Settings(**base)


def _topic(
    *,
    memory_id: str = "mastery:椭圆",
    topic_key: str = "椭圆",
    version: int = 3,
    checksum: str = "c" * 64,
    direction: str = "positive",
    strength: float = 0.8,
    occurred_at: datetime = NOW,
) -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "topic_key": topic_key,
        "version": version,
        "checksum": checksum,
        "keywords": ["椭圆"],
        "direction": direction,
        "strength": strength,
        "occurred_at": occurred_at,
    }


class _FakeSession:
    """只回答本模块用到的两条 SQL（link 查询 / 审计重放查询）。"""

    def __init__(self, *, links: list[dict[str, Any]], audits: list[list[str]]) -> None:
        self.links = links
        self.audits = audits
        self.queries: list[str] = []
        self.committed = False

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        self.queries.append(sql)
        if "memory_graph_links" in sql:
            return _FakeResult([dict(row) for row in self.links])
        if "graph_state_audit" in sql:
            return _FakeResult([{"evidence_refs": refs} for refs in self.audits])
        return _FakeResult([])

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeTransaction:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._session.committed = True
        return None


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _FakeSessionFactory:
    """按调用次序返回预置 session 的最小 session_factory 替身。"""

    def __init__(self, sessions: list[_FakeSession]) -> None:
        self._sessions = sessions
        self.created = 0

    def __call__(self) -> _FakeSession:
        session = self._sessions[min(self.created, len(self._sessions) - 1)]
        self.created += 1
        return session


@pytest.fixture()
def logger() -> logging.Logger:
    return LOGGER


# ---------------------------------------------------------------------------
# 纯函数：幂等键与投影证据
# ---------------------------------------------------------------------------


def test_idempotency_key_stable_for_same_triple() -> None:
    first = dual_write_idempotency_key(
        batch_operation_id=BATCH_ID, memory_id="mastery:椭圆", version=3
    )
    second = dual_write_idempotency_key(
        batch_operation_id=BATCH_ID, memory_id="mastery:椭圆", version=3
    )
    assert first == second
    assert first == f"kg-dual-write:{BATCH_ID}:mastery:椭圆:3"
    # 换任何一个维度都必须换键（否则重试会互相吞掉）
    assert first != dual_write_idempotency_key(
        batch_operation_id=uuid4(), memory_id="mastery:椭圆", version=3
    )
    assert first != dual_write_idempotency_key(
        batch_operation_id=BATCH_ID, memory_id="mastery:双曲线", version=3
    )
    assert first != dual_write_idempotency_key(
        batch_operation_id=BATCH_ID, memory_id="mastery:椭圆", version=4
    )
    assert node_idempotency_key(first, "n007") == f"{first}:n007"


def test_projection_operation_id_deterministic() -> None:
    key = dual_write_idempotency_key(
        batch_operation_id=BATCH_ID, memory_id="mastery:椭圆", version=3
    )
    assert projection_operation_id(key) == projection_operation_id(key)
    assert projection_operation_id(key) != projection_operation_id(key + ":n002")


def test_projection_evidence_direction_and_strength_mapping() -> None:
    topics = [
        _topic(direction="learning", strength=0.2),
        _topic(direction="positive", strength=0.8),
        _topic(direction="strong_positive", strength=1.4),
        _topic(direction="conflict", strength=0.95),
        _topic(direction="unknown_direction", strength=-1.0),
    ]
    evidence = projection_evidence(
        topics, memory_id="mastery:椭圆", version=3, checksum="a" * 64, now=NOW
    )
    # 最后一条 direction 未知 → 降级 learning，且与第一条 ref 相同被去重，
    # 因此只剩 4 条
    assert [item.direction for item in evidence] == [
        "learning",
        "positive",
        "strong_positive",
        "conflict",
    ]
    assert [item.strength for item in evidence] == [0.2, 0.8, 1.0, 0.95]
    assert all(item.occurred_at == NOW for item in evidence)
    # evidence_ref 内嵌 checksum 前缀，便于回溯 source document checksum；
    # direction 也进 ref，学习与冲突证据不会被去重吞掉
    assert all(":v3:aaaaaaaaaaaa:" in item.evidence_ref for item in evidence)
    assert ":conflict:" in evidence[3].evidence_ref


def test_projection_evidence_filters_other_memories_and_versions() -> None:
    topics = [
        _topic(memory_id="mastery:椭圆", version=3),
        _topic(memory_id="mastery:椭圆", version=4),
        _topic(memory_id="mastery:双曲线", version=3),
        _topic(memory_id="mastery:椭圆", version=3, strength=0.5),
    ]
    evidence = projection_evidence(
        topics, memory_id="mastery:椭圆", version=3, checksum="b" * 64, now=NOW
    )
    # 同 (memory, version, direction, checksum, 时间) 的两条去重成一条，
    # 其他记忆/版本被过滤
    assert len(evidence) == 1
    assert all(item.evidence_ref.startswith("mastery:椭圆:v3:bbbbbbbbbbbb:") for item in evidence)


def test_projection_evidence_naive_datetime_treated_as_utc() -> None:
    naive = datetime(2026, 9, 12, 3, 30)
    evidence = projection_evidence(
        [_topic(occurred_at=naive)], memory_id="mastery:椭圆", version=3, checksum=None, now=NOW
    )
    assert evidence[0].occurred_at.tzinfo is not None


# ---------------------------------------------------------------------------
# flag 与跳过路径
# ---------------------------------------------------------------------------


async def test_flag_disabled_returns_skipped_without_side_effects(
    logger: logging.Logger,
) -> None:
    calls: list[Any] = []

    def _factory() -> Any:
        calls.append("session")
        raise AssertionError("flag 关闭时不得建立任何 session")

    outcome = await after_consolidation(
        user_id=USER_ID,
        batch_operation_id=BATCH_ID,
        operation_id=BATCH_OPERATION_ID,
        changed_topics=[_topic()],
        conflicts=[{"memory_ids": ["mastery:椭圆"], "description": "并列冲突"}],
        settings=_settings(memory_kg_dual_write_enabled=False),
        session_factory=_factory,  # type: ignore[arg-type]
        logger=logger,
    )
    assert outcome.status == "skipped"
    assert outcome.reason == "flag_disabled"
    assert outcome.projection_operation_ids == []
    assert calls == []


async def test_no_graph_mapping_skips_gracefully(logger: logging.Logger) -> None:
    session = _FakeSession(links=[], audits=[])
    factory = _FakeSessionFactory([session])
    outcome = await after_consolidation(
        user_id=USER_ID,
        batch_operation_id=BATCH_ID,
        operation_id=BATCH_OPERATION_ID,
        changed_topics=[_topic()],
        conflicts=[],
        settings=_settings(memory_kg_dual_write_enabled=True),
        session_factory=factory,  # type: ignore[arg-type]
        logger=logger,
    )
    assert outcome == DualWriteOutcome(status="skipped", reason="no_graph_mapping")
    # 只读了一次 link，未触发 projection
    assert len(session.queries) == 1


async def test_empty_changed_topics_skipped(logger: logging.Logger) -> None:
    outcome = await after_consolidation(
        user_id=USER_ID,
        batch_operation_id=BATCH_ID,
        operation_id=BATCH_OPERATION_ID,
        changed_topics=[],
        conflicts=[{"memory_ids": ["mastery:椭圆"], "description": "并列冲突"}],
        settings=_settings(memory_kg_dual_write_enabled=True),
        session_factory=_FakeSessionFactory([_FakeSession(links=[], audits=[])]),  # type: ignore[arg-type]
        logger=logger,
    )
    assert outcome.status == "skipped"
    assert outcome.reason == "no_changed_topics"
    # 冲突标注仍要透传给调用方
    assert outcome.conflicted_memory_ids == ["mastery:椭圆"]


# ---------------------------------------------------------------------------
# 依赖注入的假 apply_projection
# ---------------------------------------------------------------------------


class _FakeProjectionService:
    """替身：记录调用，按需抛错或返回 changed。"""

    calls: ClassVar[list[dict[str, Any]]] = []
    error: ClassVar[Exception | None] = None
    changed: ClassVar[bool] = True

    def __init__(self, *, settings: Settings, session_factory: Any) -> None:
        self.settings = settings
        self.session_factory = session_factory

    async def apply_projection(self, *, operation_id: UUID, user_id: UUID, command: Any) -> Any:
        _FakeProjectionService.calls.append(
            {"operation_id": operation_id, "user_id": user_id, "command": command}
        )
        if _FakeProjectionService.error is not None:
            raise _FakeProjectionService.error

        class _Outcome:
            changed = _FakeProjectionService.changed
            change = None
            warning = None

        return _Outcome()


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    _FakeProjectionService.calls = []
    _FakeProjectionService.error = None
    _FakeProjectionService.changed = True


async def test_projection_failure_returns_failed_without_raising(
    monkeypatch: pytest.MonkeyPatch, logger: logging.Logger
) -> None:
    monkeypatch.setattr(kg_dual_write, "KnowledgeGraphStateService", _FakeProjectionService)
    _FakeProjectionService.error = RuntimeError("kg 侧写库爆炸")
    factory = _FakeSessionFactory([_FakeSession(links=[], audits=[])])
    outcome = await after_consolidation(
        user_id=USER_ID,
        batch_operation_id=BATCH_ID,
        operation_id=BATCH_OPERATION_ID,
        changed_topics=[_topic()],
        conflicts=[],
        settings=_settings(memory_kg_dual_write_enabled=True),
        session_factory=factory,  # type: ignore[arg-type]
        logger=logger,
    )
    # 无映射时连 service 都不会被构造调用
    assert outcome.status == "skipped"


async def test_projection_error_is_caught(
    monkeypatch: pytest.MonkeyPatch, logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(kg_dual_write, "KnowledgeGraphStateService", _FakeProjectionService)
    _FakeProjectionService.error = RuntimeError("kg 侧写库爆炸")
    # 第一次 session 返回 link，第二次（重放检查）返回空审计，第三次由 service 使用
    sessions = [
        _FakeSession(
            links=[{"node_id": "n007", "mapping_method": "exact_alias", "mapping_confidence": 0.9}],
            audits=[],
        ),
        _FakeSession(links=[], audits=[]),
    ]
    factory = _FakeSessionFactory(sessions)
    with caplog.at_level(logging.WARNING):
        outcome = await after_consolidation(
            user_id=USER_ID,
            batch_operation_id=BATCH_ID,
            operation_id=BATCH_OPERATION_ID,
            changed_topics=[_topic()],
            conflicts=[],
            settings=_settings(memory_kg_dual_write_enabled=True),
            session_factory=factory,  # type: ignore[arg-type]
            logger=logger,
        )
    assert outcome.status == "failed"
    assert outcome.reason == "kg_projection_failed"
    assert outcome.projection_operation_ids == []
    assert any("kg_projection_update_failed" in record.message for record in caplog.records)
    assert len(_FakeProjectionService.calls) == 1


async def test_applied_projection_records_deterministic_operation_id(
    monkeypatch: pytest.MonkeyPatch, logger: logging.Logger
) -> None:
    monkeypatch.setattr(kg_dual_write, "KnowledgeGraphStateService", _FakeProjectionService)
    sessions = [
        _FakeSession(
            links=[
                {"node_id": "n007", "mapping_method": "model_candidate", "mapping_confidence": 0.7}
            ],
            audits=[],
        ),
        _FakeSession(links=[], audits=[]),
    ]
    outcome = await after_consolidation(
        user_id=USER_ID,
        batch_operation_id=BATCH_ID,
        operation_id=BATCH_OPERATION_ID,
        changed_topics=[_topic()],
        conflicts=[],
        settings=_settings(memory_kg_dual_write_enabled=True),
        session_factory=_FakeSessionFactory(sessions),  # type: ignore[arg-type]
        logger=logger,
    )
    assert outcome.status == "applied"
    main_key = dual_write_idempotency_key(
        batch_operation_id=BATCH_ID, memory_id="mastery:椭圆", version=3
    )
    expected_anchor = projection_operation_id(node_idempotency_key(main_key, "n007"))
    # 返回给调用方的是确定性幂等键派生的追溯锚 id
    assert outcome.projection_operation_ids == [expected_anchor]
    # 审计行挂在真实存在的**批次 operation** 上（graph_state_audit.operation_id 有外键）
    assert _FakeProjectionService.calls[0]["operation_id"] == BATCH_OPERATION_ID
    command = _FakeProjectionService.calls[0]["command"]
    assert command.source_memory_id == "mastery:椭圆"
    assert command.source_version == 3
    assert command.node_id == "n007"
    assert command.mapping_method == "model_candidate"
    assert len(command.evidence) == 1


async def test_replay_skips_second_application(
    monkeypatch: pytest.MonkeyPatch, logger: logging.Logger
) -> None:
    """审计已覆盖同一份证据 → 第二次调用不再走 apply_projection。"""
    monkeypatch.setattr(kg_dual_write, "KnowledgeGraphStateService", _FakeProjectionService)
    first_evidence = projection_evidence(
        [_topic()], memory_id="mastery:椭圆", version=3, checksum="c" * 64, now=NOW
    )
    sessions = [
        _FakeSession(
            links=[{"node_id": "n007", "mapping_method": "exact_alias", "mapping_confidence": 0.9}],
            audits=[],
        ),
        _FakeSession(links=[], audits=[[item.evidence_ref for item in first_evidence]]),
    ]
    outcome = await after_consolidation(
        user_id=USER_ID,
        batch_operation_id=BATCH_ID,
        operation_id=BATCH_OPERATION_ID,
        changed_topics=[_topic()],
        conflicts=[],
        settings=_settings(memory_kg_dual_write_enabled=True),
        session_factory=_FakeSessionFactory(sessions),  # type: ignore[arg-type]
        logger=logger,
    )
    assert outcome.status == "skipped"
    assert outcome.reason == "no_effective_change"
    assert _FakeProjectionService.calls == []
