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
    settings = Settings(app_env="test", memory_storage_root="/tmp/memory-contract-test")
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
        "/api/v1/memory/review-candidates",
        "/api/v1/memory/notifications",
        "/api/v1/memory/notifications/{notification_id}/read",
        "/api/v1/knowledge-graph/nodes",
        "/api/v1/knowledge-graph/me/nodes",
        "/api/v1/knowledge-graph/me/nodes/{node_id}",
        "/api/v1/knowledge-graph/me/nodes/{node_id}/state",
        "/api/v1/knowledge-graph/me/nodes/{node_id}/explanation",
        "/api/v1/internal/account-memory/purge",
        "/health/live",
        "/health/ready",
        "/health/startup",
        "/metrics",
    }
    assert expected_paths <= set(spec["paths"])
    # 第 12 步范围界限：检索/推荐/上下文路由本步骤不存在
    assert "/api/v1/memory/search" not in spec["paths"]
    assert "/api/v1/knowledge-graph/recommendations" not in spec["paths"]
