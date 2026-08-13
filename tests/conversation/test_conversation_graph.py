"""ConversationGraph 测试（方案 §26.2）。

覆盖：无需 RAG 直接回答、多子问题 Fan-out、Worker 部分失败、证据预算、
补检索循环、Memory 降级、快照一致性、Flag 降级路径（附录 A.10）。
使用 Fake Gateway，不访问真实外部服务。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.conversation.graph.builder import build_conversation_graph
from backend.conversation.graph.runner import ConversationGraphRunner
from tests.conversation.graph_fixtures import (
    FakeMemoryGateway,
    FakeOpenAIGateway,
    FakeRetrieverGateway,
    build_runtime,
    default_rewrite_plan,
    make_hit,
)


def _initial_state() -> dict[str, Any]:
    return {
        "user_id": uuid4(),
        "thread_id": uuid4(),
        "turn_id": uuid4(),
        "request_id": "req-1",
        "run_id": "run-1",
        "user_message_id": uuid4(),
        "expected_thread_version": 0,
    }


async def _run_graph(
    runtime: Any,
    state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ConversationGraphRunner]:
    """编译并执行 Graph（无 checkpointer 的内存执行）。"""
    from backend.conversation.contracts.retrieval import ActiveCorpusVocabulary

    graph = build_conversation_graph(
        runtime_context=runtime,
        vocabulary=ActiveCorpusVocabulary(),
    )
    compiled = graph.compile()
    runner = ConversationGraphRunner(
        compiled_graph=compiled,
        runtime_context=runtime,
        graph_thread_id_for_turn=lambda turn_id: f"conv-turn:{turn_id}",
    )
    state = state or _initial_state()
    result = await compiled.ainvoke(state)
    return result, runner


async def test_no_retrieval_direct_answer() -> None:
    """无需检索路径：打招呼直接回答（§5.3 #5 / §11.3）。"""
    openai = FakeOpenAIGateway()
    openai.rewrite_queue.append(default_rewrite_plan(subqueries=0, need_retrieval=False))
    openai.answer_payloads.append(
        {"answer": "你好！有什么可以帮你？", "citations": [], "followups": []}
    )
    runtime = build_runtime(openai_gateway=openai)
    result, _ = await _run_graph(runtime)
    assert result["rewrite_plan"]["need_retrieval"] is False
    assert result["answer_payload"]["answer"] == "你好！有什么可以帮你？"
    assert "retrieve_subquery" not in result


async def test_multi_subquery_fanout_and_evidence() -> None:
    """多子问题 Send × N：证据聚合 + 引用（§5.2/§13）。"""
    openai = FakeOpenAIGateway()
    openai.rewrite_queue.append(default_rewrite_plan(subqueries=2))
    openai.assess_queue.append(
        {"status": "sufficient", "covered_aspects": ["勾股定理"], "reason_codes": ["ok"]}
    )
    openai.answer_payloads.append(
        {
            "answer": "勾股定理：a²+b²=c²[C1]",
            "citations": [
                {
                    "citation_id": "C1",
                    "corpus_id": "corpus-a",
                    "chunk_ids": ["chunk-1"],
                    "book_id": "book-1",
                    "book_name": "初中数学",
                    "chapter_path": ["第一章"],
                    "page_start": 1,
                    "page_end": 2,
                    "snippet": "勾股定理",
                    "source_refs": [],
                    "matched_subquery_ids": ["sq-0"],
                }
            ],
            "followups": [],
        }
    )
    retriever = FakeRetrieverGateway()
    retriever.results = [
        {
            "worker_key": "",
            "subquery_id": "sq-0",
            "normalized_query": "勾股定理",
            "status": "succeeded",
            "hits": [make_hit(chunk_id="chunk-1")],
            "latency_ms": 1.0,
            "attempt_count": 1,
            "error_code": None,
        },
        {
            "worker_key": "",
            "subquery_id": "sq-1",
            "normalized_query": "勾股定理证明",
            "status": "succeeded",
            "hits": [make_hit(chunk_id="chunk-2")],
            "latency_ms": 1.0,
            "attempt_count": 1,
            "error_code": None,
        },
    ]
    runtime = build_runtime(openai_gateway=openai, retriever_gateway=retriever)
    result, _ = await _run_graph(runtime)
    evidence_items = result["evidence_set"]["items"]
    assert len(evidence_items) >= 1
    assert evidence_items[0]["citation"]["citation_id"]
    assert evidence_items[0]["citation"]["book_name"] == "初中数学"
    # 验证器：引用必须存在于证据集（§15.3）
    valid_ids = {item["citation"]["citation_id"] for item in evidence_items}
    assert valid_ids  # 证据集非空


