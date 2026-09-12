"""Rollout Recorder 单元测试（memory-rebuild §5.3 Phase 1 验收 / §5.11）。

逐条对应 §5.3 的验收清单：
- 空 turn 不建文件；首个持久化记录才物化；
- ordinal 严格递增且 thread 级连续，重启后不复用已确认序号；
- 每行可独立解码，非法 payload 不污染后续行；
- flush ack 只在真正写入并 flush 后返回；
- 写失败降级不拖垮调用方，不出现半行；
- 关闭 flag（recorder 为 None）时 record_rollout 完全 no-op。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.conversation.graph.state import SystemClock, SystemIdGenerator
from backend.conversation.rollout import codec as codec_module
from backend.conversation.rollout import recorder as recorder_module
from backend.conversation.rollout.recorder import (
    RolloutRecorder,
    read_last_ordinal,
    record_rollout,
)

_CREATED = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def _recorder(tmp_path: Path, *, queue_size: int = 256) -> RolloutRecorder:
    return RolloutRecorder(
        root=tmp_path,
        clock=SystemClock(),
        id_generator=SystemIdGenerator(),
        logger=logging.getLogger("test.rollout"),
        queue_size=queue_size,
    )


def _started_payload(turn_id: uuid.UUID) -> dict[str, object]:
    return {
        "turn_id": str(turn_id),
        "request_id": "req-1",
        "started_at": datetime.now(UTC).isoformat(),
    }


async def _read_segment(recorder: RolloutRecorder) -> list[tuple[int, str]]:
    handle = recorder.active_segment
    assert handle is not None
    records = list(codec_module.iter_records(handle.path.read_bytes()))
    return [(record.ordinal, record.type) for record in records]


# ---------------------------------------------------------------------------
# 段生命周期
# ---------------------------------------------------------------------------


async def test_idle_turn_creates_no_file(tmp_path: Path) -> None:
    """空 turn 不创建空文件（§5.3 验收第一条）。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id = uuid.uuid4(), uuid.uuid4()
    handle = await recorder.open_turn(
        thread_id=thread_id,
        turn_id=uuid.uuid4(),
        user_id=user_id,
        thread_created_at=_CREATED,
    )
    assert handle is not None
    await recorder.close_turn()
    assert handle.materialized is False
    assert not handle.path.exists()
    await recorder.aclose()


async def test_first_record_materializes_file_with_thread_meta_first(tmp_path: Path) -> None:
    """首个记录才物化，且 thread_meta 是段内首行、ordinal 最小。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    handle = await recorder.open_turn(
        thread_id=thread_id, turn_id=turn_id, user_id=user_id, thread_created_at=_CREATED
    )
    assert handle is not None
    assert not handle.path.exists()

    assert await recorder.record("turn_started", payload=_started_payload(turn_id)) is True
    await recorder.flush()

    assert handle.path.exists()
    assert await _read_segment(recorder) == [(0, "thread_meta"), (1, "turn_started")]
    await recorder.close_turn()
    await recorder.aclose()


async def test_ordinals_strictly_increase_within_segment(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    thread_id, user_id = uuid.uuid4(), uuid.uuid4()
    turn_id = uuid.uuid4()
    handle = await recorder.open_turn(
        thread_id=thread_id, turn_id=turn_id, user_id=user_id, thread_created_at=_CREATED
    )
    assert handle is not None
    for _ in range(5):
        await recorder.record("turn_started", payload=_started_payload(turn_id))
    await recorder.flush()

    ordinals = [ordinal for ordinal, _ in await _read_segment(recorder)]
    assert ordinals == sorted(ordinals)
    assert len(set(ordinals)) == len(ordinals)
    await recorder.close_turn()
    await recorder.aclose()


async def test_ordinal_continues_across_turns_of_same_thread(tmp_path: Path) -> None:
    """ordinal 是 **thread 级**连续（§1.5「文件级单调递增」，文件=逻辑 thread 文件）。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id = uuid.uuid4(), uuid.uuid4()

    first_turn = uuid.uuid4()
    first = await recorder.open_turn(
        thread_id=thread_id, turn_id=first_turn, user_id=user_id, thread_created_at=_CREATED
    )
    assert first is not None
    await recorder.record("turn_started", payload=_started_payload(first_turn))
    await recorder.close_turn()
    first_max = first.next_ordinal - 1

    second_turn = uuid.uuid4()
    second = await recorder.open_turn(
        thread_id=thread_id, turn_id=second_turn, user_id=user_id, thread_created_at=_CREATED
    )
    assert second is not None
    assert second.ordinal_start == first_max + 1, "新段必须从该 thread 已有最大 ordinal + 1 开始"
    await recorder.close_turn()
    await recorder.aclose()


