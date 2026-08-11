"""读接口与 cursor 单元测试（§19.4–§19.6 / §19.9 / §8.6.1 / §23.5）。

覆盖：learner/mastery/index/memories 视图、index 未构建语义、deleted 分页、
通知列表与幂等已读、图谱快照/Overlay/解释、cursor 篡改/过期/跨路由/跨用户。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.memory.api import graph_states as graph_states_module
from backend.memory.api.dependencies import issue_cursor, resolve_cursor
from backend.memory.contracts.errors import CursorExpiredError, CursorInvalidError
from backend.memory.persistence import documents as docs_repo
from backend.memory.persistence import graph_states as graph_repo
from backend.memory.persistence import notifications as notifications_repo
from backend.memory.persistence import review_candidates as candidates_repo
from backend.memory.storage.markdown_schema import IndexDocument, IndexEntry, LearnerDocument
from backend.settings import Settings
from tests.unit.api_fakes import build_test_app

USER_ID = uuid4()
OTHER_USER_ID = uuid4()


def _settings(tmp_path: Any, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "development",
        "dev_auth_enabled": True,
        "memory_storage_root": str(tmp_path / "storage"),
    }
    base.update(overrides)
    return Settings(**base)


def _auth(user_id: UUID = USER_ID) -> dict[str, str]:
    return {"X-Dev-User-Id": str(user_id)}


def _learner_doc() -> LearnerDocument:
    return LearnerDocument(
        user_id=USER_ID,
        version=3,
        updated_at=datetime(2026, 8, 10, tzinfo=UTC),
        preferences=["喜欢例题"],
        goals=["期中 90 分"],
        plans=["每天 30 分钟"],
        evidence_refs=["conv:t1:m1"],
        confidence=0.9,
    )


# ---------------------------------------------------------------------------
# 总结记忆读（§19.4）
# ---------------------------------------------------------------------------


def test_get_learner_404_when_absent(tmp_path: Any, monkeypatch: Any) -> None:
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.get("/api/v1/memory/learner", headers=_auth())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MEMORY_NOT_FOUND"


def test_get_learner_returns_view(tmp_path: Any, monkeypatch: Any) -> None:
    app, _, _, service = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    service.learner = _learner_doc()
    client = TestClient(app)
    response = client.get("/api/v1/memory/learner", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["memory_type"] == "learner"
    assert body["version"] == 3
    assert body["preferences"] == ["喜欢例题"]


def test_get_memory_by_id_learner(tmp_path: Any, monkeypatch: Any) -> None:
    app, _, _, service = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    service.learner = _learner_doc()
    client = TestClient(app)
    response = client.get("/api/v1/memory/memories/learner", headers=_auth())
    assert response.status_code == 200
    assert response.json()["memory_id"] == "learner"


def test_get_memory_by_id_rejects_unknown_id(tmp_path: Any, monkeypatch: Any) -> None:
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.get("/api/v1/memory/memories/index", headers=_auth())
    assert response.status_code == 404


def test_mastery_topic_key_path_traversal_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.get("/api/v1/memory/mastery/a..b", headers=_auth())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PAYLOAD"


def test_index_not_built_returns_version_0_stale(tmp_path: Any, monkeypatch: Any) -> None:
    """§8.6.1：未构建 index 返回 version=0/stale=true，不 404。"""
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.get("/api/v1/memory/index", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 0
    assert body["entries"] == []
    assert body["updated_at"] is None
    assert body["stale"] is True


def test_index_built_returns_entries(tmp_path: Any, monkeypatch: Any) -> None:
    app, _, _, service = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    now = datetime(2026, 8, 10, tzinfo=UTC)
    service.index = (
        IndexDocument(
            user_id=USER_ID,
            version=2,
            updated_at=now,
            learner=IndexEntry(
                memory_id="learner",
                memory_type="learner",
                topic_key=None,
                title="学习者档案",
                version=3,
                updated_at=now,
            ),
            mastery_entries=[
                IndexEntry(
                    memory_id="mastery:ji-xian",
                    memory_type="mastery",
                    topic_key="ji-xian",
                    title="极限",
                    version=1,
                    updated_at=now,
                )
            ],
        ),
        False,
    )
    client = TestClient(app)
    response = client.get("/api/v1/memory/index", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 2
    assert body["stale"] is False
    assert [e["memory_id"] for e in body["entries"]] == ["learner", "mastery:ji-xian"]


# ---------------------------------------------------------------------------
# deleted / review-candidates 分页（§19.4 / §19.9）
# ---------------------------------------------------------------------------


def _deleted_row(memory_id: str, deleted_at: datetime) -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "memory_type": "mastery",
        "topic_key": "ji-xian",
        "topic_title": "极限",
        "deleted_version": 2,
        "deleted_at": deleted_at,
        "tombstone_until": datetime(2026, 9, 10, tzinfo=UTC),
    }


def test_deleted_page_and_cursor_roundtrip(tmp_path: Any, monkeypatch: Any) -> None:
    rows = [
        _deleted_row(f"mastery:t{i}", datetime(2026, 8, 10, 8, i, tzinfo=UTC)) for i in range(3)
    ]

    async def _fake_page(
        session: Any,
        *,
        user_id: UUID,
        now: datetime,
        limit: int,
        cursor_deleted_at: datetime | None,
        cursor_memory_id: str | None,
    ) -> list[dict[str, Any]]:
        assert user_id == USER_ID
        if cursor_deleted_at is None:
            return rows[:limit]
        return [
            r
            for r in rows
            if (r["deleted_at"], r["memory_id"]) < (cursor_deleted_at, cursor_memory_id)
        ][:limit]

    monkeypatch.setattr(docs_repo, "list_deleted_page", _fake_page)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    first = client.get("/api/v1/memory/deleted?limit=2", headers=_auth())
    assert first.status_code == 200
    page1 = first.json()
    assert len(page1["items"]) == 2
    assert page1["has_more"] is True
    assert page1["next_cursor"]
    second = client.get(
        f"/api/v1/memory/deleted?limit=2&cursor={page1['next_cursor']}", headers=_auth()
    )
    page2 = second.json()
    assert len(page2["items"]) == 1
    assert page2["has_more"] is False
    assert page2["next_cursor"] is None
    assert page2["items"][0]["restore_until"]


def test_deleted_cursor_tampered_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    async def _fake_page(session: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(docs_repo, "list_deleted_page", _fake_page)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.get("/api/v1/memory/deleted?cursor=abc.def", headers=_auth())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CURSOR_INVALID"


def test_cursor_binding_rules(tmp_path: Any) -> None:
    """§19.9：路由/主体/筛选/过期绑定（helper 级）。"""
    settings = _settings(tmp_path)
    cursor = issue_cursor(
        settings,
        route="memory.deleted",
        user_id=USER_ID,
        filters={"limit": 20},
        sort_key=["2026-08-10T08:00:00+00:00", "mastery:x"],
    )
    payload = resolve_cursor(
        settings, cursor, route="memory.deleted", user_id=USER_ID, filters={"limit": 20}
    )
    assert payload["cursor_version"] == 1
    # 跨路由
    with pytest.raises(CursorInvalidError):
        resolve_cursor(
            settings,
            cursor,
            route="memory.notifications",
            user_id=USER_ID,
            filters={"limit": 20},
        )
    # 跨用户
    with pytest.raises(CursorInvalidError):
        resolve_cursor(
            settings,
            cursor,
            route="memory.deleted",
            user_id=OTHER_USER_ID,
            filters={"limit": 20},
        )
    # 筛选不一致
    with pytest.raises(CursorInvalidError):
        resolve_cursor(
            settings, cursor, route="memory.deleted", user_id=USER_ID, filters={"limit": 50}
        )


def test_cursor_expired(tmp_path: Any) -> None:
    settings = _settings(tmp_path, cursor_ttl_seconds=1)
    cursor = issue_cursor(
        settings,
        route="memory.deleted",
        user_id=USER_ID,
        filters={"limit": 20},
        sort_key=["2026-08-10T08:00:00+00:00", "mastery:x"],
    )
    time.sleep(1.1)
    with pytest.raises(CursorExpiredError):
        resolve_cursor(
            settings, cursor, route="memory.deleted", user_id=USER_ID, filters={"limit": 20}
        )


# ---------------------------------------------------------------------------
# 候选列表（§19.4）
# ---------------------------------------------------------------------------


def _candidate_row(candidate_id: UUID) -> dict[str, Any]:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    return {
        "candidate_id": candidate_id,
        "user_id": USER_ID,
        "candidate_type": "mastery",
        "base_memory_id": None,
        "base_version": None,
        "topic_key": "ji-xian",
        "candidate_payload": {
            "memory_type": "mastery",
            "topic_key": "ji-xian",
            "topic_title": "极限",
            "overview": "o",
        },
        "evidence_refs": ["conv:t1:m1"],
        "confidence": 0.7,
        "status": "pending",
        "resolution_target": None,
        "target_memory_id": None,
        "resolved_operation_id": None,
        "reviewed_at": None,
        "created_at": now,
        "updated_at": now,
    }


def test_review_candidates_list(tmp_path: Any, monkeypatch: Any) -> None:
    row = _candidate_row(uuid4())

    async def _fake_list(
        session: Any,
        *,
        user_id: UUID,
        status: str | None,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_candidate_id: UUID | None,
    ) -> list[dict[str, Any]]:
        assert status == "pending"
        return [row]

    monkeypatch.setattr(candidates_repo, "list_candidates_page", _fake_list)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.get("/api/v1/memory/review-candidates?status=pending", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["has_more"] is False
    item = body["items"][0]
    assert item["candidate_type"] == "mastery"
    assert item["candidate_content"]["topic_title"] == "极限"
    assert item["status"] == "pending"


# ---------------------------------------------------------------------------
# 通知（§19.6）
# ---------------------------------------------------------------------------


def _notification_row(notification_id: UUID, read_at: datetime | None) -> dict[str, Any]:
    return {
        "notification_id": notification_id,
        "user_id": USER_ID,
        "event_type": "review_candidate.created",
        "title": "新候选",
        "body": "有一条记忆候选待审核",
        "aggregate_type": "review_candidate",
        "aggregate_id": str(uuid4()),
        "read_at": read_at,
        "created_at": datetime(2026, 8, 10, tzinfo=UTC),
    }


def test_notifications_list_with_unread_count(tmp_path: Any, monkeypatch: Any) -> None:
    row = _notification_row(uuid4(), None)

    async def _fake_list(
        session: Any,
        *,
        user_id: UUID,
        limit: int,
        cursor_created_at: Any,
        cursor_id: Any,
        unread_only: bool,
    ) -> list[dict[str, Any]]:
        assert unread_only is True
        return [row]

    async def _fake_unread(session: Any, *, user_id: UUID) -> int:
        return 5

    monkeypatch.setattr(notifications_repo, "list_notifications", _fake_list)
    monkeypatch.setattr(notifications_repo, "unread_count", _fake_unread)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.get("/api/v1/memory/notifications?unread_only=true", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["unread_count"] == 5
    assert body["items"][0]["event_type"] == "review_candidate.created"


def test_notification_mark_read_idempotent(tmp_path: Any, monkeypatch: Any) -> None:
    notification_id = uuid4()
    read_at = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)

    async def _fake_mark_read(
        session: Any, *, user_id: UUID, notification_id: UUID
    ) -> dict[str, Any]:
        return _notification_row(notification_id, read_at)

    monkeypatch.setattr(notifications_repo, "mark_read", _fake_mark_read)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    for key in ("k-read-1", "k-read-2"):
        response = client.post(
            f"/api/v1/memory/notifications/{notification_id}/read",
            headers={"Idempotency-Key": key, **_auth()},
        )
        assert response.status_code == 200
        # 两次调用返回同一 read_at（幂等，§19.6）
        assert response.json()["read_at"] == "2026-08-10T09:00:00Z"


def test_notification_read_other_user_returns_404(tmp_path: Any, monkeypatch: Any) -> None:
    async def _fake_mark_read(
        session: Any, *, user_id: UUID, notification_id: UUID
    ) -> dict[str, Any] | None:
        return None  # 仓储按 user_id 过滤；他人通知不可见

    monkeypatch.setattr(notifications_repo, "mark_read", _fake_mark_read)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.post(
        f"/api/v1/memory/notifications/{uuid4()}/read",
        headers={"Idempotency-Key": "k-read-3", **_auth(OTHER_USER_ID)},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOTIFICATION_NOT_FOUND"


# ---------------------------------------------------------------------------
# 图谱读（§19.5）
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self, session: Any) -> None:
        pass

    async def list_nodes(self) -> list[dict[str, Any]]:
        return [
            {"node_id": "n001", "title": "极限", "group_key": "g1", "metadata": {"stage": "高中"}},
            {"node_id": "n002", "title": "导数", "group_key": "g1", "metadata": {}},
        ]

    async def list_edges(self) -> list[dict[str, Any]]:
        return [{"from_node_id": "n001", "to_node_id": "n002", "relation_type": "prerequisite"}]

    async def latest_applied_sync(self) -> dict[str, Any]:
        return {"manifest_checksum": "a" * 64, "applied_at": datetime(2026, 8, 10, tzinfo=UTC)}

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        nodes = {n["node_id"]: n for n in await self.list_nodes()}
        return nodes.get(node_id)

    async def node_exists(self, node_id: str) -> bool:
        return node_id in {"n001", "n002"}

    async def edges_to(self, node_id: str) -> list[str]:
        return ["n001"] if node_id == "n002" else []

    async def edges_from(self, node_id: str) -> list[str]:
        return ["n002"] if node_id == "n001" else []


def test_knowledge_graph_snapshot(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(graph_states_module, "KnowledgeGraphRegistry", _FakeRegistry)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.get("/api/v1/knowledge-graph/nodes", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert len(body["nodes"]) == 2
    assert body["edges"][0]["relation_type"] == "prerequisite"
    assert body["manifest_checksum"] == "a" * 64


def test_my_node_detail_with_overlay(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(graph_states_module, "KnowledgeGraphRegistry", _FakeRegistry)

    async def _fake_overlay(
        session: Any, *, user_id: UUID, node_id: str, for_update: bool = False
    ) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "status": "learning",
            "version": 2,
            "status_source": "user",
            "updated_at": datetime(2026, 8, 10, tzinfo=UTC),
        }

    monkeypatch.setattr(graph_repo, "get_overlay", _fake_overlay)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.get("/api/v1/knowledge-graph/me/nodes/n001", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["node"]["title"] == "极限"
    assert body["overlay"]["status"] == "learning"
    assert body["successor_node_ids"] == ["n002"]


def test_my_node_detail_unknown_node_404(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(graph_states_module, "KnowledgeGraphRegistry", _FakeRegistry)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.get("/api/v1/knowledge-graph/me/nodes/n999", headers=_auth())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "GRAPH_NODE_NOT_FOUND"


def test_node_explanation_from_audit(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(graph_states_module, "KnowledgeGraphRegistry", _FakeRegistry)

    async def _fake_overlay(
        session: Any, *, user_id: UUID, node_id: str, for_update: bool = False
    ) -> dict[str, Any] | None:
        return None

    async def _fake_audit(session: Any, *, user_id: UUID, node_id: str) -> dict[str, Any]:
        return {
            "actor_type": "summary_projection",
            "reason_codes": ["STRONG_POSITIVE_EVIDENCE"],
            "evidence_refs": [f"conv:t1:m{i}" for i in range(15)],
            "explanation_summary": "近 180 天有 3 条强正向证据",
            "created_at": datetime(2026, 8, 10, tzinfo=UTC),
        }

    monkeypatch.setattr(graph_repo, "get_overlay", _fake_overlay)
    monkeypatch.setattr(graph_repo, "latest_audit", _fake_audit)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.get("/api/v1/knowledge-graph/me/nodes/n001/explanation", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["explanation_available"] is True
    assert body["source_type"] == "summary_memory"
    assert len(body["evidence_refs"]) == 10  # 最多 10 个（§19.5）
    assert body["current_status"] is None