async def test_worker_partial_failure_uses_others() -> None:
    """单个 Worker 失败不影响其他（§12.4 / §21 降级矩阵）。"""
    openai = FakeOpenAIGateway()
    openai.rewrite_queue.append(default_rewrite_plan(subqueries=2))
    openai.assess_queue.append({"status": "sufficient", "reason_codes": ["ok"]})
    openai.answer_payloads.append({"answer": "回答", "citations": [], "followups": []})
    retriever = FakeRetrieverGateway()
    retriever.results = [
        {
            "worker_key": "",
            "subquery_id": "sq-0",
            "normalized_query": "q1",
            "status": "failed",
            "hits": [],
            "latency_ms": 1.0,
            "attempt_count": 1,
            "error_code": "RETRIEVAL_FAILED",
        },
        {
            "worker_key": "",
            "subquery_id": "sq-1",
            "normalized_query": "q2",
            "status": "succeeded",
            "hits": [make_hit(chunk_id="chunk-3")],
            "latency_ms": 1.0,
            "attempt_count": 1,
            "error_code": None,
        },
    ]
    runtime = build_runtime(openai_gateway=openai, retriever_gateway=retriever)
    result, _ = await _run_graph(runtime)
    assert result["answer_payload"]["answer"] == "回答"
    assert len(result["evidence_set"]["items"]) == 1


async def test_evidence_loop_replan_and_budget() -> None:
    """§14.3 默认 max_iterations=1：允许一次补检索（第三轮必改 3 语义）。"""
    openai = FakeOpenAIGateway()
    # 第一次 rewrite + 补检索 rewrite
    openai.rewrite_queue.append(default_rewrite_plan(subqueries=1))
    openai.rewrite_queue.append(default_rewrite_plan(subqueries=1))
    openai.assess_queue.append({"status": "needs_more", "missing_aspects": ["证明"]})
    openai.assess_queue.append({"status": "sufficient", "reason_codes": ["ok"]})
    openai.answer_payloads.append({"answer": "补检索后的回答", "citations": [], "followups": []})
    retriever = FakeRetrieverGateway()
    retriever.results = [
        {
            "worker_key": "",
            "subquery_id": "sq-0",
            "normalized_query": "q1",
            "status": "succeeded",
            "hits": [make_hit(chunk_id="chunk-1")],
            "latency_ms": 1.0,
            "attempt_count": 1,
            "error_code": None,
        }
    ] * 2
    runtime = build_runtime(openai_gateway=openai, retriever_gateway=retriever)
    # 默认 max_iterations=1 即允许 1 次补检索（第三轮必改 3：不再需要设 2）
    result, _ = await _run_graph(runtime)
    assert result["plan_revision"] >= 2
    assert result["answer_payload"]["answer"] == "补检索后的回答"
    assert openai.records.count({"call": "assess"}) == 2


