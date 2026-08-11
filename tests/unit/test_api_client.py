"""MemoryClient 单元测试（§19.8）：HTTP 行为与错误映射。"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from backend.memory.client import MemoryClient, MemoryClientError


def _operation_result_payload(operation_id: Any, status: str = "queued") -> dict[str, Any]:
    return {
        "operation_id": str(operation_id),
        "status": status,
        "operation_type": "conversation_evidence",
        "created_at": "2026-08-10T08:00:00Z",
        "updated_at": "2026-08-10T08:00:00Z",
        "completed_at": None,
        "cancelled_at": None,
        "mutations": [],
        "review_candidate_ids": [],
        "graph_state_changes": [],
        "warnings": [],
        "error": None,
    }


def _mock_transport(recorded: list[httpx.Request], response: httpx.Response):
    def _handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return response

    return httpx.MockTransport(_handler)


async def test_submit_conversation_evidence_sends_service_jwt_and_idempotency_key() -> None:
    operation_id = uuid4()
    recorded: list[httpx.Request] = []
    transport = _mock_transport(
        recorded,
        httpx.Response(202, json=_operation_result_payload(operation_id)),
    )
    async with MemoryClient(
        "http://memory-api",
        token_provider=lambda: "service-jwt",
        http=httpx.AsyncClient(transport=transport, base_url="http://memory-api"),
    ) as client:
        result = await client.submit_conversation_evidence(
            idempotency_key="k-1",
            thread_id="thread-1",
            message_ids=["m1"],
            trigger="explicit_remember",
        )
    assert result.operation_id == operation_id
    assert result.status == "queued"
    request = recorded[0]
    assert request.url.path == "/api/v1/memory/events"
    assert request.headers["Authorization"] == "Bearer service-jwt"
    assert request.headers["Idempotency-Key"] == "k-1"
    body = json.loads(request.content)
    assert body["kind"] == "conversation_evidence"
    assert "user_id" not in body  # 客户端不得注入服务端字段


async def test_submit_activity_evidence() -> None:
    recorded: list[httpx.Request] = []
    transport = _mock_transport(
        recorded,
        httpx.Response(202, json=_operation_result_payload(uuid4())),
    )
    async with MemoryClient(
        "http://memory-api",
        token="static-jwt",
        http=httpx.AsyncClient(transport=transport, base_url="http://memory-api"),
    ) as client:
        await client.submit_activity_evidence(
            idempotency_key="k-2",
            activity_type="page_view",
            activity_ids=["a1"],
        )
    body = json.loads(recorded[0].content)
    assert body["kind"] == "activity_evidence"
    assert body["activity_type"] == "page_view"


async def test_get_operation() -> None:
    operation_id = uuid4()
    recorded: list[httpx.Request] = []
    transport = _mock_transport(
        recorded,
        httpx.Response(200, json=_operation_result_payload(operation_id, "succeeded")),
    )
    async with MemoryClient(
        "http://memory-api",
        token="jwt",
        http=httpx.AsyncClient(transport=transport, base_url="http://memory-api"),
    ) as client:
        result = await client.get_operation(operation_id)
    assert result.status == "succeeded"
    assert recorded[0].method == "GET"


async def test_error_mapping_raises_client_error() -> None:
    recorded: list[httpx.Request] = []
    transport = _mock_transport(
        recorded,
        httpx.Response(
            422,
            json={
                "error": {
                    "code": "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD",
                    "message": "同一幂等键提交了不同的 payload",
                    "retryable": False,
                    "field": None,
                    "trace_id": "ab" * 16,
                }
            },
        ),
    )
    async with MemoryClient(
        "http://memory-api",
        token="jwt",
        http=httpx.AsyncClient(transport=transport, base_url="http://memory-api"),
    ) as client:
        with pytest.raises(MemoryClientError) as exc_info:
            await client.submit_conversation_evidence(
                idempotency_key="k-3",
                thread_id="t",
                message_ids=["m1"],
                trigger="turn_boundary",
            )
    assert exc_info.value.code == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"
    assert exc_info.value.http_status == 422
    assert exc_info.value.trace_id == "ab" * 16


def test_client_requires_token() -> None:
    with pytest.raises(ValueError):
        MemoryClient("http://memory-api")
