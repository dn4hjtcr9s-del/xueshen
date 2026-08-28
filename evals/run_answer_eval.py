"""运行共享 Fixture 的 100 条 Answer Eval，并记录最新回答链路的细粒度耗时。

本模块只读调用 Memory、Embedding、RAG 检索和当前 Conversation Answer 链路；
不编译或调用会执行 persist_turn、Memory Submit、ConversationEvidence 或图谱写回的路径。
每条 Case 都使用独立的内存态 Thread/Turn，100 条 Case 复用同一个固定 Memory 快照和图谱状态。
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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.conversation.gateways.embedding import QueryEmbeddingGateway  # noqa: E402
from backend.conversation.gateways.openai import OpenAIGateway  # noqa: E402
from backend.conversation.gateways.retriever import AsyncRetrieverAdapter  # noqa: E402
from backend.conversation.graph.nodes import answer as answer_node  # noqa: E402
from backend.conversation.graph.nodes import context as context_node  # noqa: E402
from backend.conversation.graph.nodes import evidence as evidence_node  # noqa: E402
from backend.conversation.graph.nodes import memory as memory_node  # noqa: E402
from backend.conversation.graph.nodes import retrieval as retrieval_node  # noqa: E402
from backend.conversation.graph.nodes import rewrite as rewrite_node  # noqa: E402
from backend.conversation.graph.nodes import snapshot as snapshot_node  # noqa: E402
from backend.conversation.graph.state import (  # noqa: E402
    ConversationRuntimeContext,
    SystemClock,
    SystemIdGenerator,
)
from backend.conversation.services.context_service import ContextService  # noqa: E402
from backend.conversation.services.corpus_vocabulary import (  # noqa: E402
    ActiveCorpusVocabularyLoader,
)
from backend.conversation.services.token_counter import TokenCounter  # noqa: E402
from backend.rag.database import create_rag_engine  # noqa: E402
from backend.rag.retrieval import RetrievalService  # noqa: E402
from backend.settings import Settings, get_settings  # noqa: E402

LOG = logging.getLogger("answer-eval")
USER_ACCOUNT = "answer_eval_2026"
USER_ID = UUID("2be72e49-22bc-5635-bddb-810acfa32791")
FIXTURE_ID = "answer-eval-shared-state-v1"
EXPECTED_CASE_COUNT = 100
DEFAULT_CASES = ROOT / "evals" / "answer_eval_cases_v1.jsonl"
DEFAULT_FIXTURE = ROOT / "evals" / "answer_eval_fixture_v1.json"
DEFAULT_OUTPUT_DIR = ROOT / "evals"
MEMORY_FREEZE_QUERY = "数学教材学习者长期记忆固定评测快照"
WRITE_SERVICES = (
    "memory-worker",
    "memory-scheduler",
    "memory-outbox-consumer",
    "conversation-worker",
    "conversation-outbox-publisher",
)
INTERNAL_MARKERS = (
    FIXTURE_ID,
    "fixture:",
    "graph_state",
    "memory_id",
    "chunk_id",
    "evidence_id",
    "source_checkpoint_id",
)
CITATION_RE = __import__("re").compile(r"\bC[0-9a-f]{12}\b")


class EvaluationError(RuntimeError):
    """评测配置、链路或只读保护错误。"""


class FixtureMutationError(EvaluationError):
    """共享 Memory/图谱 Fixture 在评测期间发生变化。"""


class ServiceState:
    """冻结评测期间会异步写入 Memory/Conversation 的 Docker 服务，并负责恢复。"""

    def __init__(self, *, manage: bool) -> None:
        self.manage = manage
        self.before: list[str] = []
        self.stopped: list[str] = []
        self.after: list[str] = []
        self.restore_error: str | None = None

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
            line.strip()
            for line in self._compose("ps", "--services", "--filter", "status=running")
            if line.strip()
        )

    def freeze(self) -> None:
        if not self.manage:
            return
        self.before = self._running()
        self.stopped = [service for service in WRITE_SERVICES if service in self.before]
        if self.stopped:
            self._compose("stop", *self.stopped)

    def restore(self) -> None:
        if not self.manage:
            return
        try:
            if self.stopped:
                self._compose("start", *self.stopped)
            self.after = self._running()
        except Exception as exc:  # pragma: no cover - Docker 宿主异常
            self.restore_error = f"{type(exc).__name__}: {exc}"
            raise


class TimedStages:
    """用 perf_counter 记录独占链路阶段耗时，精度保留到 0.001ms。"""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    async def run(self, name: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        started = perf_counter()
        try:
            value = await operation()
        except Exception as exc:
            self.values[name] = {
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "status": "failed",
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            }
            raise
        self.values[name] = {
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "status": "succeeded",
            "level": "stage",
        }
        return value

    def set(self, name: str, started: float, *, level: str = "stage", **fields: Any) -> None:
        self.values[name] = {
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "status": "succeeded",
            "level": level,
            **fields,
        }


def _read_env_file(path: Path) -> dict[str, str]:
    """读取简单 .env，供评测进程复用当前工作树配置。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] == value[-1]
            and value[0] in "'\""
        ):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def configure_environment() -> None:
    """将当前 DeepSeek/Embedding 配置映射到 Conversation 最新链路。"""
    for key, value in _read_env_file(ROOT / ".env").items():
        os.environ.setdefault(key, value)
    mappings = {
        "OPENAI_API_KEY": "DEEPSEEK_API_KEY",
        "OPENAI_BASE_URL": "DEEPSEEK_BASE_URL",
        "OPENAI_REWRITE_MODEL": "DEEPSEEK_MODEL",
        "OPENAI_EVIDENCE_MODEL": "DEEPSEEK_MODEL",
        "OPENAI_ANSWER_MODEL": "DEEPSEEK_MODEL",
        "OPENAI_CONVERSATION_SUMMARY_MODEL": "DEEPSEEK_MODEL",
    }
    for target, source in mappings.items():
        if not os.environ.get(target) and os.environ.get(source):
            os.environ[target] = os.environ[source]
    os.environ.setdefault("MEMORY_API_BASE_URL", "http://127.0.0.1:8001")
    os.environ.setdefault("RAG_DATABASE_URL", "postgresql+psycopg://rag:rag@127.0.0.1:55433/rag")
    os.environ["CONVERSATION_AGENTIC_RAG_ENABLED"] = "true"
    os.environ["CONVERSATION_MULTI_QUERY_ENABLED"] = "true"
    os.environ["CONVERSATION_EVIDENCE_LOOP_ENABLED"] = "true"
    os.environ["CONVERSATION_MEMORY_READ_ENABLED"] = "true"
    os.environ["CONVERSATION_MEMORY_SUBMIT_ENABLED"] = "false"
    os.environ["CONVERSATION_STREAMING_ENABLED"] = "true"
    get_settings.cache_clear()


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _canonical_memory(context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "learner": context.get("learner"),
        "mastery": context.get("mastery") or [],
        "graph_states": context.get("graph_states") or [],
        "recommendations": context.get("recommendations") or [],
        "truncated": bool(context.get("truncated")),
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if len(rows) != EXPECTED_CASE_COUNT:
        raise EvaluationError(f"{path} 必须包含 {EXPECTED_CASE_COUNT} 条 Case，实际 {len(rows)} 条")
    for row in rows:
        if row.get("fixture_id") != FIXTURE_ID:
            raise EvaluationError(f"Case {row.get('case_id')} Fixture ID 不匹配")
        if row.get("case_type") not in {"single_turn", "multi_turn"}:
            raise EvaluationError(f"Case {row.get('case_id')} case_type 非法")
        messages = row.get("conversation") or []
        target_index = int(row.get("target_user_message_index", -1))
        if (
            target_index < 0
            or target_index >= len(messages)
            or messages[target_index].get("role") != "user"
        ):
            raise EvaluationError(f"Case {row.get('case_id')} target_user_message_index 非法")
    return rows


def _load_fixture(path: Path) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture.get("fixture_id") != FIXTURE_ID:
        raise EvaluationError("Fixture ID 不匹配")
    if fixture.get("account", {}).get("user_id") != str(USER_ID):
        raise EvaluationError("Fixture 账号 user_id 不匹配")
    return fixture


def _fixture_memory_context(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """把磁盘 Fixture 规范化为共享的只读 Memory/图谱上下文。"""
    memory = fixture.get("memory") or {}
    return {
        "learner": copy.deepcopy(memory.get("learner") or {}),
        "mastery": copy.deepcopy(memory.get("mastery") or []),
        "graph_states": copy.deepcopy(fixture.get("graph_states") or []),
        "recommendations": [],
        "truncated": False,
    }


def _uuid_for(case_id: str, suffix: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"xueshen-answer-eval:{case_id}:{suffix}")


def _token_count(counter: TokenCounter, value: Any) -> int:
    text_value = (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    )
    try:
        return int(counter.count(text_value))
    except Exception:
        return max(1, math.ceil(len(text_value) / 4)) if text_value else 0


def _object_field(value: Any, name: str, default: Any = None) -> Any:
    """兼容最新链路返回的 dataclass 对象与旧版字典。"""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _case_context(case: Mapping[str, Any]) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """把 Case 的 canonical_prefix 转为最新 Context 节点使用的有界历史。"""
    messages = list(case["conversation"])
    target_index = int(case["target_user_message_index"])
    current = dict(messages[target_index])
    recent: list[dict[str, Any]] = []
    for index, message in enumerate(messages[:target_index]):
        recent.append(
            {
                "message_id": str(_uuid_for(str(case["case_id"]), f"message-{index}")),
                "role": str(message["role"]),
                "sequence": index + 1,
                "content": str(message["content"]),
            }
        )
    context = {
        "current_message": str(current["content"]),
        "recent_messages": recent,
        "conversation_summary": None,
    }
    return context, str(current["content"]), recent


class DevHeaderMemoryGateway:
    """只提供 Memory context 读取，不实现任何写入方法。"""

    def __init__(self, *, base_url: str, user_id: UUID, timeout: float) -> None:
        import httpx

        self._user_id = str(user_id)
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def build_learning_context(
        self, *, query: str, token_budget: int | None = None
    ) -> dict[str, Any]:
        response = await self._http.post(
            "/api/v1/memory/context",
            json={"query": query, "token_budget": token_budget},
            headers={"X-Dev-User-Id": self._user_id},
        )
        if response.status_code >= 400:
            raise EvaluationError(
                f"Memory context 读取失败 HTTP {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise EvaluationError("Memory context 返回不是 JSON 对象")
        return payload

    async def aclose(self) -> None:
        await self._http.aclose()


async def _fixture_db_snapshot(settings: Settings) -> dict[str, Any]:
    """只读抓取账号相关 Memory/图谱行，作为每 Case 写入护栏。"""
    tables = {
        "account_identity_mappings": "internal_user_id",
        "memory_documents": "user_id",
        "memory_index_entries": "user_id",
        "memory_graph_links": "user_id",
        "graph_user_states": "user_id",
        "graph_state_audit": "user_id",
    }
    engine: AsyncEngine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    result: dict[str, Any] = {}
    try:
        async with factory() as session:
            for table, column in tables.items():
                rows = await session.execute(
                    text(f"SELECT to_jsonb(t) AS row FROM {table} AS t WHERE {column} = :uid"),
                    {"uid": str(USER_ID)},
                )
                payload = [dict(row[0]) for row in rows.fetchall()]
                payload.sort(
                    key=lambda item: json.dumps(
                        item, ensure_ascii=False, sort_keys=True, default=str
                    )
                )
                result[table] = payload
    finally:
        await engine.dispose()
    result["counts"] = {table: len(rows) for table, rows in result.items() if table != "counts"}
    result["hash"] = _hash_payload(result)
    return result


def _expected_counts(manifest: Mapping[str, Any]) -> dict[str, int]:
    expected = manifest.get("database_expected_state") or {}
    return {
        "memory_documents": int(expected.get("memory_documents", 10)),
        "memory_index_entries": int(expected.get("memory_index_entries", 9)),
        "memory_graph_links": int(expected.get("active_memory_graph_links", 8)),
        "graph_user_states": int(expected.get("graph_user_states", 8)),
    }


def _snapshot_counts(snapshot: Mapping[str, Any], counter: TokenCounter) -> dict[str, int]:
    return {
        "current_message_tokens": _token_count(counter, str(snapshot.get("current_message") or "")),
        "recent_message_tokens": _token_count(counter, snapshot.get("recent_messages") or []),
        "memory_tokens_estimated": _token_count(counter, snapshot.get("memory") or {}),
        "recent_message_count": len(snapshot.get("recent_messages") or []),
    }


def _evidence_stats(evidence_set: Mapping[str, Any], counter: TokenCounter) -> dict[str, Any]:
    items = list(evidence_set.get("items") or [])
    return {
        "evidence_item_count": len(items),
        "evidence_tokens": int(evidence_set.get("total_tokens") or 0),
        "evidence_chunk_ids": sorted(
            {chunk_id for item in items for chunk_id in (item.get("chunk_ids") or [])}
        ),
        "evidence_text_tokens_estimated": _token_count(
            counter, "\n".join(str(item.get("content_text") or "") for item in items)
        ),
    }


@contextmanager
def _answer_timing_hooks(
    stages: TimedStages, context_service: ContextService, runtime: ConversationRuntimeContext
):
    """不改生产节点源码，临时包裹最新 Answer 节点内部的合同、View 和模型阶段。"""
    original_contract = answer_node.build_answer_contract
    original_view = context_service.build_answer_view
    original_stream = runtime.openai_gateway.stream_answer
    marks: dict[str, float] = {}

    def timed_contract(*args: Any, **kwargs: Any) -> Any:
        started = perf_counter()
        try:
            return original_contract(*args, **kwargs)
        finally:
            stages.set("answer_contract_pack", started, level="substage")

    def timed_view(*args: Any, **kwargs: Any) -> Any:
        started = perf_counter()
        try:
            return original_view(*args, **kwargs)
        finally:
            stages.set("answer_view_build", started, level="substage")

    async def timed_stream(*, answer_context: dict[str, Any]) -> Any:
        marks["answer_context"] = perf_counter()
        started = perf_counter()
        try:
            return await original_stream(answer_context=answer_context)
        finally:
            stages.set(
                "answer_model",
                started,
                level="substage",
                model=getattr(runtime.settings, "openai_answer_model", None),
                answer_context_tokens_estimated=_token_count(runtime.token_counter, answer_context),
            )

    answer_node.build_answer_contract = timed_contract  # type: ignore[assignment]
    context_service.build_answer_view = timed_view  # type: ignore[method-assign]
    runtime.openai_gateway.stream_answer = timed_stream  # type: ignore[method-assign]
    try:
        yield
    finally:
        answer_node.build_answer_contract = original_contract  # type: ignore[assignment]
        context_service.build_answer_view = original_view  # type: ignore[method-assign]
        runtime.openai_gateway.stream_answer = original_stream  # type: ignore[method-assign]


def _quality_checks(case: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    """执行不依赖第二次模型调用的硬性 Answer Eval 检查，不把它冒充语义 Judge。"""
    payload = state.get("answer_payload") or {}
    answer = str(payload.get("answer") or "")
    items = list((state.get("evidence_set") or {}).get("items") or [])
    citations = list(payload.get("citations") or [])
    final_chunk_ids = {str(chunk) for item in items for chunk in item.get("chunk_ids", [])}
    citation_chunk_ids = {
        str(chunk) for citation in citations for chunk in citation.get("chunk_ids", [])
    }
    cited_tokens = set(CITATION_RE.findall(answer))
    allowed_citation_ids = {str(citation.get("citation_id") or "") for citation in citations}
    invalid_citation_tokens = sorted(cited_tokens - allowed_citation_ids)
    internal_markers = sorted(marker for marker in INTERNAL_MARKERS if marker in answer)
    source_text = " ".join(
        [
            str(case.get("expected", {}).get("gold_standalone_question") or ""),
            str(case.get("source", {}).get("topic") or ""),
            str(
                case.get("expected", {}).get("answer_rubric", {}).get("critical_source_excerpt")
                or ""
            ),
        ]
    )
    answer_terms = {term for term in _bigrams(answer) if term.strip()}
    source_terms = {term for term in _bigrams(source_text) if term.strip()}
    overlap = len(answer_terms & source_terms) / max(1, len(source_terms))
    checks = {
        "non_empty_answer": bool(answer.strip()),
        "final_evidence_present": bool(items),
        "citation_payload_present": bool(citations),
        "citation_chunk_ids_allowed": citation_chunk_ids <= final_chunk_ids,
        "citation_tokens_valid": not invalid_citation_tokens,
        "forbidden_internal_markers_absent": not internal_markers,
        "target_turn_answered": bool(answer.strip()),
        "memory_not_exposed_as_authority": not any(
            phrase in answer for phrase in ("长期记忆表明教材", "根据我的记忆，教材定义")
        ),
        "current_request_not_empty": bool(case.get("conversation")),
    }
    hard_failures = [name for name, passed in checks.items() if not passed]
    return {
        "checks": checks,
        "hard_failures": hard_failures,
        "citation_tokens": sorted(cited_tokens),
        "invalid_citation_tokens": invalid_citation_tokens,
        "internal_markers_found": internal_markers,
        "source_term_overlap_ratio": round(overlap, 6),
        "answer_quality_status": "structural_only_unjudged",
        "answer_quality_note": (
            "项目当前没有独立 Answer Judge；已保存 rubric、答案和硬性检查，"
            "未把结构通过冒充语义正确。"
        ),
    }


def _bigrams(value: str) -> list[str]:
    compact = "".join(ch for ch in value if not ch.isspace())
    return [compact[index : index + 2] for index in range(max(0, len(compact) - 1))]


async def _run_case(
    *,
    case: Mapping[str, Any],
    fixed_memory: dict[str, Any],
    fixed_memory_hash: str,
    memory_gateway: DevHeaderMemoryGateway,
    runtime: ConversationRuntimeContext,
    context_service: ContextService,
    vocabulary: Any,
    settings: Settings,
    token_counter: TokenCounter,
    fixture_baseline: Mapping[str, Any],
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    trace_id = uuid.uuid4().hex
    started_total = perf_counter()
    stages = TimedStages()
    errors: list[dict[str, str]] = []
    degraded_flags: list[str] = []
    state: dict[str, Any] = {
        "user_id": USER_ID,
        "thread_id": _uuid_for(case_id, "thread"),
        "turn_id": _uuid_for(case_id, "turn"),
        "user_message_id": _uuid_for(case_id, "target-message"),
        "request_id": trace_id,
        "run_id": trace_id,
        "plan_revision": 0,
        "executed_query_fingerprints": [],
        "worker_results": {},
        "retrieval_iteration": 0,
        "_max_retrieval_iterations": int(settings.conversation_retrieval_max_iterations),
        "degraded_flags": [],
        "evidence_assessment": None,
        "_citations_emitted": True,
    }
    live_after: dict[str, Any] | None = None
    try:
        guard_before_started = perf_counter()
        guard_before = await _fixture_db_snapshot(settings)
        stages.set(
            "fixture_guard_before",
            guard_before_started,
            baseline_hash=fixture_baseline["hash"],
            current_hash=guard_before["hash"],
            unchanged=guard_before["hash"] == fixture_baseline["hash"],
        )
        if guard_before["hash"] != fixture_baseline["hash"]:
            raise FixtureMutationError(f"{case_id} 开始前 Fixture 已变化")

        seed_context, target_message, recent_messages = _case_context(case)

        async def setup() -> None:
            state["snapshot"] = seed_context
            state["conversation_context"] = seed_context

        await stages.run("case_setup", setup)

        async def load_context() -> dict[str, Any]:
            return await context_node.load_conversation_context(
                {"snapshot": seed_context},
                session_factory=None,
                max_messages=settings.conversation_context_max_messages,
            )

        state["conversation_context"] = await stages.run("conversation_context_load", load_context)
        stages.values["conversation_context_load"].update(
            {"history_length": len(state["conversation_context"].get("recent_messages") or [])}
        )

        async def read_memory() -> dict[str, Any]:
            return await memory_node.recall_memory(state, runtime=runtime)

        state.update(await stages.run("memory_read", read_memory))
        live_memory = state.get("memory_context") or {}
        live_memory_hash = _hash_payload(_canonical_memory(live_memory))
        stages.values["memory_read"].update(
            {
                "memory_mode": "shared_fixture_db_read_only",
                "fixture_memory_hash": fixed_memory_hash,
                "memory_read_context_hash": live_memory_hash,
                "learner_present": bool(live_memory.get("learner")),
                "mastery_count": len(live_memory.get("mastery") or []),
                "graph_state_count": len(live_memory.get("graph_states") or []),
                "shared_fixture_state_not_updated": True,
            }
        )

        async def build_snapshot() -> dict[str, Any]:
            return await snapshot_node.build_turn_snapshot(
                state, runtime=runtime, context_service=context_service
            )

        state.update(await stages.run("snapshot_build", build_snapshot))
        stages.values["snapshot_build"].update(
            {
                "snapshot_id": state["snapshot"].get("snapshot_id"),
                "snapshot_hash": state["snapshot_hash"],
                "memory_status": state["snapshot"].get("memory", {}).get("status"),
                "context_counts": _snapshot_counts(state["snapshot"], token_counter),
            }
        )

        max_iterations = int(settings.conversation_retrieval_max_iterations)
        while True:
            iteration = int(state.get("retrieval_iteration") or 0)
            suffix = "" if iteration == 0 else f"_iteration_{iteration}"
            rewrite_name = f"rewrite{suffix}"

            async def rewrite() -> dict[str, Any]:
                return await rewrite_node.rewrite_and_plan(
                    state,
                    runtime=runtime,
                    context_service=context_service,
                    vocabulary=vocabulary,
                    max_subqueries=int(settings.conversation_retrieval_max_subqueries),
                )

            state.update(await stages.run(rewrite_name, rewrite))
            plan = state.get("rewrite_plan") or {}
            subqueries = list(plan.get("subqueries") or [])
            stages.values[rewrite_name].update(
                {
                    "model": settings.openai_rewrite_model,
                    "plan_revision": int(plan.get("plan_revision") or 0),
                    "standalone_question": plan.get("standalone_question"),
                    "answer_mode": plan.get("answer_mode"),
                    "need_retrieval": bool(plan.get("need_retrieval")),
                    "subquery_count": len(subqueries),
                    "subqueries": subqueries,
                }
            )

            if not plan.get("need_retrieval"):
                state["evidence_set"] = {"items": [], "total_tokens": 0}
                state["evidence_hits"] = []
                state["evidence_assessment"] = None
                stages.values.setdefault(
                    "evidence_assessment", {"duration_ms": 0.0, "status": "skipped_no_retrieval"}
                )
                break
            if not subqueries:
                raise EvaluationError("RewritePlan need_retrieval=true 但没有 subqueries")

            embed_name = f"embedding{suffix}"
            state.update(
                await stages.run(
                    embed_name, lambda: retrieval_node.embed_subqueries(state, runtime=runtime)
                )
            )
            stages.values[embed_name].update(
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
                    *(retrieval_node.retrieve_subquery(item, runtime=runtime) for item in inputs)
                )
                merged = dict(state.get("worker_results") or {})
                for result in results:
                    merged.update(result.get("worker_results") or {})
                return {"worker_results": merged, "_results": results}

            retrieval_name = f"retrieval{suffix}"
            retrieval_result = await stages.run(retrieval_name, retrieve_all)
            results = retrieval_result.pop("_results")
            state.update(retrieval_result)
            rows = [
                next(iter(item.get("worker_results", {}).values()))
                for item in results
                if item.get("worker_results")
            ]
            stages.values[retrieval_name].update(
                {
                    "method": "latest_async_hybrid_search",
                    "parallel": True,
                    "subquery_count": len(subqueries),
                    "candidate_count": sum(len(row.get("hits") or []) for row in rows),
                    "worker_statuses": [row.get("status") for row in rows],
                    "per_subquery": [
                        {
                            "subquery_id": row.get("subquery_id"),
                            "status": row.get("status"),
                            "candidate_count": len(row.get("hits") or []),
                            "latency_ms": round(float(row.get("latency_ms") or 0), 3),
                        }
                        for row in rows
                    ],
                }
            )

            fusion_name = f"fusion{suffix}"
            state.update(
                await stages.run(
                    fusion_name, lambda: evidence_node.aggregate_results(state, runtime=runtime)
                )
            )
            stages.values[fusion_name].update(
                {
                    "deduplicated_candidate_count": len(state.get("evidence_hits") or []),
                    "matched_subquery_count": sum(
                        1
                        for ids in (state.get("matched_subquery_ids") or {}).values()
                        if len(ids) > 1
                    ),
                }
            )

            rerank_name = f"deterministic_rerank_and_evidence_pack{suffix}"
            state.update(
                await stages.run(
                    rerank_name,
                    lambda: evidence_node.deduplicate_and_rerank(
                        state, runtime=runtime, settings=settings, token_counter=token_counter
                    ),
                )
            )
            stages.values[rerank_name].update(
                _evidence_stats(state.get("evidence_set") or {}, token_counter)
            )

            assess_name = f"evidence_assessment{suffix}"
            state.update(
                await stages.run(
                    assess_name, lambda: evidence_node.evaluate_evidence(state, runtime=runtime)
                )
            )
            assessment = state.get("evidence_assessment") or {}
            stages.values[assess_name].update(
                {
                    "model": settings.openai_evidence_model,
                    "status_value": assessment.get("status"),
                    "covered_aspect_count": len(assessment.get("covered_aspects") or []),
                    "missing_aspect_count": len(assessment.get("missing_aspects") or []),
                    "reason_codes": assessment.get("reason_codes") or [],
                }
            )
            if assessment.get("status") == "needs_more" and iteration < max_iterations:
                state["retrieval_iteration"] = iteration + 1
                continue
            break

        async def answer_run() -> dict[str, Any]:
            with _answer_timing_hooks(stages, context_service, runtime):
                return await answer_node.generate_answer(
                    state, runtime=runtime, context_service=context_service
                )

        answer_started = perf_counter()
        state.update(await answer_run())
        stages.set(
            "answer",
            answer_started,
            model=settings.openai_answer_model,
            output_chars=len(str(state.get("answer_buffer") or "")),
            output_tokens_estimated=_token_count(token_counter, state.get("answer_buffer") or ""),
            citation_count=len((state.get("answer_payload") or {}).get("citations") or []),
        )
        context_duration = sum(
            float(stages.values.get(name, {}).get("duration_ms") or 0)
            for name in ("answer_contract_pack", "answer_view_build")
        )
        stages.values["answer_context_pack"] = {
            "duration_ms": round(context_duration, 3),
            "status": "succeeded",
            "level": "substage",
            "sub_stages": ["answer_contract_pack", "answer_view_build"],
        }

        validation_started = perf_counter()
        state.update(await answer_node.validate_answer_and_citations(state, runtime=runtime))
        stages.set(
            "citation_validation",
            validation_started,
            degraded_flags=state.get("degraded_flags") or [],
            valid_citation_count=len((state.get("answer_payload") or {}).get("citations") or []),
        )
        degraded_flags.extend(str(flag) for flag in state.get("degraded_flags") or [])
        status = "succeeded"
    except Exception as exc:
        status = "failed"
        errors.append({"type": type(exc).__name__, "message": str(exc)[:1000]})
        LOG.exception("Case %s 执行失败", case_id)

    guard_after_started = perf_counter()
    try:
        live_after = await _fixture_db_snapshot(settings)
    except Exception as exc:
        stages.set(
            "fixture_guard_after",
            guard_after_started,
            status="failed",
            error_type=type(exc).__name__,
            error_message=str(exc)[:800],
        )
        errors.append(
            {"type": type(exc).__name__, "message": f"评测后 Fixture 检查失败: {str(exc)[:800]}"}
        )
        status = "failed"
    if live_after is not None:
        stages.set(
            "fixture_guard_after",
            guard_after_started,
            baseline_hash=fixture_baseline["hash"],
            current_hash=live_after["hash"],
            unchanged=live_after["hash"] == fixture_baseline["hash"],
        )
        if live_after["hash"] != fixture_baseline["hash"]:
            raise FixtureMutationError(f"{case_id} 评测后共享 Fixture hash 发生变化")

    total_ms = (perf_counter() - started_total) * 1000
    answer_payload = state.get("answer_payload") or {}
    quality = _quality_checks(case, state)
    return {
        "case_id": case_id,
        "case_type": case.get("case_type"),
        "fixture_id": FIXTURE_ID,
        "trace_id": trace_id,
        "user_account": USER_ACCOUNT,
        "user_id": str(USER_ID),
        "conversation_history_length": len(recent_messages),
        "target_user_message": target_message,
        "status": status,
        "snapshot_id": state.get("snapshot", {}).get("snapshot_id"),
        "snapshot_hash": state.get("snapshot_hash"),
        "memory_snapshot_hash": fixed_memory_hash,
        "memory_status": state.get("snapshot", {}).get("memory", {}).get("status"),
        "rewrite_plan": state.get("rewrite_plan"),
        "standalone_question": (state.get("rewrite_plan") or {}).get("standalone_question"),
        "subqueries": (state.get("rewrite_plan") or {}).get("subqueries") or [],
        "retrieval_iteration_count": int(state.get("retrieval_iteration") or 0),
        "retrieved_chunks": [
            {
                "chunk_id": _object_field(hit, "chunk_id"),
                "book_id": _object_field(hit, "book_id"),
                "page_start": _object_field(hit, "source_page_start"),
                "page_end": _object_field(hit, "source_page_end"),
                "score": _object_field(hit, "score"),
            }
            for hit in (state.get("evidence_hits") or [])
        ],
        "final_evidence": (state.get("evidence_set") or {}).get("items") or [],
        "evidence_assessment": state.get("evidence_assessment"),
        "answer": answer_payload.get("answer"),
        "citations": answer_payload.get("citations") or [],
        "followups": answer_payload.get("followups") or [],
        "rubric": case.get("expected", {}).get("answer_rubric"),
        "deterministic_checks": quality,
        "answer_quality_status": quality["answer_quality_status"],
        "degraded_flags": sorted(set(degraded_flags)),
        "stages": stages.values,
        "total_duration_ms": round(total_ms, 3),
        "accounted_stage_duration_ms": round(
            sum(
                float(item.get("duration_ms") or 0)
                for item in stages.values.values()
                if item.get("level", "stage") == "stage"
            ),
            3,
        ),
        "errors": errors,
    }


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)


def _latency(values: Sequence[float]) -> dict[str, Any]:
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


def _stage_summary(traces: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = sum(float(trace.get("total_duration_ms") or 0) for trace in traces)
    summary: dict[str, Any] = {}
    for level in ("stage", "substage"):
        names = sorted(
            {
                name
                for trace in traces
                for name, item in (trace.get("stages") or {}).items()
                if item.get("level", "stage") == level
            }
        )
        level_summary: dict[str, Any] = {}
        for name in names:
            values = [
                float((trace.get("stages") or {}).get(name, {}).get("duration_ms") or 0)
                for trace in traces
                if name in (trace.get("stages") or {})
                and (trace.get("stages") or {}).get(name, {}).get("level", "stage") == level
            ]
            item = _latency(values)
            item["total_ms"] = round(sum(values), 3)
            item["share_of_total"] = round(sum(values) / total, 6) if total else 0.0
            level_summary[name] = item
        summary[level] = level_summary
    return summary


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_state() -> dict[str, Any]:
    result = subprocess.run(
        ["git", "diff", "--no-ext-diff"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    status = subprocess.run(
        ["git", "status", "--short"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return {
        "head": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip(),
        "working_tree_diff_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
        "status_sha256": hashlib.sha256(status.stdout.encode()).hexdigest(),
    }


def build_report(
    *,
    traces: Sequence[Mapping[str, Any]],
    settings: Settings,
    fixture: Mapping[str, Any],
    fixture_hash: str,
    fixture_baseline: Mapping[str, Any],
    services: ServiceState,
    started_at: str,
    cases_path: Path,
    fixture_path: Path,
) -> dict[str, Any]:
    succeeded = [trace for trace in traces if trace.get("status") == "succeeded"]
    single = [trace for trace in traces if trace.get("case_type") == "single_turn"]
    multi = [trace for trace in traces if trace.get("case_type") == "multi_turn"]
    structural_failures = sum(
        bool((trace.get("deterministic_checks") or {}).get("hard_failures")) for trace in traces
    )
    return {
        "evaluator": "answer_eval_latest_chain_read_only",
        "run_at": datetime.now(UTC).isoformat(),
        "started_at": started_at,
        "chain_version": "working-tree-latest",
        "dataset": {
            "cases_path": str(cases_path.relative_to(ROOT)),
            "fixture_path": str(fixture_path.relative_to(ROOT)),
            "case_count": len(traces),
            "single_turn": sum(trace.get("case_type") == "single_turn" for trace in traces),
            "multi_turn": sum(trace.get("case_type") == "multi_turn" for trace in traces),
            "cases_sha256": _hash_file(cases_path),
            "fixture_sha256": fixture_hash,
        },
        "account": {
            "username": USER_ACCOUNT,
            "user_id": str(USER_ID),
            "fixture_id": FIXTURE_ID,
        },
        "execution": {
            "chain": [
                "case_setup",
                "memory_read",
                "conversation_context_load",
                "snapshot_build",
                "rewrite_and_plan",
                "embedding",
                "retrieval_parallel_hybrid",
                "fusion",
                "deterministic_rerank_and_evidence_pack",
                "evidence_assessment",
                "answer_contract_pack",
                "answer_view_build",
                "answer_model",
                "answer",
                "citation_validation",
            ],
            "latest_production_answer_gateway": True,
            "answer_gateway_mode": "Responses Structured Output AnswerGenerationOutput",
            "memory_mode": "shared_fixture_read_only_frozen_snapshot",
            "writes_disabled": [
                "persist_turn",
                "memory_ack",
                "memory_submit",
                "conversation_evidence_submit",
                "graph_state_update",
                "conversation_outbox_write",
            ],
            "not_executed": ["ConversationGraph persist_turn edge", "Answer Judge LLM call"],
            "feature_flags": settings.conversation_flags | {"memory_submit": False},
        },
        "models": {
            "rewrite": settings.openai_rewrite_model,
            "evidence": settings.openai_evidence_model,
            "answer": settings.openai_answer_model,
            "embedding": settings.embedding_model,
            "embedding_dimensions": settings.rag_embedding_dimensions,
            "openai_base_url": settings.openai_base_url,
        },
        "source_versions": {
            "git": _git_state(),
            "prompt_files": {
                "backend/conversation/graph/prompts.py": _hash_file(
                    ROOT / "backend/conversation/graph/prompts.py"
                ),
                "backend/conversation/graph/nodes/answer.py": _hash_file(
                    ROOT / "backend/conversation/graph/nodes/answer.py"
                ),
                "backend/conversation/graph/nodes/evidence.py": _hash_file(
                    ROOT / "backend/conversation/graph/nodes/evidence.py"
                ),
                "backend/conversation/graph/nodes/rewrite.py": _hash_file(
                    ROOT / "backend/conversation/graph/nodes/rewrite.py"
                ),
                "backend/conversation/services/answer_context.py": _hash_file(
                    ROOT / "backend/conversation/services/answer_context.py"
                ),
                "backend/conversation/gateways/openai.py": _hash_file(
                    ROOT / "backend/conversation/gateways/openai.py"
                ),
            },
        },
        "fixture_guard": {
            "baseline_hash": fixture_baseline.get("hash"),
            "baseline_counts": fixture_baseline.get("counts"),
            "expected_counts": _expected_counts(
                {
                    "database_expected_state": {
                        "memory_documents": 10,
                        "memory_index_entries": 9,
                        "active_memory_graph_links": 8,
                        "graph_user_states": 8,
                    }
                }
            ),
            "final_hash": fixture_baseline.get("hash"),
            "mutation_detected": False,
        },
        "services": {
            "before_running": services.before,
            "stopped_for_eval": services.stopped,
            "after_running": services.after,
            "restore_error": services.restore_error,
        },
        "summary": {
            "case_count": len(traces),
            "succeeded": len(succeeded),
            "failed": len(traces) - len(succeeded),
            "single_turn": {
                "count": len(single),
                "succeeded": sum(t.get("status") == "succeeded" for t in single),
                "latency": _latency([float(t.get("total_duration_ms") or 0) for t in single]),
            },
            "multi_turn": {
                "count": len(multi),
                "succeeded": sum(t.get("status") == "succeeded" for t in multi),
                "latency": _latency([float(t.get("total_duration_ms") or 0) for t in multi]),
            },
            "total_latency": _latency([float(t.get("total_duration_ms") or 0) for t in succeeded]),
            "stage_latency": _stage_summary(succeeded),
            "structural_hard_failure_cases": structural_failures,
            "answer_quality_status": "structural_only_unjudged",
            "answer_quality_note": (
                "每 Case 保留 expected.answer_rubric 与候选答案；当前项目没有独立 "
                "Answer Judge，因此没有将结构校验结果标为语义通过。"
            ),
            "degraded_case_count": sum(bool(t.get("degraded_flags")) for t in traces),
            "memory_mismatch_cases": sum(
                "memory_live_snapshot_mismatch_ignored" in (t.get("degraded_flags") or [])
                for t in traces
            ),
        },
        "slow_cases": sorted(
            [
                {
                    "case_id": t.get("case_id"),
                    "total_duration_ms": t.get("total_duration_ms"),
                    "status": t.get("status"),
                    "stage_durations_ms": {
                        k: v.get("duration_ms") for k, v in (t.get("stages") or {}).items()
                    },
                }
                for t in traces
            ],
            key=lambda item: float(item.get("total_duration_ms") or 0),
            reverse=True,
        )[:20],
    }


async def run(args: argparse.Namespace) -> tuple[Path, Path]:
    configure_environment()
    settings = get_settings()
    cases_path = args.cases.resolve()
    fixture_path = args.fixture.resolve()
    cases = _load_jsonl(cases_path)
    fixture = _load_fixture(fixture_path)
    fixture_hash = _hash_file(fixture_path)
    services = ServiceState(manage=not args.no_service_management)
    traces: list[dict[str, Any]] = []
    memory_gateway: DevHeaderMemoryGateway | None = None
    retrieval_service: RetrievalService | None = None
    fixture_baseline: dict[str, Any] | None = None
    started_at = datetime.now(UTC).isoformat()

    services.freeze()
    try:
        fixture_baseline = await _fixture_db_snapshot(settings)
        expected = _expected_counts(
            json.loads(
                (ROOT / "evals" / "answer_eval_manifest_v1.json").read_text(encoding="utf-8")
            )
        )
        for table, count in expected.items():
            actual = int((fixture_baseline.get("counts") or {}).get(table, -1))
            if actual != count:
                raise EvaluationError(f"Fixture 表 {table} 期望 {count} 行，实际 {actual} 行")

        from backend.conversation.services.token_counter import TokenCounter

        token_counter = TokenCounter()
        token_counter.count("预热")
        context_service = ContextService(settings=settings, token_counter=token_counter)
        memory_gateway = DevHeaderMemoryGateway(
            base_url=settings.memory_api_base_url,
            user_id=USER_ID,
            timeout=max(settings.memory_context_timeout_seconds, 10.0),
        )
        fixed_memory = _fixture_memory_context(fixture)
        fixed_memory_hash = _hash_payload(_canonical_memory(fixed_memory))

        openai_gateway = OpenAIGateway(settings=settings, logger=LOG)
        embedding_gateway = QueryEmbeddingGateway(settings=settings, logger=LOG)
        retrieval_service = RetrievalService(settings=None)
        retriever_gateway = AsyncRetrieverAdapter(
            retrieval_service=retrieval_service,
            concurrency=settings.conversation_retrieval_concurrency,
            worker_timeout_seconds=settings.conversation_retrieval_worker_timeout_seconds,
            logger=LOG,
        )
        runtime = ConversationRuntimeContext(
            openai_gateway=openai_gateway,
            memory_gateway=memory_gateway,
            embedding_gateway=embedding_gateway,
            retriever_gateway=retriever_gateway,
            conversation_repository=None,
            turn_event_writer=None,
            clock=SystemClock(),
            id_generator=SystemIdGenerator(),
            logger=LOG,
            flags={**settings.conversation_flags, "memory_submit": False},
        )
        runtime.settings = settings
        runtime.context_service = context_service
        runtime.token_counter = token_counter

        rag_engine = create_rag_engine(settings=None)
        vocabulary_loader = ActiveCorpusVocabularyLoader(rag_engine)
        failures = vocabulary_loader.validate_embedding_profile(
            model=settings.embedding_model,
            dimensions=settings.rag_embedding_dimensions,
        )
        if failures:
            raise EvaluationError(
                "Embedding profile 与 active corpus 不匹配: " + "; ".join(failures)
            )
        vocabulary = vocabulary_loader.load()

        for index, case in enumerate(cases, start=1):
            trace = await _run_case(
                case=case,
                fixed_memory=fixed_memory,
                fixed_memory_hash=fixed_memory_hash,
                memory_gateway=memory_gateway,
                runtime=runtime,
                context_service=context_service,
                vocabulary=vocabulary,
                settings=settings,
                token_counter=token_counter,
                fixture_baseline=fixture_baseline,
            )
            traces.append(trace)
            print(
                f"[answer-eval] {index:03}/{len(cases)} {trace['case_id']} "
                f"{trace['status']} {trace['total_duration_ms']:.3f} ms",
                flush=True,
            )
    finally:
        if retrieval_service is not None:
            retrieval_service.close()
        if memory_gateway is not None:
            await memory_gateway.aclose()
        try:
            services.restore()
        except Exception:
            LOG.exception("恢复异步服务失败")

    if fixture_baseline is None:
        raise EvaluationError("没有生成 Fixture 基线")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    trace_path = output_dir / f"answer_eval_trace_{timestamp}.json"
    report_path = output_dir / f"answer_eval_report_{timestamp}.json"
    trace_path.write_text(
        json.dumps(
            {
                "evaluator": "answer_eval_latest_chain_read_only",
                "run_at": datetime.now(UTC).isoformat(),
                "account": {"username": USER_ACCOUNT, "user_id": str(USER_ID)},
                "fixture_id": FIXTURE_ID,
                "fixture_sha256": fixture_hash,
                "case_count": len(traces),
                "cases": traces,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    report = build_report(
        traces=traces,
        settings=settings,
        fixture=fixture,
        fixture_hash=fixture_hash,
        fixture_baseline=fixture_baseline,
        services=services,
        started_at=started_at,
        cases_path=cases_path,
        fixture_path=fixture_path,
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"[answer-eval] trace: {trace_path}")
    print(f"[answer-eval] report: {report_path}")
    return trace_path, report_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行共享 Fixture 的 100 条只读 Answer Eval")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--no-service-management", action="store_true")
    parser.add_argument(
        "--limit", type=int, default=None, help="仅用于 smoke；正式运行不要传此参数"
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = _parse_args()
    if arguments.limit is not None:
        raise SystemExit("正式 Runner 不接受 --limit；请通过单独 smoke 脚本验证")
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    try:
        asyncio.run(run(arguments))
    except Exception as exc:
        LOG.exception("Answer Eval 失败: %s", exc)
        raise SystemExit(1) from exc