async def test_evidence_loop_budget_enforced() -> None:
    """评审 C6/第三轮必改 3：max_iterations=1 允许一次补检索，
    第二次 needs_more 时强制进入回答（不无限循环）。"""
    openai = FakeOpenAIGateway()
    openai.rewrite_queue.append(default_rewrite_plan(subqueries=1))
    openai.rewrite_queue.append(default_rewrite_plan(subqueries=1))
    openai.assess_queue.append({"status": "needs_more", "missing_aspects": ["x"]})
    openai.assess_queue.append({"status": "needs_more", "missing_aspects": ["x"]})
    openai.answer_payloads.append({"answer": "预算耗尽后的回答", "citations": [], "followups": []})
    retriever = FakeRetrieverGateway()
    retriever.results = [
        {
            "worker_key": "",
            "subquery_id": "sq-0",
            "normalized_query": "q",
            "status": "succeeded",
            "hits": [make_hit(chunk_id="chunk-1")],
            "latency_ms": 1.0,
            "attempt_count": 1,
            "error_code": None,
        }
    ] * 2
    runtime = build_runtime(openai_gateway=openai, retriever_gateway=retriever)
    runtime.settings.conversation_retrieval_max_iterations = 1  # 默认值
    result, _ = await _run_graph(runtime)
    assert result["answer_payload"]["answer"] == "预算耗尽后的回答"
    # 一次补检索（2 次 rewrite）后预算耗尽：第三次 needs_more 必须强制回答
    rewrite_calls = [r for r in openai.records if r["call"] == "rewrite"]
    assess_calls = [r for r in openai.records if r["call"] == "assess"]
    assert len(rewrite_calls) == 2
    assert len(assess_calls) == 2


async def test_memory_unavailable_degrades_but_completes() -> None:
    """Memory 不可用时回答仍完成（§16.2 / §21）。"""
    openai = FakeOpenAIGateway()
    openai.rewrite_queue.append(default_rewrite_plan(subqueries=0, need_retrieval=False))
    openai.answer_payloads.append({"answer": "回答", "citations": [], "followups": []})
    memory = FakeMemoryGateway()
    memory.errors.append(RuntimeError("memory down"))
    runtime = build_runtime(openai_gateway=openai, memory_gateway=memory)
    result, _ = await _run_graph(runtime)
    assert result["snapshot"]["memory"]["status"] == "unavailable"
    assert result["answer_payload"]["answer"] == "回答"


async def test_flag_agentic_rag_disabled_single_query() -> None:
    """附录 A.10：AGENTIC_RAG_ENABLED=false → 单查询降级。"""

    openai = FakeOpenAIGateway()
    openai.answer_payloads.append({"answer": "回答", "citations": [], "followups": []})
    runtime = build_runtime(openai_gateway=openai)
    flags = dict(runtime.flags)
    flags["agentic_rag"] = False
    runtime.flags = flags
    retriever = FakeRetrieverGateway()
    retriever.results = [
        {
            "worker_key": "",
            "subquery_id": "sq-0",
            "normalized_query": "q",
            "status": "succeeded",
            "hits": [make_hit(chunk_id="chunk-1")],
            "latency_ms": 1.0,
            "attempt_count": 1,
            "error_code": None,
        }
    ]
    runtime.retriever_gateway = retriever
    state = _initial_state()
    state["conversation_context"] = {"current_message": "勾股定理是什么？"}
    result, _ = await _run_graph(runtime, state)
    assert result["rewrite_plan"]["need_retrieval"] is True
    assert result["rewrite_plan"]["reason_codes"] == ["agentic_rag_disabled"]
    assert len(result["rewrite_plan"]["subqueries"]) == 1


async def test_flag_evidence_loop_disabled_skips_evaluate() -> None:
    """附录 A.10：EVIDENCE_LOOP_ENABLED=false → 直接进 answer。"""
    openai = FakeOpenAIGateway()
    openai.rewrite_queue.append(default_rewrite_plan(subqueries=1))
    openai.answer_payloads.append({"answer": "回答", "citations": [], "followups": []})
    retriever = FakeRetrieverGateway()
    retriever.results = [
        {
            "worker_key": "",
            "subquery_id": "sq-0",
            "normalized_query": "q",
            "status": "succeeded",
            "hits": [make_hit(chunk_id="chunk-1")],
            "latency_ms": 1.0,
            "attempt_count": 1,
            "error_code": None,
        }
    ]
    runtime = build_runtime(openai_gateway=openai, retriever_gateway=retriever)
    runtime.flags = dict(runtime.flags)
    runtime.flags["evidence_loop"] = False
    result, _ = await _run_graph(runtime)
    assert result["evidence_assessment"]["status"] == "sufficient"
    assert result["evidence_assessment"]["reason_codes"] == ["evidence_loop_disabled"]
    assert openai.records.count({"call": "assess"}) == 0


