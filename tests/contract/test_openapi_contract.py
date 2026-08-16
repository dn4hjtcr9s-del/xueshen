"""OpenAPI snapshot/contract test（§23.5 / §23.7 contracts 阶段）。

快照文件为 tests/contract/openapi_snapshot.json；路由或 schema 变更后运行：
    UPDATE_OPENAPI_SNAPSHOT=1 .venv/bin/python -m pytest tests/contract -q
重新生成快照，并在 code review 中确认 diff 符合规格 §19。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from backend.app import create_app
from backend.settings import Settings

SNAPSHOT_PATH = Path(__file__).with_name("openapi_snapshot.json")


def _normalized_spec() -> str:
    # Study（方案 §20.4）：显式启用 Study 域使 Study 路由进入契约快照
    # （dummy URL 只在装配期创建引擎对象，openapi() 不建立连接）。
    settings = Settings(
        app_env="test",
        memory_storage_root="/tmp/memory-contract-test",
        study_domain_enabled=True,
        study_database_url="postgresql+psycopg://study:study@127.0.0.1:55432/study",
        study_account_purge_service_token="contract-test-purge-token",
    )
    app = create_app(settings)
    spec = app.openapi()
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def test_openapi_snapshot() -> None:
    current = _normalized_spec()
    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT") == "1":
        SNAPSHOT_PATH.write_text(current, encoding="utf-8")
        return
    assert SNAPSHOT_PATH.exists(), "快照不存在：先用 UPDATE_OPENAPI_SNAPSHOT=1 生成"
    expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert current == expected, (
        "OpenAPI 契约已变化；确认符合规格后用 UPDATE_OPENAPI_SNAPSHOT=1 更新快照"
    )


def test_openapi_contains_spec_routes() -> None:
    """§19 本步骤要求的路由必须在契约中（独立于快照的显式断言）。"""
    spec = json.loads(_normalized_spec())
    expected_paths = {
        "/api/v1/memory/events",
        "/api/v1/memory/commands/correct",
        "/api/v1/memory/commands/forget",
        "/api/v1/memory/commands/restore",
        "/api/v1/memory/learner",
        "/api/v1/memory/review-candidates/{candidate_id}/decision",
        "/api/v1/memory/operations/{operation_id}",
        "/api/v1/memory/operations/{operation_id}/cancel",
        "/api/v1/memory/index",
        "/api/v1/memory/mastery/{topic_key}",
        "/api/v1/memory/memories/{memory_id}",
        "/api/v1/memory/deleted",
        "/api/v1/memory/search",
        "/api/v1/memory/context",
        "/api/v1/memory/review-candidates",
        "/api/v1/memory/notifications",
        "/api/v1/memory/notifications/{notification_id}/read",
        "/api/v1/knowledge-graph/nodes",
        "/api/v1/knowledge-graph/me/nodes",
        "/api/v1/knowledge-graph/me/nodes/{node_id}",
        "/api/v1/knowledge-graph/me/nodes/{node_id}/state",
        "/api/v1/knowledge-graph/me/nodes/{node_id}/explanation",
        "/api/v1/knowledge-graph/recommendations",
        "/api/v1/internal/account-memory/purge",
        "/health/live",
        "/health/ready",
        "/health/startup",
        "/metrics",
    }
    assert expected_paths <= set(spec["paths"])


def test_openapi_contains_study_routes() -> None:
    """方案 §20.4：Study 域路由必须进入契约快照（v1.2 §12 冻结）。"""
    spec = json.loads(_normalized_spec())
    expected_study_paths = {
        "/api/v1/study/intakes",
        "/api/v1/study/intakes/{intake_id}",
        "/api/v1/study/intakes/{intake_id}/messages",
        "/api/v1/study/intakes/{intake_id}/confirm",
        "/api/v1/study/plans",
        "/api/v1/study/plans/{plan_id}",
        "/api/v1/study/plans/{plan_id}/calendar",
        "/api/v1/study/plans/{plan_id}/revisions",
        "/api/v1/study/plans/{plan_id}/revisions/{revision_id}/accept",
        "/api/v1/study/plans/{plan_id}/revisions/{revision_id}/reject",
        "/api/v1/study/plans/{plan_id}/activate",
        "/api/v1/study/plans/{plan_id}/pause",
        "/api/v1/study/plans/{plan_id}/resume",
        "/api/v1/study/plans/{plan_id}/archive",
        "/api/v1/study/tasks/{task_id}/start",
        "/api/v1/study/tasks/{task_id}/complete",
        "/api/v1/study/tasks/{task_id}/reopen",
        "/api/v1/study/tasks/{task_id}/skip",
        "/api/v1/study/tasks/{task_id}/reschedule",
        "/api/v1/study/tasks/{task_id}/launch",
        "/api/v1/study/sessions/{session_id}",
        "/api/v1/study/sessions/{session_id}/heartbeat",
        "/api/v1/study/sessions/{session_id}/finish",
        "/api/v1/study/home",
        "/api/v1/study/operations/{operation_id}",
        "/api/v1/internal/study-accounts/purge",
    }
    assert expected_study_paths <= set(spec["paths"])
