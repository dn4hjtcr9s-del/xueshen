"""执行 70-case 全链路 Agentic RAG 延迟评测。

本模块只做评测，不调用 Conversation 的 persist_turn、memory_ack、Memory submit
或学习状态写回接口。评测前冻结异步写入服务，使用账号当前的长期记忆快照，逐 case
执行记忆读取、问题改写、多路检索、Qwen3-Rerank、证据判断、上下文打包、答案生成
和引用校验，并把细粒度 trace 与统计报告写入 ``evals/``。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import statistics
import subprocess
import sys
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.conversation.gateways.embedding import QueryEmbeddingGateway  # noqa: E402
from backend.conversation.gateways.openai import OpenAIGateway  # noqa: E402
from backend.conversation.gateways.retriever import AsyncRetrieverAdapter  # noqa: E402
from backend.conversation.graph.nodes import answer as answer_node  # noqa: E402
from backend.conversation.graph.nodes import evidence as evidence_node  # noqa: E402
from backend.conversation.graph.nodes import retrieval as retrieval_node  # noqa: E402
from backend.conversation.graph.nodes import rewrite as rewrite_node  # noqa: E402
from backend.conversation.graph.nodes import snapshot as snapshot_node  # noqa: E402
from backend.conversation.graph.state import (  # noqa: E402
    ConversationRuntimeContext,
    SystemClock,
    SystemIdGenerator,
    snapshot_from_dict,
)
from backend.conversation.services.context_service import ContextService  # noqa: E402
from backend.conversation.services.corpus_vocabulary import (  # noqa: E402
    ActiveCorpusVocabularyLoader,
)
from backend.conversation.services.token_counter import TokenCounter  # noqa: E402
from backend.rag.retrieval import RetrievalService  # noqa: E402
from backend.rag.settings import RAGSettings  # noqa: E402
from backend.settings import Settings, get_settings  # noqa: E402
from evals.rerank import (  # noqa: E402
    RERANK_DOCUMENT_STRATEGY,
    RERANK_QUERY_STRATEGY,
    RerankClient,
    RerankRequestError,
    RerankSettings,
)

USER_ACCOUNT = "2428938672"
USER_ID = UUID("66213a1c-45dc-4482-942b-fb18029be0ab")
EXPECTED_CASE_COUNT = 70
DEFAULT_CASES_PATH = ROOT / "evals" / "retrieval_cases_v1.jsonl"
MEMORY_FREEZE_QUERY = "数学教材学习者长期记忆固定评测快照"
ASYNC_WRITE_SERVICES = (
    "memory-worker",
    "memory-scheduler",
    "memory-outbox-consumer",
    "conversation-worker",
    "conversation-outbox-publisher",
)
LOG = logging.getLogger("full-agentic-rag-latency-eval")


class EvaluationError(RuntimeError):
    """全链路评测的配置、服务或数据错误。"""


class EvalOpenAIGateway(OpenAIGateway):
    """评测专用模型适配：有限重试并保留生产语义的显式降级路径。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._trace_meta: dict[str, dict[str, Any]] = {}

    async def _retry(
        self,
        role: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        max_attempts: int = 3,
    ) -> Any:
        """对空输出、截断 JSON、限流和瞬时网络错误做有限重试。"""
        failures: list[str] = []
        for attempt in range(1, max_attempts + 1):
            try:
                value = await operation()
            except Exception as exc:
                failures.append(type(exc).__name__)
                if attempt >= max_attempts:
                    self._trace_meta[role] = {
                        "model_attempt_count": attempt,
                        "model_retry_count": attempt - 1,
                        "model_failure_types": failures,
                        "model_fallback": None,
                    }
                    raise
                await asyncio.sleep(min(2.0 * attempt, 5.0))
            else:
                self._trace_meta[role] = {
                    "model_attempt_count": attempt,
                    "model_retry_count": attempt - 1,
                    "model_failure_types": failures,
                    "model_fallback": None,
                }
                return value
        raise AssertionError("模型重试循环不应到达此处")

    def consume_trace(self, role: str) -> dict[str, Any]:
        """返回最近一次角色调用的重试/降级元数据。"""
        return dict(self._trace_meta.pop(role, {}))

    async def rewrite_and_plan(
        self, *, context_view: dict[str, Any], prior_attempts: int
    ) -> dict[str, Any]:
        """真实改写失败三次后，按生产设计降级为当前问题单查询。"""
        parent = super()

        async def operation() -> dict[str, Any]:
            return await parent.rewrite_and_plan(
                context_view=context_view,
                prior_attempts=prior_attempts,
            )

        try:
            return await self._retry("rewrite", operation)
        except Exception:
            current = str(context_view.get("current_user_request") or "")[:500]
            meta = self._trace_meta.setdefault("rewrite", {})
            meta["model_fallback"] = "single_query_plan"
            return {
                "schema_version": "1",
                "plan_revision": prior_attempts,
                "standalone_question": current or "数学教材问题",
                "answer_mode": "rag",
                "need_retrieval": True,
                "memory_trigger": "none",
                "topic_hints": [],
                "subqueries": [
                    {
                        "subquery_id": "sq-fallback",
                        "query_text": current or "数学教材问题",
                        "intent": "fallback",
                        "coverage_target": "",
                        "semantic_filters": {},
                    }
                ],
                "reason_codes": ["rewrite_model_fallback"],
            }

    async def assess_evidence(
        self, *, question: str, evidence_summary: str, budget_remaining: str
    ) -> dict[str, Any]:
        """证据判断失败三次后，依据是否存在证据进入明确降级结果。"""
        parent = super()

        async def operation() -> dict[str, Any]:
            return await parent.assess_evidence(
                question=question,
                evidence_summary=evidence_summary,
                budget_remaining=budget_remaining,
            )

        try:
            return await self._retry("evidence", operation)
        except Exception:
            has_evidence = evidence_summary.strip() not in {"", "（无证据）"}
            meta = self._trace_meta.setdefault("evidence", {})
            meta["model_fallback"] = "evidence_presence_rule"
            return {
                "status": "sufficient" if has_evidence else "insufficient",
                "covered_aspects": [],
                "missing_aspects": [] if has_evidence else ["缺少教材证据"],
                "unsupported_claim_risk": "medium" if has_evidence else "high",
                "next_search_focus": [],
                "reason_codes": ["evidence_model_fallback"],
            }

    async def stream_answer(
        self, *, answer_context: dict[str, Any]
    ) -> tuple[list[str], dict[str, Any]]:
        """使用真实 Answer 模型生成正文，绕开当前兼容网关的空/截断 JSON。

        Rewrite 与 Evidence 仍使用 Structured Output；Answer 只影响评测适配层，
        不修改生产 Conversation Gateway。服务端证据集仍由 answer 节点注入。
        """
        from backend.conversation.graph.prompts import ANSWER_SYSTEM_PROMPT

        response = await self._client.responses.create(
            model=self._model_for("answer"),
            input=[
                {
                    "role": "system",
                    "content": (
                        ANSWER_SYSTEM_PROMPT
                        + "\n评测兼容模式：只输出最终回答正文，不输出 JSON，不重复引用清单。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(answer_context, ensure_ascii=False, default=str),
                },
            ],
            max_output_tokens=4000,
        )
        answer = str(response.output_text or "").strip()
        if not answer:
            raise EvaluationError("Answer 模型兼容模式返回空内容")
        self._trace_meta["answer"] = {
            "model_attempt_count": 1,
            "model_retry_count": 0,
            "model_failure_types": [],
            "model_fallback": "plain_text_compat",
        }
        result = {"answer": answer, "citations": [], "followups": []}
        return [answer], result


class DevHeaderMemoryGateway:
    """使用本地开发认证头读取 Memory，明确不提供任何写入方法。"""

    def __init__(self, *, base_url: str, user_id: UUID, timeout: float) -> None:
        import httpx

        self._user_id = str(user_id)
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def build_learning_context(
        self, *, query: str, token_budget: int | None = None
    ) -> dict[str, Any]:
        """只读调用 Memory context；不携带 Bearer，避免误走生产 Agent token。"""
        response = await self._http.post(
            "/api/v1/memory/context",
            json={"query": query, "token_budget": token_budget},
            headers={"X-Dev-User-Id": self._user_id},
        )
        if response.status_code >= 400:
            raise EvaluationError(
                f"Memory context 读取失败：HTTP {response.status_code} {response.text[:200]}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise EvaluationError("Memory context 返回不是 JSON 对象")
        return payload

    async def aclose(self) -> None:
        """关闭 Memory 只读客户端。"""
        await self._http.aclose()


class ServiceState:
    """保存评测前服务状态，并在 finally 中恢复原状态。"""

    def __init__(self, *, manage: bool) -> None:
        self.manage = manage
        self.before: list[str] = []
        self.stopped: list[str] = []
        self.after: list[str] = []
        self.error: str | None = None

    @staticmethod
    def _compose(*args: str, check: bool = True) -> list[str]:
        result = subprocess.run(
            ["docker", "compose", *args],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-1000:]
            raise EvaluationError(f"docker compose {' '.join(args)} 失败：{detail}")
        return result.stdout.splitlines()

    def _running(self) -> list[str]:
        return sorted(
            item.strip()
            for item in self._compose("ps", "--services", "--filter", "status=running")
            if item.strip()
        )

    def freeze(self) -> None:
        """只停止评测前正在运行的异步写入服务。"""
        if not self.manage:
            return
        self.before = self._running()
        self.stopped = [service for service in ASYNC_WRITE_SERVICES if service in self.before]
        if self.stopped:
            self._compose("stop", *self.stopped)

    def restore(self) -> None:
        """只启动评测前处于运行状态的服务，避免改变用户原有部署。"""
        if not self.manage:
            return
        try:
            if self.stopped:
                self._compose("start", *self.stopped)
            self.after = self._running()
        except Exception as exc:  # pragma: no cover - 仅在宿主 Docker 异常时触发
            self.error = f"恢复异步服务失败：{type(exc).__name__}: {exc}"
            raise


class TraceClock:
    """提供单调时钟，避免系统时间跳变影响耗时统计。"""

    @staticmethod
    def now_ms() -> float:
        return perf_counter() * 1000.0


async def timed_stage(
    stages: dict[str, dict[str, Any]],
    name: str,
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    """运行一个异步阶段并写入统一耗时/错误字段。"""
    started = TraceClock.now_ms()
    try:
        value = await operation()
    except Exception as exc:
        stages[name] = {
            "duration_ms": round(TraceClock.now_ms() - started, 3),
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }
        raise
    duration = TraceClock.now_ms() - started
    current = stages.setdefault(name, {})
    current["duration_ms"] = round(duration, 3)
    current.setdefault("status", "succeeded")
    return value


def _read_env_file(path: Path) -> dict[str, str]:
    """读取本地简单 .env，仅用于把 Compose 的变量映射到评测进程。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def configure_environment() -> None:
    """复用现有 .env/Compose 配置，不把任何凭证写入 trace 或标准输出。"""
    values = _read_env_file(ROOT / ".env")
    for key, value in values.items():
        os.environ.setdefault(key, value)
    mapping = {
        "OPENAI_API_KEY": "DEEPSEEK_API_KEY",
        "OPENAI_BASE_URL": "DEEPSEEK_BASE_URL",
        "OPENAI_REWRITE_MODEL": "DEEPSEEK_MODEL",
        "OPENAI_EVIDENCE_MODEL": "DEEPSEEK_MODEL",
        "OPENAI_ANSWER_MODEL": "DEEPSEEK_MODEL",
        "OPENAI_CONVERSATION_SUMMARY_MODEL": "DEEPSEEK_MODEL",
    }
    for target, source in mapping.items():
        if not os.environ.get(target) and os.environ.get(source):
            os.environ[target] = os.environ[source]
    os.environ.setdefault("RAG_DATABASE_URL", "postgresql+psycopg://rag:rag@127.0.0.1:55433/rag")
    os.environ.setdefault("MEMORY_API_BASE_URL", "http://127.0.0.1:8001")
    # 评测期间只读 Memory；提交、学习状态写回和 Conversation 持久化均不进入 harness。
    os.environ["CONVERSATION_MEMORY_READ_ENABLED"] = "true"
    os.environ["CONVERSATION_MEMORY_SUBMIT_ENABLED"] = "false"
    os.environ["CONVERSATION_AGENTIC_RAG_ENABLED"] = "true"
    os.environ["CONVERSATION_MULTI_QUERY_ENABLED"] = "true"
    os.environ["CONVERSATION_EVIDENCE_LOOP_ENABLED"] = "true"
    get_settings.cache_clear()


def _load_cases(path: Path) -> list[dict[str, Any]]:
    """读取并校验固定 70-case JSONL。"""
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise EvaluationError(f"{path}:{line_number} 必须是 JSON 对象")
        rows.append(payload)
    if len(rows) != EXPECTED_CASE_COUNT:
        raise EvaluationError(f"评测集必须是 {EXPECTED_CASE_COUNT} 条，实际 {len(rows)} 条")
    return rows


def _canonical_memory_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    """提取稳定记忆内容，排除每次请求可变化的 query 字段。"""
    return {
        "learner": context.get("learner"),
        "mastery": context.get("mastery") or [],
        "graph_states": context.get("graph_states") or [],
        "recommendations": context.get("recommendations") or [],
        "truncated": bool(context.get("truncated")),
    }


def _hash_payload(payload: Any) -> str:
    """计算可审计但不泄露原文的 SHA-256 哈希。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _uuid_for(case_id: str, suffix: str) -> UUID:
    """为评测生成不写入数据库的稳定 UUID。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"xueshen-full-eval:{case_id}:{suffix}")


def _token_count(counter: TokenCounter, text: str) -> int:
    """使用项目 TokenCounter 估算 trace 输入输出 token。"""
    try:
        return int(counter.count(text))
    except Exception:
        return max(1, math.ceil(len(text) / 4))


def _snapshot_counts(snapshot: Mapping[str, Any], counter: TokenCounter) -> dict[str, int]:
    """统计快照各分区大小，便于解释上下文打包耗时。"""
    memory = snapshot.get("memory") or {}
    memory_text = json.dumps(memory, ensure_ascii=False, default=str)
    recent_text = json.dumps(snapshot.get("recent_messages") or [], ensure_ascii=False, default=str)
    return {
        "current_message_tokens": _token_count(counter, str(snapshot.get("current_message") or "")),
        "recent_message_tokens": _token_count(counter, recent_text),
        "memory_tokens_estimated": _token_count(counter, memory_text),
    }


def _evidence_items_stats(evidence_set: Mapping[str, Any], counter: TokenCounter) -> dict[str, int]:
    """统计最终证据集大小和上下文 token。"""
    items = evidence_set.get("items") or []
    evidence_text = "\n".join(str(item.get("content_text") or "") for item in items)
    return {
        "evidence_item_count": len(items),
        "evidence_tokens": int(evidence_set.get("total_tokens") or 0),
        "evidence_text_tokens_estimated": _token_count(counter, evidence_text)
        if evidence_text
        else 0,
    }


def _apply_rerank_results(
    state: dict[str, Any],
    *,
    results: Sequence[Any],
) -> int:
    """把 Qwen 返回的候选顺序注入 Evidence 阶段，并同步 matched-subquery 映射。"""
    raw_hits = list(state.get("evidence_hits") or [])
    matched = state.get("matched_subquery_ids") or {}
    selected_hits = []
    selected_keys: set[tuple[str, str]] = set()
    for item in results:
        hit = raw_hits[int(item.index)]
        selected_hits.append(replace(hit, score=float(item.relevance_score)))
        selected_keys.add((hit.corpus_id, hit.chunk_id))
    state["evidence_hits"] = selected_hits
    state["matched_subquery_ids"] = {
        key: value for key, value in matched.items() if key in selected_keys
    }
    return len(selected_hits)


async def _run_case(
    *,
    case: Mapping[str, Any],
    fixed_memory: dict[str, Any],
    fixed_memory_hash: str,
    memory_gateway: DevHeaderMemoryGateway,
    runtime: ConversationRuntimeContext,
    context_service: ContextService,
    vocabulary: Any,
    rerank_client: RerankClient,
    settings: Settings,
    token_counter: TokenCounter,
) -> dict[str, Any]:
    """执行一个 case 的完整 Agentic RAG 链路并返回脱敏 trace。"""
    case_id = str(case["case_id"])
    query = str(case["query"])
    trace_id = uuid.uuid4().hex
    case_started = TraceClock.now_ms()
    stages: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    degraded_flags: list[str] = []
    thread_id = _uuid_for(case_id, "thread")
    turn_id = _uuid_for(case_id, "turn")
    state: dict[str, Any] = {
        "user_id": USER_ID,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "user_message_id": _uuid_for(case_id, "message"),
        "request_id": trace_id,
        "run_id": trace_id,
        "conversation_context": {
            "current_message": query,
            "recent_messages": [],
            "conversation_summary": None,
        },
        "memory_context": copy.deepcopy(fixed_memory),
        "plan_revision": 0,
        "executed_query_fingerprints": [],
        "worker_results": {},
        "evidence_assessment": None,
        "retrieval_iteration": 0,
        "_max_retrieval_iterations": int(settings.conversation_retrieval_max_iterations),
        "degraded_flags": [],
        "errors": [],
        "_citations_emitted": True,
    }

    try:

        async def read_memory() -> dict[str, Any]:
            # 每 case 仍执行真实只读请求计时，但下游始终使用 run 开始时冻结的快照。
            return await memory_gateway.build_learning_context(
                query=MEMORY_FREEZE_QUERY,
                token_budget=settings.conversation_memory_token_budget,
            )

        live_memory = await timed_stage(stages, "memory_read", read_memory)
        live_hash = _hash_payload(_canonical_memory_payload(live_memory))
        stages["memory_read"].update(
            {
                "mode": "frozen_snapshot_replay",
                "query": MEMORY_FREEZE_QUERY,
                "memory_snapshot_hash": fixed_memory_hash,
                "live_memory_hash": live_hash,
                "snapshot_match": live_hash == fixed_memory_hash,
                "learner_present": bool(fixed_memory.get("learner")),
                "mastery_count": len(fixed_memory.get("mastery") or []),
                "graph_state_count": len(fixed_memory.get("graph_states") or []),
                "recommendation_count": len(fixed_memory.get("recommendations") or []),
            }
        )
        if live_hash != fixed_memory_hash:
            degraded_flags.append("memory_live_snapshot_changed_ignored")

        async def pack_snapshot() -> dict[str, Any]:
            return await snapshot_node.build_turn_snapshot(
                state,
                runtime=runtime,
                context_service=context_service,
            )

        state.update(await timed_stage(stages, "snapshot_context_pack", pack_snapshot))
        snapshot = state["snapshot"]
        stages["snapshot_context_pack"].update(
            {
                "snapshot_hash": state["snapshot_hash"],
                "fixed_memory_hash": fixed_memory_hash,
                "context_counts": _snapshot_counts(snapshot, token_counter),
                "recent_message_count": len(snapshot.get("recent_messages") or []),
            }
        )

        max_iterations = int(settings.conversation_retrieval_max_iterations)
        while True:
            iteration = int(state.get("retrieval_iteration") or 0)
            rewrite_name = "rewrite" if iteration == 0 else f"rewrite_iteration_{iteration}"

            async def rewrite() -> dict[str, Any]:
                return await rewrite_node.rewrite_and_plan(
                    state,
                    runtime=runtime,
                    context_service=context_service,
                    vocabulary=vocabulary,
                    max_subqueries=int(settings.conversation_retrieval_max_subqueries),
                )

            rewrite_result = await timed_stage(stages, rewrite_name, rewrite)
            state.update(rewrite_result)
            state["retrieval_iteration"] = iteration
            plan = state.get("rewrite_plan") or {}
            subqueries = plan.get("subqueries") or []
            stages[rewrite_name].update(
                {
                    "model": settings.openai_rewrite_model,
                    **runtime.openai_gateway.consume_trace("rewrite"),
                    "plan_revision": int(plan.get("plan_revision") or 0),
                    "need_retrieval": bool(plan.get("need_retrieval")),
                    "answer_mode": plan.get("answer_mode"),
                    "subquery_count": len(subqueries),
                    "subqueries_tokens_estimated": _token_count(
                        token_counter,
                        json.dumps(subqueries, ensure_ascii=False, default=str),
                    ),
                }
            )

            if plan.get("need_retrieval") and subqueries:

                async def embed() -> dict[str, Any]:
                    return await retrieval_node.embed_subqueries(state, runtime=runtime)

                embed_result = await timed_stage(
                    stages,
                    "embedding" if iteration == 0 else f"embedding_iteration_{iteration}",
                    embed,
                )
                state.update(embed_result)
                embedding_name = (
                    "embedding" if iteration == 0 else f"embedding_iteration_{iteration}"
                )
                stages[embedding_name].update(
                    {
                        "model": settings.embedding_model,
                        "query_count": len(subqueries),
                        "dimensions": settings.rag_embedding_dimensions,
                    }
                )

                async def retrieve_all(
                    plan_value: Mapping[str, Any] = plan,
                    subquery_values: Sequence[Mapping[str, Any]] = tuple(subqueries),
                ) -> dict[str, Any]:
                    plan_revision = int(plan_value.get("plan_revision") or 0)
                    embedded = state.get("embedded_queries") or {}
                    inputs = [
                        {
                            "plan_revision": plan_revision,
                            "subquery_id": str(item["subquery_id"]),
                            "query_text": str(item["query_text"]),
                            "query_vector": embedded.get(str(item["subquery_id"])),
                            "validated_filters": item.get("semantic_filters") or {},
                            "limit": int(settings.conversation_retrieval_result_limit),
                        }
                        for item in subquery_values
                    ]
                    results = await asyncio.gather(
                        *(
                            retrieval_node.retrieve_subquery(
                                item,
                                runtime=runtime,
                            )
                            for item in inputs
                        )
                    )
                    merged: dict[str, dict[str, Any]] = dict(state.get("worker_results") or {})
                    for result in results:
                        merged.update(result.get("worker_results") or {})
                    return {"worker_results": merged, "_retrieval_results": results}

                retrieval_name = (
                    "retrieval" if iteration == 0 else f"retrieval_iteration_{iteration}"
                )
                retrieval_result = await timed_stage(stages, retrieval_name, retrieve_all)
                results = retrieval_result.pop("_retrieval_results")
                state.update(retrieval_result)
                worker_rows = [
                    next(iter(item.get("worker_results", {}).values()))
                    for item in results
                    if item.get("worker_results")
                ]
                stages[retrieval_name].update(
                    {
                        "method": "hybrid_search",
                        "parallel": True,
                        "subquery_count": len(subqueries),
                        "candidate_count": sum(len(row.get("hits") or []) for row in worker_rows),
                        "worker_statuses": [row.get("status") for row in worker_rows],
                        "worker_latencies_ms": [
                            round(float(row.get("latency_ms") or 0), 3) for row in worker_rows
                        ],
                        "per_subquery": [
                            {
                                "subquery_id": row.get("subquery_id"),
                                "status": row.get("status"),
                                "candidate_count": len(row.get("hits") or []),
                                "latency_ms": round(float(row.get("latency_ms") or 0), 3),
                            }
                            for row in worker_rows
                        ],
                    }
                )

                async def aggregate() -> dict[str, Any]:
                    return await evidence_node.aggregate_results(state, runtime=runtime)

                fusion_name = "fusion" if iteration == 0 else f"fusion_iteration_{iteration}"
                state.update(await timed_stage(stages, fusion_name, aggregate))
                raw_hits = list(state.get("evidence_hits") or [])
                stages[fusion_name].update(
                    {
                        "deduplicated_candidate_count": len(raw_hits),
                        "matched_subquery_count": sum(
                            1
                            for ids in (state.get("matched_subquery_ids") or {}).values()
                            if len(ids) > 1
                        ),
                    }
                )

                rerank_name = "rerank" if iteration == 0 else f"rerank_iteration_{iteration}"
                rerank_started = TraceClock.now_ms()
                if raw_hits:
                    try:
                        rerank_top_n = min(
                            int(settings.conversation_retrieval_result_limit), len(raw_hits)
                        )
                        rerank_results = await asyncio.to_thread(
                            rerank_client.rerank,
                            query=str(plan.get("standalone_question") or query),
                            documents=[str(hit.content_text) for hit in raw_hits],
                            top_n=rerank_top_n,
                        )
                        selected_count = _apply_rerank_results(state, results=rerank_results)
                        stages[rerank_name] = {
                            "duration_ms": round(TraceClock.now_ms() - rerank_started, 3),
                            "status": "succeeded",
                            "model": rerank_client._settings.model,
                            "query_strategy": RERANK_QUERY_STRATEGY,
                            "document_strategy": RERANK_DOCUMENT_STRATEGY,
                            "candidate_count": len(raw_hits),
                            "top_n": rerank_top_n,
                            "selected_count": selected_count,
                            "document_tokens_estimated": _token_count(
                                token_counter,
                                "\n".join(str(hit.content_text) for hit in raw_hits),
                            ),
                        }
                    except RerankRequestError as exc:
                        degraded_flags.append("rerank_failed_fallback_deterministic")
                        stages[rerank_name] = {
                            "duration_ms": round(TraceClock.now_ms() - rerank_started, 3),
                            "status": "failed_fallback",
                            "model": rerank_client._settings.model,
                            "candidate_count": len(raw_hits),
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                        }
                else:
                    stages[rerank_name] = {
                        "duration_ms": round(TraceClock.now_ms() - rerank_started, 3),
                        "status": "skipped_empty_candidates",
                        "candidate_count": 0,
                    }

                async def deterministic_pack() -> dict[str, Any]:
                    return await evidence_node.deduplicate_and_rerank(
                        state,
                        runtime=runtime,
                        settings=settings,
                        token_counter=token_counter,
                    )

                dedup_name = (
                    "evidence_pack" if iteration == 0 else f"evidence_pack_iteration_{iteration}"
                )
                state.update(await timed_stage(stages, dedup_name, deterministic_pack))
                stages[dedup_name].update(
                    _evidence_items_stats(state.get("evidence_set") or {}, token_counter)
                )
            else:
                state["evidence_set"] = {"items": [], "total_tokens": 0}
                state["evidence_hits"] = []
                stages.setdefault(
                    "retrieval", {"duration_ms": 0.0, "status": "skipped_no_retrieval"}
                )

            async def assess() -> dict[str, Any]:
                return await evidence_node.evaluate_evidence(state, runtime=runtime)

            assessment_name = (
                "evidence_assessment"
                if iteration == 0
                else f"evidence_assessment_iteration_{iteration}"
            )
            assessment_result = await timed_stage(stages, assessment_name, assess)
            state.update(assessment_result)
            assessment = state.get("evidence_assessment") or {}
            stages[assessment_name].update(
                {
                    "model": settings.openai_evidence_model,
                    **runtime.openai_gateway.consume_trace("evidence"),
                    "status_value": assessment.get("status"),
                    "missing_aspect_count": len(assessment.get("missing_aspects") or []),
                    "covered_aspect_count": len(assessment.get("covered_aspects") or []),
                    "reason_codes": assessment.get("reason_codes") or [],
                }
            )
            if assessment.get("status") == "needs_more" and iteration < max_iterations:
                state["retrieval_iteration"] = iteration + 1
                continue
            break

        snapshot_obj = snapshot_from_dict(state["snapshot"])
        items = (state.get("evidence_set") or {}).get("items") or []
        evidence_summary = "\n".join(str(item.get("content_text") or "") for item in items)
        evidence_refs = [str((item.get("citation") or {}).get("citation_id", "")) for item in items]
        answer_view_started = TraceClock.now_ms()
        answer_view = context_service.build_answer_view(
            snapshot=snapshot_obj,
            standalone_question=str(
                (state.get("rewrite_plan") or {}).get("standalone_question") or query
            ),
            evidence_summary=evidence_summary[:8000] or "（无证据）",
            evidence_refs=evidence_refs,
            degraded_flags=degraded_flags,
        )
        stages["context_packing"] = {
            "duration_ms": round(TraceClock.now_ms() - answer_view_started, 3),
            "status": "succeeded",
            "evidence_item_count": len(items),
            "evidence_tokens": int((state.get("evidence_set") or {}).get("total_tokens") or 0),
            "answer_context_tokens_estimated": _token_count(
                token_counter, json.dumps(answer_view, ensure_ascii=False, default=str)
            ),
            "answer_context_chars": len(json.dumps(answer_view, ensure_ascii=False, default=str)),
            "snapshot_hash": state["snapshot_hash"],
        }

        async def answer() -> dict[str, Any]:
            return await answer_node.generate_answer(
                state, runtime=runtime, context_service=context_service
            )

        answer_result = await timed_stage(stages, "answer", answer)
        state.update(answer_result)
        stages["answer"].update(
            {
                "model": settings.openai_answer_model,
                **runtime.openai_gateway.consume_trace("answer"),
                "output_chars": len(str(state.get("answer_buffer") or "")),
                "output_tokens_estimated": _token_count(
                    token_counter, str(state.get("answer_buffer") or "")
                ),
                "citation_count": len((state.get("answer_payload") or {}).get("citations") or []),
            }
        )

        async def validate() -> dict[str, Any]:
            return await answer_node.validate_answer_and_citations(state, runtime=runtime)

        state.update(await timed_stage(stages, "citation_validation", validate))
        stages["citation_validation"].update(
            {
                "degraded_flags": state.get("degraded_flags") or [],
                "valid_citation_count": len(
                    (state.get("answer_payload") or {}).get("citations") or []
                ),
            }
        )
        degraded_flags.extend(str(item) for item in state.get("degraded_flags") or [])
        status = "succeeded"
    except Exception as exc:
        status = "failed"
        error = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        errors.append(error)
        LOG.exception("case %s 失败", case_id)

    total_ms = TraceClock.now_ms() - case_started
    accounted_ms = sum(
        float(item.get("duration_ms") or 0) for item in stages.values() if isinstance(item, dict)
    )
    return {
        "case_id": case_id,
        "trace_id": trace_id,
        "user_account": USER_ACCOUNT,
        "user_id": str(USER_ID),
        "status": status,
        "memory_snapshot_hash": fixed_memory_hash,
        "stages": stages,
        "total_duration_ms": round(total_ms, 3),
        "accounted_stage_duration_ms": round(accounted_ms, 3),
        "unattributed_duration_ms": round(max(0.0, total_ms - accounted_ms), 3),
        "retrieval_iteration_count": int(state.get("retrieval_iteration") or 0),
        "final_subquery_count": len((state.get("rewrite_plan") or {}).get("subqueries") or []),
        "final_candidate_count": len(state.get("evidence_hits") or []),
        "final_evidence_count": len((state.get("evidence_set") or {}).get("items") or []),
        "context_tokens_estimated": stages.get("context_packing", {}).get(
            "answer_context_tokens_estimated", 0
        ),
        "degraded_flags": sorted(set(degraded_flags)),
        "errors": errors,
    }


def _percentile(values: Sequence[float], percentile: float) -> float:
    """使用线性插值计算分位数，样本少时仍保持可解释。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)


def _latency_summary(values: Sequence[float]) -> dict[str, float | int]:
    """输出平均、P50/P90/P95、最大最小。"""
    if not values:
        return {
            "count": 0,
            "avg_ms": 0.0,
            "p50_ms": 0.0,
            "p90_ms": 0.0,
            "p95_ms": 0.0,
            "min_ms": 0.0,
            "max_ms": 0.0,
        }
    return {
        "count": len(values),
        "avg_ms": round(statistics.fmean(values), 3),
        "p50_ms": round(_percentile(values, 0.50), 3),
        "p90_ms": round(_percentile(values, 0.90), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "min_ms": round(min(values), 3),
        "max_ms": round(max(values), 3),
    }


def _stage_report(traces: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """按阶段聚合耗时和总耗时占比。"""
    names: set[str] = set()
    for trace in traces:
        names.update((trace.get("stages") or {}).keys())
    total_all = sum(float(trace.get("total_duration_ms") or 0) for trace in traces)
    report: dict[str, Any] = {}
    for name in sorted(names):
        values = [
            float((trace.get("stages") or {}).get(name, {}).get("duration_ms") or 0)
            for trace in traces
            if name in (trace.get("stages") or {})
        ]
        total_stage = sum(values)
        summary = _latency_summary(values)
        summary["total_ms"] = round(total_stage, 3)
        summary["share_of_total"] = round(total_stage / total_all, 6) if total_all else 0.0
        report[name] = summary
    return report


def _group_latency(
    traces: Sequence[Mapping[str, Any]],
    key_fn: Callable[[Mapping[str, Any]], str],
) -> dict[str, Any]:
    """按输入规模分组输出平均耗时。"""
    groups: dict[str, list[float]] = defaultdict(list)
    for trace in traces:
        if trace.get("status") != "succeeded":
            continue
        groups[key_fn(trace)].append(float(trace.get("total_duration_ms") or 0))
    return {key: _latency_summary(values) for key, values in sorted(groups.items())}


def build_report(
    *,
    traces: Sequence[Mapping[str, Any]],
    settings: Settings,
    fixed_memory_hash: str,
    services: ServiceState,
    started_at: str,
) -> dict[str, Any]:
    """生成完整脱敏统计报告。"""
    succeeded = [trace for trace in traces if trace.get("status") == "succeeded"]
    total_values = [float(trace.get("total_duration_ms") or 0) for trace in succeeded]
    stage = _stage_report(succeeded)
    rerank_failures = sum(
        1
        for trace in traces
        if any(
            item.get("status") == "failed_fallback"
            for item in (trace.get("stages") or {}).values()
            if isinstance(item, dict)
        )
    )
    mismatch_count = sum(
        1
        for trace in traces
        if any(
            item.get("snapshot_match") is False
            for item in (trace.get("stages") or {}).values()
            if isinstance(item, dict)
        )
    )
    return {
        "evaluator": "full_agentic_rag_latency",
        "run_at": datetime.now(UTC).isoformat(),
        "started_at": started_at,
        "dataset": {"path": str(DEFAULT_CASES_PATH.relative_to(ROOT)), "case_count": len(traces)},
        "account": {
            "account": USER_ACCOUNT,
            "user_id": str(USER_ID),
            "memory_snapshot_hash": fixed_memory_hash,
        },
        "execution": {
            "chain": [
                "memory_read",
                "snapshot_context_pack",
                "rewrite",
                "embedding",
                "retrieval_parallel_hybrid",
                "fusion",
                "qwen3_rerank",
                "evidence_pack",
                "evidence_assessment",
                "context_packing",
                "answer_non_streaming_plain_text_compat",
                "citation_validation",
            ],
            "memory_mode": "frozen_snapshot_replay",
            "answer_generation_mode": "plain_text_compat_real_model",
            "writes_disabled_in_harness": [
                "persist_turn",
                "memory_ack",
                "memory_submit",
                "learning_state_update",
                "conversation_outbox_write",
            ],
            "feature_flags": {
                "agentic_rag": settings.conversation_agentic_rag_enabled,
                "multi_query": settings.conversation_multi_query_enabled,
                "evidence_loop": settings.conversation_evidence_loop_enabled,
                "memory_read": True,
                "memory_submit": False,
            },
        },
        "services": {
            "before_running": services.before,
            "stopped_for_eval": services.stopped,
            "after_running": services.after,
            "restore_error": services.error,
        },
        "summary": {
            "case_count": len(traces),
            "succeeded": len(succeeded),
            "failed": len(traces) - len(succeeded),
            "total_latency": _latency_summary(total_values),
            "stage_latency": stage,
            "rerank_fallback_cases": rerank_failures,
            "memory_snapshot_mismatch_cases_ignored": mismatch_count,
            "degraded_case_count": sum(bool(trace.get("degraded_flags")) for trace in traces),
        },
        "factors": {
            "by_final_subquery_count": _group_latency(
                succeeded, lambda trace: str(trace.get("final_subquery_count", 0))
            ),
            "by_final_candidate_count_bucket": _group_latency(
                succeeded,
                lambda trace: (
                    "0"
                    if int(trace.get("final_candidate_count", 0)) == 0
                    else "1-20"
                    if int(trace.get("final_candidate_count", 0)) <= 20
                    else "21-40"
                    if int(trace.get("final_candidate_count", 0)) <= 40
                    else "41+"
                ),
            ),
            "by_context_tokens_bucket": _group_latency(
                succeeded,
                lambda trace: (
                    "0-1000"
                    if int(trace.get("context_tokens_estimated", 0)) <= 1000
                    else "1001-2500"
                    if int(trace.get("context_tokens_estimated", 0)) <= 2500
                    else "2501+"
                ),
            ),
            "by_retrieval_iteration_count": _group_latency(
                succeeded, lambda trace: str(trace.get("retrieval_iteration_count", 0))
            ),
        },
        "slow_cases": sorted(
            [
                {
                    "case_id": trace.get("case_id"),
                    "total_duration_ms": trace.get("total_duration_ms"),
                    "status": trace.get("status"),
                    "degraded_flags": trace.get("degraded_flags"),
                    "stage_durations_ms": {
                        name: item.get("duration_ms")
                        for name, item in (trace.get("stages") or {}).items()
                        if isinstance(item, dict)
                    },
                }
                for trace in traces
            ],
            key=lambda item: float(item.get("total_duration_ms") or 0),
            reverse=True,
        )[:10],
    }


async def run_evaluation(
    *, cases_path: Path, manage_services: bool, limit: int | None
) -> tuple[Path, Path]:
    """冻结服务、运行 70-case 全链路并恢复服务。"""
    configure_environment()
    cases = _load_cases(cases_path)
    if limit is not None:
        cases = cases[:limit]
    started_at = datetime.now(UTC).isoformat()
    services = ServiceState(manage=manage_services)
    memory_gateway: DevHeaderMemoryGateway | None = None
    rerank_client: RerankClient | None = None
    retrieval_service: RetrievalService | None = None
    traces: list[dict[str, Any]] = []
    fixed_memory: dict[str, Any] = {}
    fixed_memory_hash = ""
    try:
        services.freeze()
        settings = Settings()
        if not settings.openai_api_key:
            raise EvaluationError("未配置 OPENAI_API_KEY/DEEPSEEK_API_KEY")
        if not settings.embedding_api_key_resolved:
            raise EvaluationError("未配置 EMBEDDING_API_KEY/DASHSCOPE_API_KEY")
        rag_settings = RAGSettings()
        retrieval_service = RetrievalService(settings=rag_settings)
        vocabulary_loader = ActiveCorpusVocabularyLoader(retrieval_service.engine)
        failures = vocabulary_loader.validate_embedding_profile(
            model=settings.embedding_model,
            dimensions=settings.rag_embedding_dimensions,
        )
        if failures:
            raise EvaluationError("Embedding profile 校验失败：" + "; ".join(failures))
        vocabulary = vocabulary_loader.load()
        token_counter = TokenCounter()
        # Tokenizer 首次加载属于运行准备，不应混入第一个 case 的业务延迟。
        token_counter.count("评测预热")
        context_service = ContextService(settings=settings, token_counter=token_counter)
        openai_gateway = EvalOpenAIGateway(settings=settings, logger=LOG)
        embedding_gateway = QueryEmbeddingGateway(settings=settings, logger=LOG)
        retriever_gateway = AsyncRetrieverAdapter(
            retrieval_service=retrieval_service,
            concurrency=settings.conversation_retrieval_concurrency,
            worker_timeout_seconds=settings.conversation_retrieval_worker_timeout_seconds,
            logger=LOG,
        )
        runtime = ConversationRuntimeContext(
            openai_gateway=openai_gateway,
            memory_gateway=None,
            embedding_gateway=embedding_gateway,
            retriever_gateway=retriever_gateway,
            conversation_repository=None,
            turn_event_writer=None,
            clock=SystemClock(),
            id_generator=SystemIdGenerator(),
            logger=LOG,
            flags={
                "agentic_rag": True,
                "multi_query": True,
                "evidence_loop": True,
                "memory_read": True,
                "memory_submit": False,
                "streaming": True,
            },
        )
        runtime.settings = settings
        runtime.context_service = context_service
        runtime.token_counter = token_counter
        memory_gateway = DevHeaderMemoryGateway(
            base_url=settings.memory_api_base_url,
            user_id=USER_ID,
            timeout=max(settings.memory_context_timeout_seconds, 10.0),
        )
        runtime.memory_gateway = memory_gateway
        # 在计时前先读取一次并固定 Memory；之后每 case 只为延迟 trace 发同样的只读请求。
        fixed_memory = await memory_gateway.build_learning_context(
            query=MEMORY_FREEZE_QUERY,
            token_budget=settings.conversation_memory_token_budget,
        )
        fixed_memory_hash = _hash_payload(_canonical_memory_payload(fixed_memory))
        rerank_settings = RerankSettings.from_sources(env_file=ROOT / ".env")
        rerank_client = RerankClient(rerank_settings)
        for index, case in enumerate(cases, start=1):
            trace = await _run_case(
                case=case,
                fixed_memory=fixed_memory,
                fixed_memory_hash=fixed_memory_hash,
                memory_gateway=memory_gateway,
                runtime=runtime,
                context_service=context_service,
                vocabulary=vocabulary,
                rerank_client=rerank_client,
                settings=settings,
                token_counter=token_counter,
            )
            traces.append(trace)
            print(
                f"[full-eval] {index:02}/{len(cases)} {trace['case_id']} "
                f"{trace['status']} {float(trace['total_duration_ms']):.1f} ms",
                flush=True,
            )
    finally:
        if rerank_client is not None:
            rerank_client.close()
        if retrieval_service is not None:
            retrieval_service.close()
        if memory_gateway is not None:
            await memory_gateway.aclose()
        try:
            services.restore()
        except Exception:
            LOG.exception("恢复服务失败")

    settings = Settings()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    trace_path = ROOT / "evals" / f"full_agentic_rag_latency_trace_{timestamp}.json"
    report_path = ROOT / "evals" / f"full_agentic_rag_latency_report_{timestamp}.json"
    trace_payload = {
        "evaluator": "full_agentic_rag_latency",
        "run_at": datetime.now(UTC).isoformat(),
        "account": {"account": USER_ACCOUNT, "user_id": str(USER_ID)},
        "memory_snapshot_hash": fixed_memory_hash,
        "case_count": len(traces),
        "cases": traces,
    }
    trace_path.write_text(
        json.dumps(trace_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    report = build_report(
        traces=traces,
        settings=settings,
        fixed_memory_hash=fixed_memory_hash,
        services=services,
        started_at=started_at,
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"[full-eval] trace: {trace_path}")
    print(f"[full-eval] report: {report_path}")
    total = report["summary"]["total_latency"]
    print(
        "[full-eval] average={avg_ms:.3f} ms p50={p50_ms:.3f} ms "
        "p90={p90_ms:.3f} ms p95={p95_ms:.3f} ms".format(**total)
    )
    return trace_path, report_path


def _parse_args() -> argparse.Namespace:
    """解析评测参数；默认执行完整 70-case 并管理异步服务。"""
    parser = argparse.ArgumentParser(description="执行完整 Agentic RAG 延迟评测")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument(
        "--limit", type=int, default=None, help="仅运行前 N 条，用于连通性 smoke test"
    )
    parser.add_argument(
        "--no-service-management",
        action="store_true",
        help="不停止/恢复 Docker 异步服务（仅用于已有冻结环境）",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    try:
        asyncio.run(
            run_evaluation(
                cases_path=args.cases.resolve(),
                manage_services=not args.no_service_management,
                limit=args.limit,
            )
        )
    except Exception as exc:
        print(f"[full-eval] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
