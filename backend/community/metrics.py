"""Community 指标（方案 §12.3 冻结，D42）。

与 backend/memory/metrics.py 同模式：prometheus-client 注册到同一默认
REGISTRY，/metrics 出口不动（backend/app.py 已挂载）。指标名均为
community_ 前缀，与 memory_ 无冲突。
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

#: Outbox pending 总量（按事件类型）
community_outbox_pending_total = Gauge(
    "community_outbox_pending_total",
    "Community outbox 中 pending/retry_wait 事件数",
    ["event_type"],
)

#: Outbox 最老事件年龄（秒）
community_outbox_oldest_age_seconds = Gauge(
    "community_outbox_oldest_age_seconds",
    "Community outbox 最老事件年龄（秒）",
    ["event_type"],
)

#: 证据投递结果（按 activity_type/status）
community_activity_publish_total = Counter(
    "community_activity_publish_total",
    "Community activity 投递总数",
    ["activity_type", "status"],
)

#: 证据投递延迟
community_activity_publish_latency_seconds = Histogram(
    "community_activity_publish_latency_seconds",
    "Community activity 投递延迟（秒）",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

#: Memory 侧 source read 结果
community_memory_source_read_total = Counter(
    "community_memory_source_read_total",
    "Memory → Community source read 结果",
    ["result"],
)

#: source deletion 投递延迟
community_source_deletion_lag_seconds = Histogram(
    "community_source_deletion_lag_seconds",
    "Community source deletion 投递延迟（秒）",
    buckets=(1, 5, 15, 60, 300, 900, 1800),
)

#: 帖子/回复创建（按板块 slug）
community_post_created_total = Counter(
    "community_post_created_total",
    "Community 帖子创建数",
    ["board"],
)
community_reply_created_total = Counter(
    "community_reply_created_total",
    "Community 回复创建数",
    ["board"],
)

#: API 请求（route/status；与 memory_http_requests_total 对称）
community_api_requests_total = Counter(
    "community_api_requests_total",
    "Community API 请求数",
    ["route", "status"],
)
