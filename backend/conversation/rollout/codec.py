"""Rollout JSONL 编解码（memory-rebuild §5.3 Phase 1）。

对齐 codex ``rollout/src/recorder.rs::JsonlWriter::write_rollout_item`` 的行语义：
UTF-8、一条记录一行、行尾 ``\\n``（含最后一行）、每行可独立解码。

**截断尾行**：逐行 write+flush 的代价是进程可能在写到一半时被杀，于是文件末尾可能
出现"半行"。半行既不完整也不可解析，重放时必须能识别出来而不是当成合法记录——
:func:`has_truncated_tail` 提供这个判据，:func:`iter_records` 默认跳过它。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

from backend.conversation.contracts.rollout import (
    ROLLOUT_SCHEMA_VERSION as ROLLOUT_SCHEMA_VERSION,
)
from backend.conversation.contracts.rollout import (
    RolloutContractError,
    RolloutRecord,
    parse_rollout_line,
    serialize_rollout_line,
)
from backend.shared.cursor import canonical_json

#: 行终止符（字符串形式；落盘时编码为单字节 0x0a）。
LINE_TERMINATOR = "\n"


def encode_record(record: RolloutRecord) -> bytes:
    """单条记录 → 带行尾的一行 UTF-8 字节。

    行尾必须与 :func:`iter_records` 的切分约定一致；末尾必须有终止符，否则最后一行
    会被 :func:`has_truncated_tail` 判为截断。
    """
    return (serialize_rollout_line(record) + LINE_TERMINATOR).encode("utf-8")


def decode_record(line: str | bytes) -> RolloutRecord:
    """一行（带或不带行尾）→ 记录；非法内容抛 :class:`RolloutContractError`。"""
    text = line.decode("utf-8") if isinstance(line, bytes) else line
    return parse_rollout_line(text)


def iter_records(data: bytes, *, include_truncated_tail: bool = False) -> Iterator[RolloutRecord]:
    """按行重放一个段。

    ``include_truncated_tail=False``（默认）时跳过末尾的半行；置 True 会尝试解析它，
    通常只用于诊断。中间行若无法解析（不是"半行"，而是真损坏）一律抛错，不静默跳过——
    静默跳过会让重放结果看起来"完整"却缺记录。
    """
    if not data:
        return
    text = data.decode("utf-8", errors="strict")
    lines = text.split(LINE_TERMINATOR)
    # split 后末元素是最后一个终止符之后的内容：空串表示文件以终止符结束（无半行）
    tail = lines.pop()
    for line in lines:
        if line:
            yield decode_record(line)
    if tail:
        if include_truncated_tail:
            yield decode_record(tail)
        # 否则：末尾半行，丢弃（由 has_truncated_tail 报告）


def has_truncated_tail(data: bytes) -> bool:
    """文件是否以半行结尾（崩溃窗口的判据）。

    空文件不算截断。Phase 2 的 reconcile 用它区分"正常段"与"崩溃留下的尾段"。
    """
    if not data:
        return False
    return not data.endswith(LINE_TERMINATOR.encode("utf-8"))


def sha256_hex(data: bytes) -> str:
    """段内容哈希（§5.4 封存时写入 manifest 的 sha256）。"""
    return hashlib.sha256(data).hexdigest()


def canonical_record_hash(record: RolloutRecord) -> str:
    """记录的 canonical JSON 哈希（JCS + SHA-256）。

    与 :func:`sha256_hex` 的区别：这里规范化了 JSON（键序、数字格式），因此对同一
    语义的记录稳定；用于幂等与去重判据，不用于段内容校验。
    """
    payload: dict[str, Any] = record.model_dump(mode="json")
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def iter_json_objects(data: bytes) -> Iterator[dict[str, Any]]:
    """只做 JSON 解码不校验契约；供运维工具在契约演进后仍能读旧段。"""
    for line in data.decode("utf-8", errors="replace").split(LINE_TERMINATOR):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RolloutContractError(f"段内存在非法 JSON 行: {exc}") from exc
        if isinstance(obj, dict):
            yield obj