async def test_restart_does_not_reuse_acknowledged_ordinals(tmp_path: Path) -> None:
    """进程重启（新建 recorder）后不复用已确认写入的 ordinal。"""
    thread_id, user_id = uuid.uuid4(), uuid.uuid4()
    first_recorder = _recorder(tmp_path)
    turn_id = uuid.uuid4()
    await first_recorder.open_turn(
        thread_id=thread_id, turn_id=turn_id, user_id=user_id, thread_created_at=_CREATED
    )
    await first_recorder.record("turn_started", payload=_started_payload(turn_id))
    await first_recorder.close_turn()
    await first_recorder.aclose()

    second_recorder = _recorder(tmp_path)
    handle = await second_recorder.open_turn(
        thread_id=thread_id, turn_id=uuid.uuid4(), user_id=user_id, thread_created_at=_CREATED
    )
    assert handle is not None
    assert handle.ordinal_start == 2, "重启后应从既有段的最大 ordinal + 1 继续"
    await second_recorder.close_turn()
    await second_recorder.aclose()


async def test_single_active_segment_rejects_concurrent_open(tmp_path: Path) -> None:
    """单一活动段（Phase 1 决策）：并发 open 返回 None 而不是覆盖。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id = uuid.uuid4(), uuid.uuid4()
    first = await recorder.open_turn(
        thread_id=thread_id, turn_id=uuid.uuid4(), user_id=user_id, thread_created_at=_CREATED
    )
    assert first is not None
    second = await recorder.open_turn(
        thread_id=thread_id, turn_id=uuid.uuid4(), user_id=user_id, thread_created_at=_CREATED
    )
    assert second is None
    await recorder.close_turn()
    await recorder.aclose()


# ---------------------------------------------------------------------------
# flush ack 与崩溃语义
# ---------------------------------------------------------------------------


async def test_flush_ack_implies_data_on_disk(tmp_path: Path) -> None:
    """flush ack 返回时，此前入队的行必须已经真正写入并 flush。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    handle = await recorder.open_turn(
        thread_id=thread_id, turn_id=turn_id, user_id=user_id, thread_created_at=_CREATED
    )
    assert handle is not None
    await recorder.record("turn_started", payload=_started_payload(turn_id))
    await recorder.flush()

    # ack 之后：文件存在、内容完整、无半行
    data = handle.path.read_bytes()
    assert data.endswith(b"\n")
    assert codec_module.has_truncated_tail(data) is False
    assert len(list(codec_module.iter_records(data))) == 2
    await recorder.close_turn()
    await recorder.aclose()


async def test_aclose_drains_pending_records(tmp_path: Path) -> None:
    """worker 停机前 aclose：队列中已入队但未 flush 的记录也要落盘。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    handle = await recorder.open_turn(
        thread_id=thread_id, turn_id=turn_id, user_id=user_id, thread_created_at=_CREATED
    )
    assert handle is not None
    for _ in range(20):
        await recorder.record("turn_started", payload=_started_payload(turn_id))
    await recorder.aclose()

    records = list(codec_module.iter_records(handle.path.read_bytes()))
    assert len(records) == 21, "thread_meta + 20 条全部落盘"
    assert codec_module.has_truncated_tail(handle.path.read_bytes()) is False


async def test_write_failure_degrades_without_corrupting_or_hanging(tmp_path: Path) -> None:
    """写入必然失败（段路径被目录占据）时：降级、不抛错、flush 不永久阻塞、无半行。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    handle = await recorder.open_turn(
        thread_id=thread_id, turn_id=turn_id, user_id=user_id, thread_created_at=_CREATED
    )
    assert handle is not None
    # 用目录占住段路径：open(...,"ab") 与 reopen 都会失败
    handle.path.mkdir(parents=True)

    assert await recorder.record("turn_started", payload=_started_payload(turn_id)) is True
    await recorder.flush()  # 必须返回，不能挂起
    assert recorder.degraded is True
    # 降级后不再接受新记录
    assert await recorder.record("turn_started", payload=_started_payload(turn_id)) is False
    await recorder.close_turn()
    await recorder.aclose()


