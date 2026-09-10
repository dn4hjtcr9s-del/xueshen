"""Turn 级 Rollout Recorder（memory-rebuild §1.5 / §5.3 Phase 1）。

对齐 codex ``rollout/src/recorder.rs``：有界队列 + 后台 writer task + 延迟建文件 +
逐行 write/flush + flush ack + 写失败重开重试。

**为什么后台 task**：图节点在关键产出点记录，若每条都同步落盘，磁盘延迟会直接进入
turn 关键路径；队列把"节点产出"与"落盘"解耦，同时用有界队列提供背压。

**核心不变式**：

1. 一行一条记录、行尾 ``\\n``、逐行 flush——最小化崩溃窗口；
2. ``ordinal`` 在 **thread 内**严格递增，重启后不复用已确认写入的序号；
3. **空 turn 不建文件**：文件在首次真正写入时才物化，且此时才写 ``thread_meta``；
4. ``flush()`` 的 ack 只在**此前所有入队行都真正写入并 flush 后**才返回；
5. 写失败先按"截断到最后一次成功 flush 的偏移 → 重开 → 重试一次"处理，
   仍失败则整体降级：记指标、丢弃后续记录，**绝不拖垮 turn**（此阶段 checkpoint
   与 conversation_messages 仍是恢复/读取权威）；
6. 同一时刻只允许一个活动段（Phase 1 决策：worker 单并发执行 turn）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from backend.conversation.contracts.rollout import (
    RolloutRecord,
    RolloutRecordType,
)
from backend.conversation.rollout.codec import encode_record
from backend.conversation.rollout.file_naming import (
    list_segment_paths,
    segment_dir,
    segment_path,
)
from backend.conversation.rollout.policy import ensure_persistable

#: 队列写满后的等待上限；超时即丢弃该条并计入降级指标。
#: 理由（§1.5「降级不拖垮 turn」）：磁盘挂起时不能把图执行一起挂住。
QUEUE_PUT_TIMEOUT_SECONDS = 5.0

#: 写入 ``thread_meta.graph_version`` 的图版本标识。
#: 文档未规定取值来源，Phase 1 用固定串；图拓扑变更时应同步升版。
CONVERSATION_GRAPH_VERSION = "conversation-graph-v1"

#: 反查"最后一个已写 ordinal"时从文件尾部读取的字节数上限。
_TAIL_READ_BYTES = 64 * 1024

#: ``thread_meta`` 由 recorder 自动写入，不接受调用方显式记录。
_AUTO_MANAGED_TYPES = frozenset({"thread_meta"})


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new_uuid(self) -> UUID: ...


@dataclass(slots=True)
class RolloutSegmentHandle:
    """一个已开启的 turn 级段。"""

    segment_id: UUID
    thread_id: UUID
    user_id: UUID
    turn_id: UUID
    path: Path
    ordinal_start: int
    next_ordinal: int = 0
    lines_written: int = 0
    bytes_written: int = 0
    materialized: bool = False
    guard_triggered: bool = False
    meta_ordinal: int | None = None
    turn_completed_recorded: bool = False

    def allocate_ordinal(self) -> int:
        """分配下一个 ordinal（同段内由调用方串行保证）。"""
        ordinal = self.next_ordinal
        self.next_ordinal += 1
        return ordinal


@dataclass(slots=True)
class _Envelope:
    """队列元素：一条待写记录，或一个顺序屏障（flush/close）。"""

    kind: Literal["line", "flush", "close"]
    line: bytes | None = None
    record_type: str | None = None
    ack: asyncio.Future[None] | None = None


def read_last_ordinal(path: Path) -> int | None:
    """读取段文件中最后一个**完整**行的 ordinal；空文件/全为半行时返回 None。

    只从尾部读取固定字节数：ordinal 单调递增，最大序号必然在最后一行，无需全文扫描。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, size - _TAIL_READ_BYTES))
            data = handle.read()
    except OSError:
        return None
    if not data.endswith(b"\n"):
        # 末尾半行不参与：截到最后一条完整记录
        boundary = data.rfind(b"\n")
        if boundary < 0:
            return None
        data = data[: boundary + 1]
    lines = [line for line in data.split(b"\n") if line]
    if not lines:
        return None
    try:
        obj = json.loads(lines[-1])
        return int(obj["ordinal"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


class RolloutRecorder:
    """thread 级 ordinal 分配 + turn 级段写入。"""

    def __init__(
        self,
        *,
        root: str | Path,
        clock: Clock,
        id_generator: IdGenerator,
        logger: logging.Logger,
        queue_size: int = 256,
        segment_max_bytes: int = 16_777_216,
    ) -> None:
        self._root = Path(root)
        self._clock = clock
        self._ids = id_generator
        self._logger = logger
        self._queue: asyncio.Queue[_Envelope] = asyncio.Queue(maxsize=queue_size)
        self._segment_max_bytes = segment_max_bytes
        self._writer_task: asyncio.Task[None] | None = None
        self._file: Any = None
        self._active: RolloutSegmentHandle | None = None
        self._committed_bytes = 0
        self._degraded = False
        self._closed = False
        self._pending_acks: set[asyncio.Future[None]] = set()

    # ------------------------------------------------------------------
    # 只读属性（指标 / 测试用）
    # ------------------------------------------------------------------

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def active_segment(self) -> RolloutSegmentHandle | None:
        return self._active

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动后台 writer task（幂等）。"""
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = asyncio.create_task(self._writer_loop())

    async def aclose(self) -> None:
        """drain 队列并停止 writer；worker 停机前必须调用。"""
        if self._closed:
            return
        self._closed = True
        task = self._writer_task
        if task is None or task.done():
            await self._close_file()
            self._active = None
            return
        ack: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending_acks.add(ack)
        try:
            await self._queue.put(_Envelope(kind="close", ack=ack))
            await ack
        except Exception:
            self._logger.warning("rollout recorder 关闭时未能完成 drain", exc_info=True)
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                self._logger.warning("rollout writer task 退出异常", exc_info=True)
            self._writer_task = None
            await self._close_file()
            self._active = None

    # ------------------------------------------------------------------
    # 段开关
    # ------------------------------------------------------------------

    async def open_turn(
        self,
        *,
        thread_id: UUID,
        turn_id: UUID,
        user_id: UUID,
        thread_created_at: datetime,
    ) -> RolloutSegmentHandle | None:
        """开启本 turn 的段；降级/已关闭/已有活动段时返回 None。

        ``ordinal_start`` = 该 thread 已有最大 ordinal + 1（thread 级连续，§1.5）。
        Phase 1 无 manifest，靠扫描本地段目录确定；Phase 2 接入 manifest 后改走索引。
        """
        if self._degraded or self._closed:
            return None
        if self._active is not None:
            # 单一活动段：worker 单并发，出现第二个即契约被破坏 → 降级而非覆盖
            self._logger.warning(
                "rollout 已有活动段，拒绝并发开启: active_turn=%s new_turn=%s",
                self._active.turn_id,
                turn_id,
            )
            return None
        self.start()
        directory = segment_dir(
            root=self._root, thread_created_at=thread_created_at, thread_id=thread_id
        )
        ordinal_start = self._next_ordinal_start(directory)
        segment_id = self._ids.new_uuid()
        handle = RolloutSegmentHandle(
            segment_id=segment_id,
            thread_id=thread_id,
            user_id=user_id,
            turn_id=turn_id,
            path=segment_path(
                root=self._root,
                thread_created_at=thread_created_at,
                thread_id=thread_id,
                ordinal_start=ordinal_start,
                segment_id=segment_id,
            ),
            ordinal_start=ordinal_start,
            next_ordinal=ordinal_start,
        )
        self._active = handle
        self._committed_bytes = 0
        return handle

    def _next_ordinal_start(self, directory: Path) -> int:
        """扫描该 thread 已有段，返回"最大 ordinal + 1"。"""
        segments = list_segment_paths(directory)
        if not segments:
            return 0
        ordinal_start, _segment_id, last_path = segments[-1]
        last_ordinal = read_last_ordinal(last_path)
        if last_ordinal is None:
            # 最后一段没有完整行（崩溃留下的空/半行文件）：回退到其 ordinal_start
            return ordinal_start
        return last_ordinal + 1

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------

    async def record(
        self,
        record_type: str,
        *,
        payload: dict[str, Any],
        turn_id: UUID | None = None,
    ) -> bool:
        """记录一条；返回是否成功入队。

        失败（策略拒绝 / 降级 / 队列超时）一律返回 False **不抛错**：rollout 是旁路，
        不能让它把 turn 打挂（§5.3）。
        """
        handle = self._active
        if handle is None or self._degraded or self._closed:
            return False
        if record_type == "turn_completed" and handle.turn_completed_recorded:
            # 不变式：一个段最多一条 turn_completed。正常路径由 finalize 写入；
            # 图在 finalize 之后仍失败时 runner 会补写 failed，这里拦住重复终态。
            self._logger.warning("本 turn 已有 turn_completed，忽略重复记录")
            return False
        if record_type in _AUTO_MANAGED_TYPES:
            self._logger.warning("%s 由 recorder 自动写入，忽略显式记录", record_type)
            return False
        try:
            normalized = ensure_persistable(record_type, payload)
        except Exception as exc:
            self._logger.warning("rollout 记录被策略拒绝: type=%s err=%s", record_type, exc)
            _inc_counter("rollout_records_rejected_total", record_type=record_type)
            return False
        resolved_turn_id = turn_id if turn_id is not None else handle.turn_id
        # 分配顺序：thread_meta 必须先拿到 ordinal，否则它会排在首条业务记录之后，
        # 违反"thread_meta 是段内首行"（§5.3 写入顺序第 1 条）。
        meta_line: bytes | None = None
        if handle.meta_ordinal is None:
            handle.meta_ordinal = handle.allocate_ordinal()
            meta_line = self._build_thread_meta_line(handle)
        ordinal = handle.allocate_ordinal()
        try:
            record = RolloutRecord(
                recorded_at=self._clock.now().astimezone(UTC),
                ordinal=ordinal,
                type=cast(RolloutRecordType, record_type),
                turn_id=resolved_turn_id,
                payload=normalized,
            )
        except Exception as exc:
            # 回滚本次分配，避免序号空洞与"占用了 meta 序号却没写 meta"
            handle.next_ordinal = ordinal
            if meta_line is not None:
                handle.meta_ordinal = None
            self._logger.warning("rollout 记录构造失败: type=%s err=%s", record_type, exc)
            _inc_counter("rollout_records_rejected_total", record_type=record_type)
            return False
        if meta_line is not None and not await self._enqueue_line(meta_line, "thread_meta"):
            return False
        enqueued = await self._enqueue_line(encode_record(record), record_type)
        if enqueued and record_type == "turn_completed":
            handle.turn_completed_recorded = True
        return enqueued

    def _build_thread_meta_line(self, handle: RolloutSegmentHandle) -> bytes:
        """构造段首 ``thread_meta`` 行。"""
        meta = RolloutRecord(
            recorded_at=self._clock.now().astimezone(UTC),
            ordinal=handle.meta_ordinal if handle.meta_ordinal is not None else 0,
            type="thread_meta",
            turn_id=None,
            payload={
                "thread_id": str(handle.thread_id),
                "user_id": str(handle.user_id),
                "created_at": _segment_date(handle).isoformat(),
                "graph_version": CONVERSATION_GRAPH_VERSION,
            },
        )
        return encode_record(meta)

    # ------------------------------------------------------------------
    # 同步屏障
    # ------------------------------------------------------------------

    async def flush(self) -> None:
        """等待此前所有记录真正写入并 flush（§1.5 的 flush ack）。

        writer 已停止或降级时立即返回——调用方不能因为旁路故障而永久阻塞。
        """
        task = self._writer_task
        if task is None or task.done() or self._degraded:
            return
        ack: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending_acks.add(ack)
        try:
            await self._queue.put(_Envelope(kind="flush", ack=ack))
            await ack
        except Exception:
            self._logger.warning("rollout flush 未获确认", exc_info=True)
        finally:
            self._pending_acks.discard(ack)

    async def close_turn(self) -> None:
        """结束本 turn 的段：等 ack → 释放句柄（此后不再写入）。"""
        await self.flush()
        await self._close_file()
        self._active = None
        self._committed_bytes = 0

    # ------------------------------------------------------------------
    # 入队与 writer
    # ------------------------------------------------------------------

    async def _enqueue_line(self, line: bytes, record_type: str) -> bool:
        try:
            await asyncio.wait_for(
                self._queue.put(_Envelope(kind="line", line=line, record_type=record_type)),
                timeout=QUEUE_PUT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            self._logger.warning("rollout 队列写满超时，丢弃记录: type=%s", record_type)
            _inc_counter("rollout_dropped_total", reason="queue_timeout")
            return False
        except Exception:
            return False
        _inc_counter("rollout_records_total", record_type=record_type)
        return True

    async def _writer_loop(self) -> None:
        try:
            while True:
                envelope = await self._queue.get()
                try:
                    if envelope.kind == "close":
                        await self._flush_file()
                        self._resolve_ack(envelope)
                        return
                    if envelope.kind == "flush":
                        await self._flush_file()
                        self._resolve_ack(envelope)
                        continue
                    if envelope.line is not None:
                        await self._write_with_retry(envelope.line)
                except Exception:
                    self._logger.warning(
                        "rollout writer 处理异常，该条已丢弃: type=%s",
                        envelope.record_type,
                        exc_info=True,
                    )
                    self._resolve_ack(envelope)
                finally:
                    self._queue.task_done()
        finally:
            # writer 退出：唤醒所有等待 ack 的调用方，避免永久挂起
            for pending in list(self._pending_acks):
                if not pending.done():
                    pending.set_result(None)
            self._pending_acks.clear()

    def _resolve_ack(self, envelope: _Envelope) -> None:
        if envelope.ack is not None:
            if not envelope.ack.done():
                envelope.ack.set_result(None)
            self._pending_acks.discard(envelope.ack)

    async def _write_with_retry(self, line: bytes) -> None:
        """写一行（含 flush）；失败则截断重开重试一次，仍失败则降级。"""
        try:
            await self._write_once(line)
            return
        except OSError as first_error:
            self._logger.warning("rollout 写入失败，截断后重开重试: %s", first_error)
            _inc_counter("rollout_write_retry_total")
        try:
            await self._close_file()
            await self._reopen_and_truncate()
            await self._write_once(line)
        except Exception as second_error:
            self._degraded = True
            self._logger.error(
                "rollout 写入二次失败，进入降级（后续记录丢弃，不影响 turn）: %s", second_error
            )
            _inc_counter("rollout_write_failed_total")
            await self._close_file()
            raise

    async def _write_once(self, line: bytes) -> None:
        """真正落盘：延迟建文件 → write → flush。"""
        handle = self._active
        if handle is None:
            return
        if self._file is None:
            await asyncio.to_thread(handle.path.parent.mkdir, parents=True, exist_ok=True)
            self._file = await asyncio.to_thread(handle.path.open, "ab")
            handle.materialized = True
            self._committed_bytes = await asyncio.to_thread(lambda: handle.path.stat().st_size)
        file_handle = self._file

        def _do_write() -> None:
            file_handle.write(line)
            file_handle.flush()

        await asyncio.to_thread(_do_write)
        self._committed_bytes += len(line)
        handle.lines_written += 1
        handle.bytes_written += len(line)
        _inc_counter("rollout_records_written_total")
        self._check_size_guard(handle)

    def _check_size_guard(self, handle: RolloutSegmentHandle) -> None:
        """防爆阈值：只告警一次，**不丢行、不拆段**。

        §5.3 要求"达到阈值时记录可观测的 segment_size_guard_triggered，不得在一个 turn
        中静默丢行"。拆段需要显式的 segment 边界记录与重放测试（Phase 2），Phase 1
        只做可观测化。
        """
        if handle.guard_triggered or handle.bytes_written < self._segment_max_bytes:
            return
        handle.guard_triggered = True
        self._logger.warning(
            "rollout 段达到防爆阈值: turn=%s bytes=%s threshold=%s（继续写入，不丢行）",
            handle.turn_id,
            handle.bytes_written,
            self._segment_max_bytes,
        )
        _inc_counter("rollout_segment_guard_triggered_total")

    async def _flush_file(self) -> None:
        file_handle = self._file
        if file_handle is None:
            return
        await asyncio.to_thread(file_handle.flush)

    async def _close_file(self) -> None:
        file_handle = self._file
        self._file = None
        if file_handle is None:
            return

        def _do_close() -> None:
            try:
                file_handle.flush()
            finally:
                file_handle.close()

        try:
            await asyncio.to_thread(_do_close)
        except Exception:
            self._logger.warning("rollout 文件句柄关闭失败", exc_info=True)

    async def _reopen_and_truncate(self) -> None:
        """重开文件并截断到最后一次成功 flush 的偏移，避免半行污染。

        直接重写整行而不截断，会让"失败时已写了一半"的字节与新行拼成一条坏记录；
        截断到已确认偏移后追加，保证任何时刻文件里只有完整行。
        """
        handle = self._active
        if handle is None:
            return

        def _do_reopen() -> Any:
            handle.path.parent.mkdir(parents=True, exist_ok=True)
            reopened = handle.path.open("r+b")
            try:
                reopened.truncate(self._committed_bytes)
                reopened.seek(0, 2)
            except Exception:
                reopened.close()
                raise
            return reopened

        self._file = await asyncio.to_thread(_do_reopen)


def _segment_date(handle: RolloutSegmentHandle) -> datetime:
    """段所在日期目录对应的 UTC 零点。

    首行 ``created_at`` 必须与文件名/目录同源（§1.5 双时间戳语义：时间戳①来自 thread
    创建时间），因此从已算好的路径反推日期，而不是再取一次 now()——否则跨零点运行时
    首行时间会与目录不一致。
    """
    parts = handle.path.parts
    try:
        index = parts.index("threads")
        return datetime(
            int(parts[index + 1]), int(parts[index + 2]), int(parts[index + 3]), tzinfo=UTC
        )
    except (ValueError, IndexError):
        return datetime.now(UTC)


def _inc_counter(name: str, **labels: str) -> None:
    """指标写入失败绝不能影响写入链路，因此整体兜底。"""
    try:
        from backend.conversation import metrics

        metric = getattr(metrics, name, None)
        if metric is None:
            return
        if labels:
            metric.labels(**labels).inc()
        else:
            metric.inc()
    except Exception:
        pass


async def record_rollout(
    runtime: Any,
    record_type: str,
    payload: dict[str, Any],
    *,
    turn_id: UUID | None = None,
) -> bool:
    """节点侧统一记录入口；recorder 未装配时静默 no-op。

    flag 关闭时 ``runtime.rollout_recorder`` 为 None，本函数立即返回，因此
    "关闭 flag 行为完全不变"是构造性成立的。
    """
    recorder = getattr(runtime, "rollout_recorder", None)
    if recorder is None:
        return False
    # recorder 由 composition root 装配（runtime 是 Any），显式 bool() 收敛返回类型
    return bool(await recorder.record(record_type, payload=payload, turn_id=turn_id))


__all__ = [
    "CONVERSATION_GRAPH_VERSION",
    "QUEUE_PUT_TIMEOUT_SECONDS",
    "RolloutRecorder",
    "RolloutSegmentHandle",
    "read_last_ordinal",
    "record_rollout",
]
