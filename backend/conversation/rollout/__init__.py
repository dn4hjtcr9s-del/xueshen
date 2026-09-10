"""Conversation Rollout 本地链路（memory-rebuild §1 / §5.3）。

短期记忆的三层模型：内存（图执行工作态）→ JSONL rollout（唯一事实源、人类可读、
可独立重放）→ PostgreSQL（索引 + 分布式协调）。本包实现第一层到第二层的写入链路。

Phase 1 只使用本地目录，不接对象存储、不写 manifest（那是 Phase 2/3）。feature flag
``conversation_rollout_enabled`` 默认关闭；关闭时 ``runtime.rollout_recorder`` 为 None，
:func:`record_rollout` 立即返回，因此不产生任何行为。

模块分工：
- :mod:`codec`：JSONL 编解码与哈希（行尾、UTF-8、截断尾行识别）；
- :mod:`file_naming`：段文件路径与命名（thread 级目录分片、段序号解析）；
- :mod:`policy`：持久化白名单与脱敏边界（"什么该落盘"）；
- :mod:`recorder`：有界队列 + 后台 writer + flush ack + 重开重试（"怎么写"）。
"""

from __future__ import annotations

from backend.conversation.rollout.codec import (
    LINE_TERMINATOR,
    canonical_record_hash,
    decode_record,
    encode_record,
    has_truncated_tail,
    iter_records,
    sha256_hex,
)
from backend.conversation.rollout.file_naming import (
    SEGMENT_SUFFIX,
    find_segment_dir,
    list_segment_paths,
    parse_segment_filename,
    segment_dir,
    segment_filename,
    segment_path,
)
from backend.conversation.rollout.policy import (
    PERSISTED_RECORD_TYPES,
    RESERVED_RECORD_TYPES,
    RolloutPolicyError,
    ensure_persistable,
    should_persist,
)
from backend.conversation.rollout.recorder import (
    CONVERSATION_GRAPH_VERSION,
    RolloutRecorder,
    RolloutSegmentHandle,
    read_last_ordinal,
    record_rollout,
)

__all__ = [
    "CONVERSATION_GRAPH_VERSION",
    "LINE_TERMINATOR",
    "PERSISTED_RECORD_TYPES",
    "RESERVED_RECORD_TYPES",
    "SEGMENT_SUFFIX",
    "RolloutPolicyError",
    "RolloutRecorder",
    "RolloutSegmentHandle",
    "canonical_record_hash",
    "decode_record",
    "encode_record",
    "ensure_persistable",
    "find_segment_dir",
    "has_truncated_tail",
    "iter_records",
    "list_segment_paths",
    "parse_segment_filename",
    "read_last_ordinal",
    "record_rollout",
    "segment_dir",
    "segment_filename",
    "segment_path",
    "sha256_hex",
    "should_persist",
]