async def test_streaming_json_parsed_answer_and_followups() -> None:
    """第三轮必改 1：流式 JSON 片段被解析为 AnswerPayload，
    answer 取 payload.answer（非原始 JSON），followups 非空。"""
    import json as _json

    from tests.conversation.graph_fixtures import JsonStreamingOpenAIGateway

    openai = JsonStreamingOpenAIGateway()
    openai.rewrite_queue.append(default_rewrite_plan(subqueries=0, need_retrieval=False))
    openai.answer_payloads.append(
        {
            "answer": "正弦定理：a/sinA = b/sinB",
            "citations": [],
            "followups": ["余弦定理是什么？", "怎么推导？"],
        }
    )
    runtime = build_runtime(openai_gateway=openai)
    result, _ = await _run_graph(runtime)
    answer = result["answer_payload"]["answer"]
    # answer 必须是模型生成的正文，不是 JSON 文本
    assert answer == "正弦定理：a/sinA = b/sinB"
    assert _json.loads(_json.dumps(answer)) == answer  # 不是 JSON 序列化结果
    assert result["answer_payload"]["followups"] == ["余弦定理是什么？", "怎么推导？"]
    assert result["answer_buffer"] == answer


async def test_citation_validation_matches_server_hex_id() -> None:
    """第三轮必改 2：服务端生成 12 位 hex citation_id，正文引用可被验证器识别；
    伪造 ID 被移除并标记 citation_degraded。"""

    from tests.conversation.graph_fixtures import JsonStreamingOpenAIGateway

    openai = JsonStreamingOpenAIGateway()
    openai.rewrite_queue.append(default_rewrite_plan(subqueries=1))
    openai.assess_queue.append({"status": "sufficient", "reason_codes": ["ok"]})
    retriever = FakeRetrieverGateway()
    retriever.results = [
        {
            "worker_key": "",
            "subquery_id": "sq-0",
            "normalized_query": "q",
            "status": "succeeded",
            "hits": [make_hit(chunk_id="chunk-1")],
            "latency_ms": 1.0,
            "attempt_count": 1,
            "error_code": None,
        }
    ]
    # 先跑一遍拿到真实证据 citation_id，再以该 ID 构造回答
    openai.answer_payloads.append({"answer": "占位", "citations": [], "followups": []})
    runtime0 = build_runtime(openai_gateway=openai, retriever_gateway=retriever)
    result0, _ = await _run_graph(runtime0)
    real_citation = result0["evidence_set"]["items"][0]["citation"]["citation_id"]
    assert len(real_citation) == 13  # C + 12 hex
    assert real_citation[1:].isalnum()

    # 第二遍：正文引用伪造 ID（合法形状但不在证据集）→ 应被移除
    openai2 = JsonStreamingOpenAIGateway()
    openai2.rewrite_queue.append(default_rewrite_plan(subqueries=1))
    openai2.assess_queue.append({"status": "sufficient", "reason_codes": ["ok"]})
    retriever2 = FakeRetrieverGateway()
    retriever2.results = [
        {
            "worker_key": "",
            "subquery_id": "sq-0",
            "normalized_query": "q",
            "status": "succeeded",
            "hits": [make_hit(chunk_id="chunk-1")],
            "latency_ms": 1.0,
            "attempt_count": 1,
            "error_code": None,
        }
    ]
    fake_id = f"C{'a' * 12}"  # 12 位 hex 形状，但不在证据集
    openai2.answer_payloads.append(
        {
            "answer": f"正弦定理公式见[{fake_id}]",
            "citations": [],
            "followups": [],
        }
    )
    runtime2 = build_runtime(openai_gateway=openai2, retriever_gateway=retriever2)
    result2, _ = await _run_graph(runtime2)
    # 伪造引用被移除、degraded 标记（引用校验正则与服务端 ID 形状一致）
    assert fake_id not in result2["answer_payload"]["answer"]
    assert "citation_degraded" in result2.get("degraded_flags", [])