async def test_record_rejected_by_policy_does_not_poison_following_lines(tmp_path: Path) -> None:
    """非法 payload 被拒后，后续合法记录仍能正常落盘（不污染后续行）。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await recorder.open_turn(
        thread_id=thread_id, turn_id=turn_id, user_id=user_id, thread_created_at=_CREATED
    )
    assert await recorder.record("turn_started", payload={"request_id": "missing-turn"}) is False
    assert await recorder.record("turn_started", payload=_started_payload(turn_id)) is True
    await recorder.flush()

    records = list(codec_module.iter_records(recorder.active_segment.path.read_bytes()))  # type: ignore[union-attr]
    assert [record.type for record in records] == ["thread_meta", "turn_started"]
    assert [record.ordinal for record in records] == [0, 1], "被拒记录不得留下序号空洞"
    await recorder.close_turn()
    await recorder.aclose()


async def test_turn_completed_is_recorded_at_most_once(tmp_path: Path) -> None:
    """一个段最多一条 turn_completed：finalize 写过后 runner 不得再补 failed。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await recorder.open_turn(
        thread_id=thread_id, turn_id=turn_id, user_id=user_id, thread_created_at=_CREATED
    )
    completed = {
        "turn_id": str(turn_id),
        "status": "completed",
        "degraded_flags": [],
        "completed_at": datetime.now(UTC).isoformat(),
    }
    assert await recorder.record("turn_completed", payload=completed) is True
    assert await recorder.record("turn_completed", payload=completed) is False
    await recorder.flush()

    types = [
        record.type
        for record in codec_module.iter_records(recorder.active_segment.path.read_bytes())
    ]  # type: ignore[union-attr]
    assert types.count("turn_completed") == 1
    await recorder.close_turn()
    await recorder.aclose()


