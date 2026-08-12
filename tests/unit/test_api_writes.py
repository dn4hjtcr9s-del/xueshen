"""写接口 Gateway 单元测试（§19.1–§19.3 / §4.5 / §14.2 / §18.5 / §23.5）。

覆盖：幂等键规则、外部注入字段拒绝、actor 证据边界、限流、expert 禁止、
P0/P1 快速路径（200/202）、取消规则、跨用户 IDOR。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from backend.memory.api import dependencies
from backend.settings import Settings
from tests.unit.api_fakes import FakeRunner, build_test_app

USER_ID = uuid4()
OTHER_USER_ID = uuid4()

EVENT_BODY = {
    "kind": "conversation_evidence",
    "thread_id": "thread-1",
    "message_ids": ["m1", "m2"],
    "trigger": "explicit_remember",
}


def _settings(tmp_path: Any, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "development",
        "dev_auth_enabled": True,
        "memory_storage_root": str(tmp_path / "storage"),
    }
    base.update(overrides)
    return Settings(**base)


def _auth(user_id: UUID = USER_ID, **extra: str) -> dict[str, str]:
    return {"X-Dev-User-Id": str(user_id), **extra}


def _post_event(client: TestClient, key: str, body: dict[str, Any] | None = None, **headers: str):
    return client.post(
        "/api/v1/memory/events",
        json=body or EVENT_BODY,
        headers={"Idempotency-Key": key, **_auth(), **headers},
    )


# ---------------------------------------------------------------------------
# 幂等与注入（§4.5 / §5.3）
# ---------------------------------------------------------------------------


def test_submit_event_returns_202_and_persists(tmp_path: Any, monkeypatch: Any) -> None:
    app, store, _, _ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = _post_event(client, "k-1")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["operation_type"] == "conversation_evidence"
    row = store.rows[UUID(body["operation_id"])]
    assert row["user_id"] == USER_ID  # user_id 由认证上下文注入
    assert row["actor_type"] == "user"
    assert row["priority"] == 50  # P2（§5.2）
    assert row["graph_thread_id"] == f"memory-op:{row['operation_id']}"
    assert len(row["trace_id"]) == 32


def test_account_purge_blocks_new_operation(tmp_path: Any, monkeypatch: Any) -> None:
    """§21.3 步骤 1（评审 P0-1）：账号删除 manifest 存在时写路径返回 409。"""
    from backend.memory.contracts.common import user_privacy_hash

    settings = _settings(tmp_path)
    app, store, _, _ = build_test_app(settings, monkeypatch=monkeypatch)
    store.manifests[user_privacy_hash(settings.privacy_hmac_key, str(USER_ID))] = {
        "status": "requested"
    }
    client = TestClient(app)
    response = _post_event(client, "k-blocked")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ACCOUNT_PURGE_IN_PROGRESS"
    assert store.rows == {}


def test_missing_idempotency_key_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.post("/api/v1/memory/events", json=EVENT_BODY, headers=_auth())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_IDEMPOTENCY_KEY"


def test_control_char_idempotency_key_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.post(
        "/api/v1/memory/events",
        json=EVENT_BODY,
        headers={**_auth(), "Idempotency-Key": "bad\nkey"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_IDEMPOTENCY_KEY"


def test_idempotent_replay_returns_same_operation(tmp_path: Any, monkeypatch: Any) -> None:
    app, store, _, _ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    first = _post_event(client, "k-dup")
    second = _post_event(client, "k-dup")
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["operation_id"] == second.json()["operation_id"]
    assert len(store.rows) == 1


def test_same_key_different_payload_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    assert _post_event(client, "k-conflict").status_code == 202
    changed = {**EVENT_BODY, "thread_id": "thread-2"}
    response = _post_event(client, "k-conflict", body=changed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"


def test_injected_gateway_fields_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    """user_id/actor_type/priority/graph_thread_id/operation_id 注入一律 422（§5.3）。"""
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    for field in ("user_id", "actor_type", "priority", "graph_thread_id", "operation_id"):
        response = _post_event(client, f"k-inject-{field}", body={**EVENT_BODY, field: "x"})
        assert response.status_code == 422, field
        assert response.json()["error"]["code"] == "REQUEST_EXTRA_FIELD", field


def test_invalid_payload_returns_422_invalid_payload(tmp_path: Any, monkeypatch: Any) -> None:
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = _post_event(client, "k-bad", body={**EVENT_BODY, "trigger": "not_a_trigger"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PAYLOAD"


# ---------------------------------------------------------------------------
# actor 证据边界（§18.3）
# ---------------------------------------------------------------------------


def test_user_can_only_submit_explicit_remember(tmp_path: Any, monkeypatch: Any) -> None:
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = _post_event(client, "k-turn", body={**EVENT_BODY, "trigger": "turn_boundary"})
    assert response.status_code == 403


def test_user_cannot_submit_activity_evidence(tmp_path: Any, monkeypatch: Any) -> None:
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = _post_event(
        client,
        "k-act",
        body={"kind": "activity_evidence", "activity_type": "page_view", "activity_ids": ["a1"]},
    )
    assert response.status_code == 403


def test_conversation_agent_cannot_submit_activity(tmp_path: Any, monkeypatch: Any) -> None:
    app, *_ = build_test_app(
        _settings(tmp_path, dev_auth_allow_scope_override=True), monkeypatch=monkeypatch
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/memory/events",
        json={"kind": "activity_evidence", "activity_type": "page_view", "activity_ids": ["a1"]},
        headers={
            "Idempotency-Key": "k-agent",
            **_auth(
                **{
                    "X-Dev-Actor-Type": "conversation_agent",
                    "X-Dev-Scopes": "memory:submit_evidence",
                }
            ),
        },
    )
    assert response.status_code == 403


def test_activity_agent_submit_returns_202(tmp_path: Any, monkeypatch: Any) -> None:
    app, store, _, _ = build_test_app(
        _settings(tmp_path, dev_auth_allow_scope_override=True), monkeypatch=monkeypatch
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/memory/events",
        json={"kind": "activity_evidence", "activity_type": "page_view", "activity_ids": ["a1"]},
        headers={
            "Idempotency-Key": "k-agent-2",
            **_auth(
                **{"X-Dev-Actor-Type": "activity_agent", "X-Dev-Scopes": "memory:submit_evidence"}
            ),
        },
    )
    assert response.status_code == 202
    row = next(iter(store.rows.values()))
    assert row["actor_type"] == "activity_agent"
    assert row["priority"] == 20  # P3（§5.2）


# ---------------------------------------------------------------------------
# 限流（§18.5）
# ---------------------------------------------------------------------------


def test_write_rate_limit_returns_429(tmp_path: Any, monkeypatch: Any) -> None:
    app, *_ = build_test_app(
        _settings(tmp_path, rate_limit_write_per_minute=2), monkeypatch=monkeypatch
    )
    client = TestClient(app)
    assert _post_event(client, "k-rl-1").status_code == 202
    assert _post_event(client, "k-rl-2").status_code == 202
    response = _post_event(client, "k-rl-3")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.json()["error"]["retryable"] is True


def test_graph_state_rate_limit_independent_bucket(tmp_path: Any, monkeypatch: Any) -> None:
    """图谱标记限流 bucket 独立（§18.5），与写 bucket 分开计数。"""
    from backend.memory.api import graph_states as graph_states_module

    class _FakeRegistry:
        def __init__(self, session: Any) -> None:
            pass

        async def node_exists(self, node_id: str) -> bool:
            return True

    monkeypatch.setattr(graph_states_module, "KnowledgeGraphRegistry", _FakeRegistry)
    app, *_ = build_test_app(
        _settings(tmp_path, rate_limit_graph_state_per_minute=1), monkeypatch=monkeypatch
    )
    client = TestClient(app)
    headers = {"Idempotency-Key": "k-g1", **_auth()}
    body = {"action": "mark_familiar"}
    first = client.put("/api/v1/knowledge-graph/me/nodes/n001/state", json=body, headers=headers)
    assert first.status_code == 200  # 第一次通过限流并走快速路径
    second = client.put(
        "/api/v1/knowledge-graph/me/nodes/n001/state",
        json=body,
        headers={**headers, "Idempotency-Key": "k-g2"},
    )
    assert second.status_code == 429


# ---------------------------------------------------------------------------
# 图谱状态写（§19.5 / §6.4）
# ---------------------------------------------------------------------------


def test_expert_action_rejected_with_dedicated_code(tmp_path: Any, monkeypatch: Any) -> None:
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.put(
        "/api/v1/knowledge-graph/me/nodes/n001/state",
        json={"action": "expert"},
        headers={"Idempotency-Key": "k-expert", **_auth()},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "GRAPH_STATUS_NOT_USER_SETTABLE"


def test_graph_put_extra_field_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    """客户端传入 kind/node_id（即使与路径一致）一律 422 REQUEST_EXTRA_FIELD（§6.4）。"""
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    for extra in ({"node_id": "n001"}, {"kind": "set_graph_state"}):
        response = client.put(
            "/api/v1/knowledge-graph/me/nodes/n001/state",
            json={"action": "mark_familiar", **extra},
            headers={"Idempotency-Key": "k-extra", **_auth()},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "REQUEST_EXTRA_FIELD"


def test_graph_node_id_pattern_validated(tmp_path: Any, monkeypatch: Any) -> None:
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.put(
        "/api/v1/knowledge-graph/me/nodes/not-a-node/state",
        json={"action": "mark_familiar"},
        headers={"Idempotency-Key": "k-node", **_auth()},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PAYLOAD"


# ---------------------------------------------------------------------------
# P0/P1 快速路径（§14.2）
# ---------------------------------------------------------------------------

_LEARNER_BODY = {"preferences": ["喜欢例题驱动"], "goals": [], "plans": []}


def test_fast_path_completes_within_window_returns_200(tmp_path: Any, monkeypatch: Any) -> None:
    app, _store, runner, _ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.put(
        "/api/v1/memory/learner",
        json=_LEARNER_BODY,
        headers={"Idempotency-Key": "k-fast", **_auth()},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["operation_type"] == "override_learner_profile"
    assert len(runner.calls) == 1
    operation = runner.calls[0]
    assert operation.priority == 100  # P0（§5.2）
    assert operation.graph_thread_id == f"memory-op:{operation.operation_id}"


def test_fast_path_timeout_returns_202_and_runner_continues(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """2 秒窗口内未完成 → 202；Runner 不被取消，后台完成后落库（§14.2 第 6 条）。"""
    monkeypatch.setattr(dependencies, "FAST_PATH_TIMEOUT_SECONDS", 0.2)
    app, store, _, _ = build_test_app(
        _settings(tmp_path), monkeypatch=monkeypatch, runner=FakeRunner(delay=1.0)
    )
    # 持续 portal 让后台 Runner 在请求返回后继续执行
    with TestClient(app) as client:
        response = client.put(
            "/api/v1/memory/learner",
            json=_LEARNER_BODY,
            headers={"Idempotency-Key": "k-slow", **_auth()},
        )
        assert response.status_code == 202
        operation_id = UUID(response.json()["operation_id"])
        deadline = time.time() + 5
        while time.time() < deadline:
            if store.rows[operation_id]["status"] == "succeeded":
                break
            time.sleep(0.1)
    assert store.rows[operation_id]["status"] == "succeeded"


def test_graph_state_put_fast_path_200(tmp_path: Any, monkeypatch: Any) -> None:
    from backend.memory.api import graph_states as graph_states_module

    class _FakeRegistry:
        def __init__(self, session: Any) -> None:
            pass

        async def node_exists(self, node_id: str) -> bool:
            return node_id == "n001"

    monkeypatch.setattr(graph_states_module, "KnowledgeGraphRegistry", _FakeRegistry)
    app, _store, runner, _ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.put(
        "/api/v1/knowledge-graph/me/nodes/n001/state",
        json={"action": "mark_familiar", "expected_version": 3},
        headers={"Idempotency-Key": "k-gs", **_auth()},
    )
    assert response.status_code == 200
    assert response.json()["operation_type"] == "set_graph_state"
    operation = runner.calls[0]
    assert operation.priority == 80  # P1（§5.2）
    assert operation.payload.kind == "set_graph_state"
    assert operation.payload.node_id == "n001"  # type: ignore[union-attr]


def test_graph_node_not_found_returns_404(tmp_path: Any, monkeypatch: Any) -> None:
    from backend.memory.api import graph_states as graph_states_module

    class _FakeRegistry:
        def __init__(self, session: Any) -> None:
            pass

        async def node_exists(self, node_id: str) -> bool:
            return False

    monkeypatch.setattr(graph_states_module, "KnowledgeGraphRegistry", _FakeRegistry)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.put(
        "/api/v1/knowledge-graph/me/nodes/n999/state",
        json={"action": "mark_familiar"},
        headers={"Idempotency-Key": "k-404", **_auth()},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "GRAPH_NODE_NOT_FOUND"


# ---------------------------------------------------------------------------
# 取消规则（§11.6）
# ---------------------------------------------------------------------------


def _create_queued_operation(client: TestClient, key: str = "k-cancel") -> UUID:
    response = _post_event(client, key)
    assert response.status_code == 202
    return UUID(response.json()["operation_id"])


def test_cancel_queued_operation(tmp_path: Any, monkeypatch: Any) -> None:
    app, _store, _, _ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    operation_id = _create_queued_operation(client)
    response = client.post(
        f"/api/v1/memory/operations/{operation_id}/cancel",
        headers={"Idempotency-Key": "k-cancel-1", **_auth()},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["cancelled_at"] is not None


def test_cancel_terminal_operation_returns_409(tmp_path: Any, monkeypatch: Any) -> None:
    app, _store, _, _ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    operation_id = _create_queued_operation(client)
    headers = {"Idempotency-Key": "k-cancel-2", **_auth()}
    assert (
        client.post(f"/api/v1/memory/operations/{operation_id}/cancel", headers=headers).status_code
        == 200
    )
    again = client.post(
        f"/api/v1/memory/operations/{operation_id}/cancel",
        headers={**headers, "Idempotency-Key": "k-cancel-3"},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "OPERATION_CANCEL_NOT_ALLOWED"


def test_cancel_running_operation_is_cooperative(tmp_path: Any, monkeypatch: Any) -> None:
    app, store, _, _ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    operation_id = _create_queued_operation(client)
    store.rows[operation_id]["status"] = "running"
    response = client.post(
        f"/api/v1/memory/operations/{operation_id}/cancel",
        headers={"Idempotency-Key": "k-cancel-4", **_auth()},
    )
    # 协作取消已受理但 operation 未到终态 → 202（§7.2 状态语义）
    assert response.status_code == 202
    assert store.rows[operation_id]["cancel_requested_at"] is not None


def test_cancel_running_in_commit_returns_409(tmp_path: Any, monkeypatch: Any) -> None:
    """§11.6（裁决 2026-08-11）：已进入 commit 副作用的 running operation 不可取消。"""
    app, store, _, _ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    operation_id = _create_queued_operation(client)
    store.rows[operation_id]["status"] = "running"
    store.rows[operation_id]["commit_started_at"] = datetime.now(UTC)
    response = client.post(
        f"/api/v1/memory/operations/{operation_id}/cancel",
        headers={"Idempotency-Key": "k-cancel-5", **_auth()},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OPERATION_CANCEL_NOT_ALLOWED"
    # 未退化为协作取消
    assert store.rows[operation_id]["cancel_requested_at"] is None
    assert store.rows[operation_id]["status"] == "running"


def test_cancel_needs_review_operation(tmp_path: Any, monkeypatch: Any) -> None:
    """§11.6：needs_review 允许取消。"""
    app, store, _, _ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    operation_id = _create_queued_operation(client)
    store.rows[operation_id]["status"] = "needs_review"
    response = client.post(
        f"/api/v1/memory/operations/{operation_id}/cancel",
        headers={"Idempotency-Key": "k-cancel-6", **_auth()},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["cancelled_at"] is not None


# ---------------------------------------------------------------------------
# 跨用户 IDOR（§18.4）
# ---------------------------------------------------------------------------


def test_cannot_read_other_users_operation(tmp_path: Any, monkeypatch: Any) -> None:
    app, _store, _, _ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    operation_id = _create_queued_operation(client, "k-idor")
    response = client.get(f"/api/v1/memory/operations/{operation_id}", headers=_auth(OTHER_USER_ID))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "OPERATION_NOT_FOUND"


def test_cannot_cancel_other_users_operation(tmp_path: Any, monkeypatch: Any) -> None:
    app, store, _, _ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    operation_id = _create_queued_operation(client, "k-idor-2")
    response = client.post(
        f"/api/v1/memory/operations/{operation_id}/cancel",
        headers={"Idempotency-Key": "k-idor-3", **_auth(OTHER_USER_ID)},
    )
    assert response.status_code == 404
    assert store.rows[operation_id]["status"] == "queued"


def test_cannot_decide_other_users_candidate(tmp_path: Any, monkeypatch: Any) -> None:
    from backend.memory.persistence import review_candidates as candidates_repo

    async def _fake_get_candidate(session: Any, *, candidate_id: UUID) -> dict[str, Any]:
        return {"candidate_id": candidate_id, "user_id": OTHER_USER_ID, "status": "pending"}

    monkeypatch.setattr(candidates_repo, "get_candidate", _fake_get_candidate)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.post(
        f"/api/v1/memory/review-candidates/{uuid4()}/decision",
        json={"decision": "reject"},
        headers={"Idempotency-Key": "k-idor-4", **_auth()},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CANDIDATE_NOT_FOUND"


def test_decide_already_reviewed_candidate_returns_409(tmp_path: Any, monkeypatch: Any) -> None:
    from backend.memory.persistence import review_candidates as candidates_repo

    async def _fake_get_candidate(session: Any, *, candidate_id: UUID) -> dict[str, Any]:
        return {"candidate_id": candidate_id, "user_id": USER_ID, "status": "accepted"}

    monkeypatch.setattr(candidates_repo, "get_candidate", _fake_get_candidate)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.post(
        f"/api/v1/memory/review-candidates/{uuid4()}/decision",
        json={"decision": "reject"},
        headers={"Idempotency-Key": "k-idor-5", **_auth()},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANDIDATE_ALREADY_REVIEWED"


def test_decision_extra_candidate_id_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    """body 注入 candidate_id/kind 一律 422（§6.4 同模式）。"""
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.post(
        f"/api/v1/memory/review-candidates/{uuid4()}/decision",
        json={"decision": "reject", "candidate_id": str(uuid4())},
        headers={"Idempotency-Key": "k-idor-6", **_auth()},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_EXTRA_FIELD"
