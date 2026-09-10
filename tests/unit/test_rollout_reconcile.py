"""Rollout reconcile 单元测试（memory-rebuild §5.4 Phase 2 验收）。

覆盖五类 finding 各至少一条：

1. ``sealed_object_missing``：manifest 已 sealed 但对象被删除；
2. ``sealed_checksum_mismatch``：对象存在但内容被篡改；
3. ``open_local_missing``：open 段在本地根目录下没有段文件；
4. ``orphan_object``：对象已上传但没有任何 manifest 行引用；
5. ``pointer_out_of_range``：``conversation_messages`` 指针字节越界 / ordinal 越界。

同时覆盖两类"不该报"的负例（一致的 sealed 段、本地文件仍在的 open 段）、
``repair(dry_run=True)`` 无副作用、``repair(dry_run=False)`` 只标记被授权的两种段。

数据库侧用最小假会话工厂（不依赖 PostgreSQL），对象存储用
:class:`FakeRolloutObjectStore`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from backend.conversation.rollout.file_naming import segment_dir, segment_filename
from backend.conversation.rollout.object_store import (
    FakeRolloutObjectStore,
    build_object_key,
)
from backend.conversation.rollout.reconcile import (
    FAILURE_PREFIX,
    KIND_OPEN_LOCAL_MISSING,
    KIND_ORPHAN_OBJECT,
    KIND_POINTER_OUT_OF_RANGE,
    KIND_SEALED_CHECKSUM_MISMATCH,
    KIND_SEALED_OBJECT_MISSING,
    ReconcileReport,
    RolloutReconciler,
)

_THREAD_ID = uuid4()
_CREATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 最小假数据库
# ---------------------------------------------------------------------------


class _FakeResult:
    """只实现 reconcile 用到的三种结果形态。"""

    def __init__(self, rows: list[Any], *, rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def mappings(self) -> _FakeResult:
        return self

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeTransaction:
    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """按 SQL 文本分发到预设数据；非 reconcile 语句直接失败以暴露误用。"""

    def __init__(self, db: _FakeDb) -> None:
        self._db = db

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = " ".join(str(statement).split())
        bound = dict(params or {})
        self._db.calls.append((sql, bound))
        if "conversation_messages" in sql:
            return _FakeResult(list(self._db.pointer_rows))
        if "DISTINCT object_key" in sql:
            after = bound.get("after")
            page = int(bound.get("page", 1000))
            keys = sorted(k for k in self._db.referenced_keys if after is None or k > after)
            return _FakeResult([str(key) for key in keys[:page]])
        if "SET status = 'deleted'" in sql:
            self._db.marked_deleted.append(bound.get("segment_id"))
            return _FakeResult([], rowcount=1)
        if "conversation_rollout_segments" in sql:
            return _FakeResult(list(self._db.manifest_rows))
        raise AssertionError(f"未预期的 SQL：{sql}")


class _FakeDb:
    """假会话工厂：记录会话创建次数与全部 SQL，便于断言 dry-run 无写操作。"""

    def __init__(self) -> None:
        self.manifest_rows: list[dict[str, Any]] = []
        self.pointer_rows: list[dict[str, Any]] = []
        self.referenced_keys: set[str] = set()
        self.marked_deleted: list[Any] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.session_count = 0

    def __call__(self) -> _FakeSession:
        self.session_count += 1
        return _FakeSession(self)

    def set_manifests(self, rows: list[dict[str, Any]]) -> None:
        self.manifest_rows = rows
        self.referenced_keys = {str(row["object_key"]) for row in rows if row.get("object_key")}


def _segment_row(
    *,
    segment_id: UUID,
    status: str,
    ordinal_start: int,
    object_key: str | None = None,
    sha256: str | None = None,
    byte_size: int | None = None,
    ordinal_end: int | None = None,
) -> dict[str, Any]:
    """构造一行 manifest（字段名与 rollout_manifests._COLUMNS 一致）。"""
    return {
        "segment_id": segment_id,
        "thread_id": _THREAD_ID,
        "turn_id": uuid4(),
        "ordinal_start": ordinal_start,
        "ordinal_end": ordinal_end,
        "object_key": object_key,
        "object_etag": "etag" if status == "sealed" else None,
        "sha256": sha256,
        "byte_size": byte_size,
        "status": status,
        "created_at": _CREATED,
        "sealed_at": _CREATED if status == "sealed" else None,
        "deleted_at": None,
    }


class _Scenario:
    """一套同时命中五类 finding 的夹具事实。"""

    def __init__(self, tmp_path: Path) -> None:
        self.store = FakeRolloutObjectStore()
        self.db = _FakeDb()
        self.reconciler = RolloutReconciler(
            session_factory=self.db,
            object_store=self.store,
            rollout_root=tmp_path,
        )
        self.tmp_path = tmp_path

    async def build(self) -> ReconcileReport:
        """写入对象与 manifest 行，再跑一次扫描。"""
        # 一致段：对象存在且 sha256 匹配（负例，不应产生 finding）。
        self.ok_segment = uuid4()
        self.ok_data = b'{"ordinal":0,"type":"thread_meta"}\n'
        self.ok_key = build_object_key(
            thread_created_at=_CREATED,
            thread_id=_THREAD_ID,
            ordinal_start=0,
            segment_id=self.ok_segment,
        )
        await self.store.put_immutable(key=self.ok_key, data=self.ok_data)

        # 第 1 类：sealed 但对象不存在（先上传再 drop，模拟对象丢失）。
        self.missing_segment = uuid4()
        self.missing_key = build_object_key(
            thread_created_at=_CREATED,
            thread_id=_THREAD_ID,
            ordinal_start=10,
            segment_id=self.missing_segment,
        )
        await self.store.put_immutable(key=self.missing_key, data=b'{"ordinal":10}\n')
        self.store.drop(self.missing_key)

        # 第 2 类：sealed 但对象内容被篡改。
        self.corrupt_segment = uuid4()
        self.corrupt_data = b'{"ordinal":20}\n'
        self.corrupt_key = build_object_key(
            thread_created_at=_CREATED,
            thread_id=_THREAD_ID,
            ordinal_start=20,
            segment_id=self.corrupt_segment,
        )
        await self.store.put_immutable(key=self.corrupt_key, data=self.corrupt_data)
        self.store.corrupt(self.corrupt_key)

        # 第 3 类：open 段本地热段缺失（不建文件）。
        self.orphan_open_segment = uuid4()
        # 负例：open 段本地文件仍在。
        self.hot_segment = uuid4()
        thread_dir = segment_dir(
            root=self.tmp_path, thread_created_at=_CREATED, thread_id=_THREAD_ID
        )
        thread_dir.mkdir(parents=True, exist_ok=True)
        hot_path = thread_dir / segment_filename(ordinal_start=30, segment_id=self.hot_segment)
        hot_path.write_bytes(b'{"ordinal":30}\n')

        # 第 4 类：已上传未登记（没有任何 manifest 行引用）。
        self.orphan_key = build_object_key(
            thread_created_at=_CREATED,
            thread_id=_THREAD_ID,
            ordinal_start=90,
            segment_id=uuid4(),
        )
        await self.store.put_immutable(key=self.orphan_key, data=b'{"ordinal":90}\n')
        # 非 rollouts/ 前缀的对象不属于 rollout 对账范围。
        await self.store.put_immutable(key="community/images/cover.png", data=b"png")

        self.db.set_manifests(
            [
                _segment_row(
                    segment_id=self.ok_segment,
                    status="sealed",
                    ordinal_start=0,
                    ordinal_end=0,
                    object_key=self.ok_key,
                    sha256=_sha256(self.ok_data),
                    byte_size=len(self.ok_data),
                ),
                _segment_row(
                    segment_id=self.missing_segment,
                    status="sealed",
                    ordinal_start=10,
                    ordinal_end=11,
                    object_key=self.missing_key,
                    sha256=_sha256(b'{"ordinal":10}\n'),
                    byte_size=15,
                ),
                _segment_row(
                    segment_id=self.corrupt_segment,
                    status="sealed",
                    ordinal_start=20,
                    ordinal_end=20,
                    object_key=self.corrupt_key,
                    sha256=_sha256(self.corrupt_data),
                    byte_size=len(self.corrupt_data),
                ),
                _segment_row(segment_id=self.orphan_open_segment, status="open", ordinal_start=40),
                _segment_row(segment_id=self.hot_segment, status="open", ordinal_start=30),
            ]
        )

        # 第 5 类：两条越界指针 + 一条合法指针。
        self.db.pointer_rows = [
            {
                "message_id": uuid4(),
                "segment_id": self.ok_segment,
                "rollout_ordinal": 0,
                "rollout_byte_offset_start": 0,
                "rollout_byte_offset_end": len(self.ok_data) + 5,
                "thread_id": _THREAD_ID,
                "object_key": self.ok_key,
                "ordinal_start": 0,
                "ordinal_end": 0,
                "byte_size": len(self.ok_data),
                "segment_status": "sealed",
            },
            {
                "message_id": uuid4(),
                "segment_id": self.missing_segment,
                "rollout_ordinal": 5,
                "rollout_byte_offset_start": 0,
                "rollout_byte_offset_end": 3,
                "thread_id": _THREAD_ID,
                "object_key": self.missing_key,
                "ordinal_start": 10,
                "ordinal_end": 11,
                "byte_size": 15,
                "segment_status": "sealed",
            },
            {
                "message_id": uuid4(),
                "segment_id": self.ok_segment,
                "rollout_ordinal": 0,
                "rollout_byte_offset_start": 0,
                "rollout_byte_offset_end": len(self.ok_data),
                "thread_id": _THREAD_ID,
                "object_key": self.ok_key,
                "ordinal_start": 0,
                "ordinal_end": 0,
                "byte_size": len(self.ok_data),
                "segment_status": "sealed",
            },
        ]
        return await self.reconciler.scan(limit=100)


def _sha256(data: bytes) -> str:
    from backend.conversation.rollout.codec import sha256_hex

    return sha256_hex(data)


# ---------------------------------------------------------------------------
# 扫描：五类 finding
# ---------------------------------------------------------------------------


async def test_scan_classifies_all_five_kinds(tmp_path: Path) -> None:
    scenario = _Scenario(tmp_path)
    report = await scenario.build()

    assert report.counts() == {
        KIND_SEALED_OBJECT_MISSING: 1,
        KIND_SEALED_CHECKSUM_MISMATCH: 1,
        KIND_OPEN_LOCAL_MISSING: 1,
        KIND_ORPHAN_OBJECT: 1,
        KIND_POINTER_OUT_OF_RANGE: 2,
    }
    assert report.ok is False

    missing = report.of_kind(KIND_SEALED_OBJECT_MISSING)[0]
    assert missing.segment_id == scenario.missing_segment
    assert missing.thread_id == _THREAD_ID
    assert missing.object_key == scenario.missing_key
    assert missing.detail

    corrupt = report.of_kind(KIND_SEALED_CHECKSUM_MISMATCH)[0]
    assert corrupt.segment_id == scenario.corrupt_segment
    assert corrupt.object_key == scenario.corrupt_key
    assert "sha256" in corrupt.detail

    local_missing = report.of_kind(KIND_OPEN_LOCAL_MISSING)[0]
    assert local_missing.segment_id == scenario.orphan_open_segment
    assert local_missing.object_key is None

    orphan = report.of_kind(KIND_ORPHAN_OBJECT)[0]
    assert orphan.object_key == scenario.orphan_key
    assert orphan.segment_id is None

    # 指针类必须带上 message_id 便于定位到具体消息。
    assert all(
        finding.message_id is not None for finding in report.of_kind(KIND_POINTER_OUT_OF_RANGE)
    )
    details = "；".join(finding.detail for finding in report.of_kind(KIND_POINTER_OUT_OF_RANGE))
    assert "rollout_byte_offset_end" in details
    assert "rollout_ordinal" in details


async def test_scan_ignores_consistent_segments_and_non_rollout_objects(tmp_path: Path) -> None:
    """负例：内容一致的 sealed 段、本地文件仍在的 open 段、非 rollouts/ 对象都不报。"""
    scenario = _Scenario(tmp_path)
    report = await scenario.build()

    flagged_segments = {finding.segment_id for finding in report.findings}
    assert (
        scenario.ok_segment not in flagged_segments or report.count(KIND_POINTER_OUT_OF_RANGE) >= 1
    )
    # ok_segment 只可能因指针越界出现，绝不出现在对象/本地段三类里。
    assert scenario.ok_segment not in {
        finding.segment_id
        for finding in report.of_kind(KIND_SEALED_OBJECT_MISSING)
        + report.of_kind(KIND_SEALED_CHECKSUM_MISMATCH)
        + report.of_kind(KIND_OPEN_LOCAL_MISSING)
    }
    assert scenario.hot_segment not in flagged_segments
    assert all(finding.object_key != "community/images/cover.png" for finding in report.findings)


async def test_scan_clean_workspace_reports_ok(tmp_path: Path) -> None:
    """全部一致时报告为空，且 counts 仍列出五种 kind（值为 0）。"""
    store = FakeRolloutObjectStore()
    db = _FakeDb()
    segment_id = uuid4()
    data = b'{"ordinal":0}\n'
    key = build_object_key(
        thread_created_at=_CREATED, thread_id=_THREAD_ID, ordinal_start=0, segment_id=segment_id
    )
    await store.put_immutable(key=key, data=data)
    db.set_manifests(
        [
            _segment_row(
                segment_id=segment_id,
                status="sealed",
                ordinal_start=0,
                ordinal_end=0,
                object_key=key,
                sha256=_sha256(data),
                byte_size=len(data),
            )
        ]
    )
    reconciler = RolloutReconciler(session_factory=db, object_store=store, rollout_root=tmp_path)

    report = await reconciler.scan(limit=100)

    assert report.ok is True
    assert report.findings == []
    assert report.summary() == "未发现不一致"
    assert set(report.counts().values()) == {0}
    assert set(report.counts()) == set(ReconcileReport().counts())


# ---------------------------------------------------------------------------
# 修复
# ---------------------------------------------------------------------------


async def test_repair_dry_run_has_no_side_effects(tmp_path: Path) -> None:
    scenario = _Scenario(tmp_path)
    report = await scenario.build()
    calls_before = len(scenario.db.calls)
    keys_before = scenario.store.keys()

    actions = await scenario.reconciler.repair(report, dry_run=True)

    # dry-run 绝不触碰数据库与对象存储。
    assert len(scenario.db.calls) == calls_before
    assert scenario.db.marked_deleted == []
    assert scenario.store.keys() == keys_before
    # 只报告两种可自动修复的段，并说明其余不自动修复。
    assert any(str(scenario.missing_segment) in action for action in actions)
    assert any(str(scenario.orphan_open_segment) in action for action in actions)
    assert any("孤儿对象" in action for action in actions)
    assert any(KIND_SEALED_CHECKSUM_MISMATCH in action for action in actions)
    assert any(KIND_POINTER_OUT_OF_RANGE in action for action in actions)
    assert any("dry-run" in action for action in actions)


async def test_repair_apply_marks_only_authorized_segments(tmp_path: Path) -> None:
    scenario = _Scenario(tmp_path)
    report = await scenario.build()
    keys_before = scenario.store.keys()

    actions = await scenario.reconciler.repair(report, dry_run=False)

    # 只有"对象缺失的 sealed 段"与"本地文件缺失的 open 段"被标记 deleted。
    assert sorted(str(item) for item in scenario.db.marked_deleted) == sorted(
        [str(scenario.missing_segment), str(scenario.orphan_open_segment)]
    )
    # 篡改段绝不被自动标记；对象存储里的孤儿对象绝不被自动删除。
    assert str(scenario.corrupt_segment) not in {str(item) for item in scenario.db.marked_deleted}
    assert scenario.store.keys() == keys_before
    assert all(not action.startswith(FAILURE_PREFIX) for action in actions)
    assert any("已把" in action for action in actions)


async def test_repair_clean_report_returns_no_actions(tmp_path: Path) -> None:
    store = FakeRolloutObjectStore()
    db = _FakeDb()
    reconciler = RolloutReconciler(session_factory=db, object_store=store, rollout_root=tmp_path)

    actions = await reconciler.repair(ReconcileReport(), dry_run=True)

    assert actions == []
    assert db.calls == []
