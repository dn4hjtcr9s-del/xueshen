"""Conversation KnowledgeSummary Prometheus 指标（方案 §21.2）。

所有标签都限制为稳定枚举或受控模型名，不包含 user_id、thread_id、turn_id、summary_id
或任何正文，指标通过应用进程已有的 ``/metrics`` 端点暴露。
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

knowledge_summary_jobs_total = Counter(
    "conversation_knowledge_summary_jobs_total",
    "知识总结 Generation Job 总数",
    ["trigger", "status"],
)
knowledge_summary_queue_depth = Gauge(
    "conversation_knowledge_summary_queue_depth",
    "知识总结可领取 Job 深度",
    ["status", "trigger"],
)
knowledge_summary_job_duration_seconds = Histogram(
    "conversation_knowledge_summary_job_duration_seconds",
    "知识总结 Job 执行耗时",
    ["trigger", "status"],
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
knowledge_summary_model_calls_total = Counter(
    "conversation_knowledge_summary_model_calls_total",
    "知识总结模型调用总数",
    ["purpose", "result", "model"],
)
knowledge_summary_model_tokens_total = Counter(
    "conversation_knowledge_summary_model_tokens_total",
    "知识总结模型 token 用量",
    ["purpose", "direction"],
)
knowledge_summary_candidates_total = Counter(
    "conversation_knowledge_summary_candidates_total",
    "知识总结候选处置总数",
    ["disposition"],
)
knowledge_summary_item_mutations_total = Counter(
    "conversation_knowledge_summary_item_mutations_total",
    "知识总结条目变更总数",
    ["section", "action"],
)
knowledge_summary_merge_total = Counter(
    "conversation_knowledge_summary_merge_total",
    "知识总结合并决策总数",
    ["decision"],
)
knowledge_summary_review_total = Counter(
    "conversation_knowledge_summary_review_total",
    "知识总结 review 总数",
    ["reason"],
)
knowledge_summary_api_requests_total = Counter(
    "conversation_knowledge_summary_api_requests_total",
    "知识总结 API 请求总数",
    ["route", "status"],
)
knowledge_summary_auto_suspensions_total = Counter(
    "conversation_knowledge_summary_auto_suspensions_total",
    "知识总结自动生成熔断总数",
    ["reason"],
)
knowledge_summary_retention_operations_total = Counter(
    "conversation_knowledge_summary_retention_operations_total",
    "知识总结 retention 操作总数",
    ["operation", "result"],
)

# ---------------------------------------------------------------------------
# Conversation Rollout（memory-rebuild §1.5 / §5.12）
#
# 标签同样只允许稳定枚举：record_type 取自契约的封闭类型集合，reason 是固定的
# 丢弃原因枚举。禁止把 thread_id / turn_id / 正文放进标签（基数与隐私双重原因）。
# ---------------------------------------------------------------------------

rollout_records_total = Counter(
    "conversation_rollout_records_total",
    "rollout 记录成功入队总数",
    ["record_type"],
)
rollout_records_written_total = Counter(
    "conversation_rollout_records_written_total",
    "rollout 记录实际写入并 flush 的总数",
)
rollout_records_rejected_total = Counter(
    "conversation_rollout_records_rejected_total",
    "rollout 记录被白名单/脱敏/契约拒绝总数",
    ["record_type"],
)
rollout_write_retry_total = Counter(
    "conversation_rollout_write_retry_total",
    "rollout 写入失败后截断重开的次数",
)
rollout_write_failed_total = Counter(
    "conversation_rollout_write_failed_total",
    "rollout 写入二次失败并进入降级的次数",
)
rollout_dropped_total = Counter(
    "conversation_rollout_dropped_total",
    "rollout 记录被丢弃总数",
    ["reason"],
)
rollout_segment_guard_triggered_total = Counter(
    "conversation_rollout_segment_guard_triggered_total",
    "rollout 段达到防爆阈值（segment_size_guard_triggered）的次数",
)
rollout_segment_registration_total = Counter(
    "conversation_rollout_segment_registration_total",
    "rollout 段 open manifest 登记结果（inserted / already_open / failed）",
    ["result"],
)
rollout_queue_depth = Gauge(
    "conversation_rollout_queue_depth",
    "rollout 写入队列当前深度",
)
rollout_flush_latency_seconds = Histogram(
    "conversation_rollout_flush_latency_seconds",
    "rollout flush ack 等待耗时",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5),
)

rollout_read_source_total = Counter(
    "conversation_rollout_read_source_total",
    "rollout 正文读取的来源（local / object / db_fallback）",
    ["source"],
)
rollout_read_repair_total = Counter(
    "conversation_rollout_read_repair_total",
    "rollout 指针不可用而回退到 conversation_messages.content 的次数",
)
rollout_sealed_total = Counter(
    "conversation_rollout_sealed_total",
    "rollout 段封存成功/失败次数",
    ["result"],
)
