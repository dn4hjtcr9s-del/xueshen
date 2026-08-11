"""Prometheus 指标定义（规格 §22）。

所有指标在此集中定义，/metrics 由 prometheus-client 生成；
任何指标禁止 user_id 标签（§14.8）。本步骤只由 Gateway 写入
memory_operations_total / memory_http_requests_total，其余仪表由
Worker/Scheduler/Graph 在既有或后续步骤中接入。
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

memory_operations_total = Counter(
    "memory_operations_total",
    "记忆操作总数",
    ["type", "status"],
)
memory_operation_duration_seconds = Histogram(
    "memory_operation_duration_seconds",
    "记忆操作执行耗时",
    ["type"],
)
memory_operation_queue_depth = Gauge(
    "memory_operation_queue_depth",
    "按优先级的排队深度",
    ["priority"],
)
memory_operation_oldest_queued_seconds = Gauge(
    "memory_operation_oldest_queued_seconds",
    "最老 queued operation 的等待秒数",
)
memory_operation_retry_total = Counter(
    "memory_operation_retry_total",
    "任务级重试次数",
    ["error_code"],
)
memory_dead_letter_total = Counter(
    "memory_dead_letter_total",
    "dead letter 总数",
    ["type"],
)
memory_review_candidate_total = Counter(
    "memory_review_candidate_total",
    "候选审核总数",
    ["status"],
)
memory_llm_calls_total = Counter(
    "memory_llm_calls_total",
    "LLM 调用次数",
    ["model", "status", "schema"],
)
memory_llm_tokens_total = Counter(
    "memory_llm_tokens_total",
    "LLM token 用量",
    ["model", "direction"],
)
memory_llm_latency_seconds = Histogram(
    "memory_llm_latency_seconds",
    "LLM 调用延迟",
    ["model"],
)
memory_commit_total = Counter(
    "memory_commit_total",
    "记忆 commit 总数",
    ["action", "type"],
)
memory_version_conflict_total = Counter(
    "memory_version_conflict_total",
    "版本冲突总数",
)
memory_outbox_queue_depth = Gauge(
    "memory_outbox_queue_depth",
    "Outbox 待投递深度",
)
memory_outbox_oldest_pending_seconds = Gauge(
    "memory_outbox_oldest_pending_seconds",
    "Outbox 最老 pending 秒数",
)
memory_graph_state_changes_total = Counter(
    "memory_graph_state_changes_total",
    "图谱状态变更总数",
    ["from", "to", "source"],
)
memory_storage_checksum_failure_total = Counter(
    "memory_storage_checksum_failure_total",
    "存储 checksum 校验失败总数",
)
memory_http_requests_total = Counter(
    "memory_http_requests_total",
    "API HTTP 请求总数（route 为稳定模板，不含任何用户标识）",
    ["route", "method", "status"],
)
