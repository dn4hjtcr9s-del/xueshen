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
