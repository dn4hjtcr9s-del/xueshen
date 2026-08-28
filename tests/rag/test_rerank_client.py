"""Rerank 评测客户端的离线单元测试。"""

from __future__ import annotations

import json

import httpx
import pytest

from evals.rerank import (
    MATHEMATICS_RERANK_INSTRUCT,
    RERANK_DOCUMENT_STRATEGY,
    RERANK_QUERY_STRATEGY,
    RerankClient,
    RerankRequestError,
    RerankSettings,
)


def test_rerank_uses_configured_instruct_and_preserves_service_order() -> None:
    """请求应携带数学教材指令，并按服务返回索引顺序解析结果。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.84},
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reranker = RerankClient(
        RerankSettings(
            base_url="https://rerank.example/v1/reranks",
            model="qwen3-rerank",
            api_key="test-secret",
        ),
        client=client,
    )

    results = reranker.rerank(query="什么是极限？", documents=["文档一", "文档二"], top_n=2)

    assert [(item.index, item.relevance_score) for item in results] == [(1, 0.91), (0, 0.84)]
    assert captured["authorization"] == "Bearer test-secret"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["instruct"] == MATHEMATICS_RERANK_INSTRUCT
    assert payload["query"] == "什么是极限？"
    assert payload["documents"] == ["文档一", "文档二"]
    assert payload["return_documents"] is False
    client.close()


def test_rerank_rejects_duplicate_result_indexes() -> None:
    """服务重复返回同一候选时必须失败，不能静默篡改排名。"""
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 0, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.8},
                    ]
                },
            )
        )
    )
    reranker = RerankClient(
        RerankSettings(
            base_url="https://rerank.example/v1/reranks",
            model="qwen3-rerank",
            api_key="test-secret",
        ),
        client=client,
    )

    with pytest.raises(RerankRequestError, match="重复候选索引"):
        reranker.rerank(query="问题", documents=["文档一", "文档二"], top_n=2)

    client.close()


def test_rerank_input_strategy_keeps_original_question_and_chunk() -> None:
    """真实对比后，Rerank 输入不再追加主题或候选元数据。"""
    assert RERANK_QUERY_STRATEGY == "raw-query/no-rewrite/v3"
    assert RERANK_DOCUMENT_STRATEGY == "raw-content-text/no-metadata-prefix/v3"