async def test_thread_meta_cannot_be_recorded_explicitly(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    thread_id, user_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await recorder.open_turn(
        thread_id=thread_id, turn_id=turn_id, user_id=user_id, thread_created_at=_CREATED
    )
    assert (
        await recorder.record(
            "thread_meta",
            payload={
                "thread_id": str(thread_id),
                "user_id": str(user_id),
                "created_at": _CREATED.isoformat(),
                "graph_version": "x",
            },
        )
        is False
    )
    await recorder.close_turn()
    await recorder.aclose()


# ---------------------------------------------------------------------------
# 降级只作用于本 turn（review-2 新发现 5）
# ---------------------------------------------------------------------------


class _FlakySealer:
    """登记先失败、后成功的 sealer 替身（模拟一次瞬时 DB 错误）。"""

    def __init__(self, *, failures: int) -> None:
        self.failures = failures
        self.register_calls = 0

    async def resolve_resume(self, **kwargs: object) -> object:
        from backend.conversation.rollout.sealer import NewSegment

        return NewSegment(ordinal_start=0)

    async def register_open(self, **kwargs: object) -> object:
        from backend.conversation.rollout.sealer import RegisterResult, RegistrationStatus

        self.register_calls += 1
        if self.failures > 0:
            self.failures -= 1
            return RegisterResult(status=RegistrationStatus.failed, reason="InjectedError")
        return RegisterResult(status=RegistrationStatus.inserted)

    async def discard_open(self, **kwargs: object) -> bool:
        return True

    async def seal(self, request: object) -> object:
        from backend.conversation.rollout.sealer import SealResult

        return SealResult(sealed=True)


def _recorder_with_sealer(tmp_path: Path, sealer: object) -> RolloutRecorder:
    return RolloutRecorder(
        root=tmp_path,
        clock=SystemClock(),
        id_generator=SystemIdGenerator(),
        logger=logging.getLogger("test.rollout"),
        sealer=sealer,
    )


async def test_registration_failure_degrades_only_this_turn(tmp_path: Path) -> None:
    """新发现 5：一次瞬时登记失败只让**本 turn**停止记录，下一个 turn 必须能重新写。

    旧实现把 ``_degraded`` 当成进程级开关（永不重置），一次瞬时 DB 错误会静默停掉该
    worker 上所有 thread 的 rollout 写入，直到进程重启。
    """
    sealer = _FlakySealer(failures=1)
    recorder = _recorder_with_sealer(tmp_path, sealer)
    thread_id, user_id = uuid.uuid4(), uuid.uuid4()

    # 第一个 turn：登记失败 → 本 turn 降级、不写行
    turn_1 = uuid.uuid4()
    handle = await recorder.open_turn(
        thread_id=thread_id, turn_id=turn_1, user_id=user_id, thread_created_at=_CREATED
    )
    assert handle is not None
    assert await recorder.record("turn_started", payload=_started_payload(turn_1)) is True
    await recorder.flush()
    assert recorder.degraded is True
    assert recorder.degraded_reason == "manifest_register_failed"
    assert await recorder.record("turn_started", payload=_started_payload(turn_1)) is False
    await recorder.close_turn()

    # 第二个 turn：**同一个 recorder** 必须恢复正常（降级收敛到单个 turn）
    turn_2 = uuid.uuid4()
    handle_2 = await recorder.open_turn(
        thread_id=thread_id, turn_id=turn_2, user_id=user_id, thread_created_at=_CREATED
    )
    assert handle_2 is not None, "降级不得泄漏到下一个 turn"
    assert recorder.degraded is False
    assert recorder.degraded_reason is None
    assert await recorder.record("turn_started", payload=_started_payload(turn_2)) is True
    await recorder.flush()
    assert handle_2.path.is_file(), "第二个 turn 必须真的重新落盘"
    await recorder.close_turn()
    await recorder.aclose()


async def test_write_failure_degrades_only_this_turn(tmp_path: Path) -> None:
    """新发现 5（写路径同理）：磁盘故障降级也要在下一个 turn 清零。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id = uuid.uuid4(), uuid.uuid4()

    broken_turn = uuid.uuid4()
    broken = await recorder.open_turn(
        thread_id=thread_id, turn_id=broken_turn, user_id=user_id, thread_created_at=_CREATED
    )
    assert broken is not None
    broken.path.mkdir(parents=True)  # 用目录占住段路径 → 写必然失败
    assert await recorder.record("turn_started", payload=_started_payload(broken_turn)) is True
    await recorder.flush()
    assert recorder.degraded is True
    await recorder.close_turn()

    healthy_turn = uuid.uuid4()
    healthy = await recorder.open_turn(
        thread_id=thread_id, turn_id=healthy_turn, user_id=user_id, thread_created_at=_CREATED
    )
    assert healthy is not None
    assert recorder.degraded is False
    assert await recorder.record("turn_started", payload=_started_payload(healthy_turn)) is True
    await recorder.flush()
    assert healthy.path.is_file()
    await recorder.close_turn()
    await recorder.aclose()


# ---------------------------------------------------------------------------
# 背压
# ---------------------------------------------------------------------------


async def test_full_queue_drops_instead_of_blocking_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """队列写满且 writer 不消费时，record 超时丢弃并返回 False（不拖垮 turn）。"""
    monkeypatch.setattr(recorder_module, "QUEUE_PUT_TIMEOUT_SECONDS", 0.05)
    # 容量 2 = 首条记录所需的 thread_meta + 该记录本身；此后队列无人消费即写满
    recorder = _recorder(tmp_path, queue_size=2)
    # 不启动 writer，使队列无人消费
    monkeypatch.setattr(recorder, "start", lambda: None)
    thread_id, user_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await recorder.open_turn(
        thread_id=thread_id, turn_id=turn_id, user_id=user_id, thread_created_at=_CREATED
    )
    results = [
        await recorder.record("turn_started", payload=_started_payload(turn_id)) for _ in range(4)
    ]
    assert results[0] is True, "thread_meta + 首条记录刚好填满容量 2"
    assert False in results, "队列占满后必须出现丢弃（False）而不是永久阻塞"


# ---------------------------------------------------------------------------
# 未装配 recorder 时的 no-op
# ---------------------------------------------------------------------------


async def test_record_rollout_is_noop_without_recorder() -> None:
    """flag 关闭时 runtime.rollout_recorder 为 None，record_rollout 直接 no-op。"""

    class _Runtime:
        rollout_recorder = None

    assert await record_rollout(_Runtime(), "turn_started", {"any": "thing"}) is False


# ---------------------------------------------------------------------------
# 尾部 ordinal 反查
# ---------------------------------------------------------------------------


def test_read_last_ordinal_ignores_truncated_tail(tmp_path: Path) -> None:
    """半行不参与 ordinal 计算，否则重启后会从一个"半条记录"的序号续写。"""
    path = tmp_path / "seg.jsonl"
    path.write_bytes(b'{"ordinal": 5, "type": "thread_meta"}\n{"ordinal": 6, "ty')
    assert read_last_ordinal(path) == 5


def test_read_last_ordinal_returns_none_for_empty_or_broken(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert read_last_ordinal(empty) is None

    broken = tmp_path / "broken.jsonl"
    broken.write_bytes(b'{"no-ordinal": true}\n')
    assert read_last_ordinal(broken) is None

    assert read_last_ordinal(tmp_path / "missing.jsonl") is None
