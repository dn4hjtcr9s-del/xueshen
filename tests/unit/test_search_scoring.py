"""检索排序与中文规范化单元测试（§12.1 / §12.2 / §23.1）。

覆盖：NFKC 规范化、§12.2 各项加分、similarity 0.20 阈值、
摘录窗口、/memory/search 端点的 cursor 绑定与限流（§18.5 / §19.9）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.memory.persistence import index_entries as index_repo
from backend.memory.services.search_service import (
    SCORE_EXACT_TITLE,
    SCORE_EXACT_TOPIC_KEY,
    SCORE_PREFIX_TITLE,
    SCORE_RECENCY_MAX,
    SCORE_SIMILARITY_WEIGHT,
    SCORE_TOPIC_FILTER,
    SIMILARITY_THRESHOLD,
    is_candidate,
    matched_excerpt,
    normalize_search_query,
    recency_score,
    score_candidate,
)
from backend.settings import Settings
from tests.unit.api_fakes import build_test_app

USER_ID = uuid4()
NOW = datetime(2026, 8, 11, tzinfo=UTC)


def _settings(tmp_path: Any, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "development",
        "dev_auth_enabled": True,
        "memory_storage_root": str(tmp_path / "storage"),
    }
    base.update(overrides)
    return Settings(**base)


def _auth(user_id: Any = USER_ID) -> dict[str, str]:
    return {"X-Dev-User-Id": str(user_id)}


def _entry(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "memory_id": "mastery:topic-a",
        "memory_type": "mastery",
        "topic_key": "topic-a",
        "title": "一次函数",
        "summary": "掌握一次函数的图象与性质",
        "keywords": [],
        "evidence_refs": ["conv:t1:m1"],
        "confidence": 0.9,
        "source_version": 3,
        "updated_at": NOW,
        "similarity": 0.5,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# NFKC 规范化（§12.1 中文策略）
# ---------------------------------------------------------------------------


def test_normalize_nfkc_full_width_and_whitespace() -> None:
    assert normalize_search_query("　一次函数　") == "一次函数"
    assert normalize_search_query("ｆ（ｘ）＝２ｘ") == "f(x)=2x"
    assert normalize_search_query("平方  差\n公式") == "平方 差 公式"
    # 中文原文保留，不套用英文分词
    assert normalize_search_query("三角函数") == "三角函数"


# ---------------------------------------------------------------------------
# §12.2 排序公式各项加分
# ---------------------------------------------------------------------------


def _score(**kwargs: Any) -> float:
    params: dict[str, Any] = {
        "topic_key": None,
        "title": "不相关标题",
        "similarity": 0.0,
        "query": "查询词",
        "topic_filter": False,
        "updated_at": NOW - timedelta(days=90),
        "now": NOW,
    }
    params.update(kwargs)
    return score_candidate(**params)


def test_score_exact_topic_key() -> None:
    assert _score(topic_key="查询词") == SCORE_EXACT_TOPIC_KEY


def test_score_exact_normalized_title() -> None:
    # 规范化后相等即可得精确标题分（全半角差异不阻断）
    assert _score(title="查询词") == SCORE_EXACT_TITLE
    assert _score(title="ｆ（ｘ）", query="f(x)") == SCORE_EXACT_TITLE


def test_score_prefix_title() -> None:
    assert _score(title="查询词 进阶", query="查询词") == SCORE_PREFIX_TITLE


def test_score_exact_title_does_not_double_count_prefix() -> None:
    assert _score(title="查询词", query="查询词") == SCORE_EXACT_TITLE


def test_score_similarity_weight() -> None:
    assert _score(similarity=0.5) == pytest.approx(0.5 * SCORE_SIMILARITY_WEIGHT)


def test_score_topic_filter() -> None:
    assert _score(topic_filter=True) == SCORE_TOPIC_FILTER


def test_score_recency_linear_decay() -> None:
    assert recency_score(NOW, NOW) == pytest.approx(SCORE_RECENCY_MAX)
    assert recency_score(NOW - timedelta(days=15), NOW) == pytest.approx(SCORE_RECENCY_MAX / 2)
    assert recency_score(NOW - timedelta(days=30), NOW) == 0.0
    assert recency_score(NOW - timedelta(days=365), NOW) == 0.0


def test_score_combines_all_terms() -> None:
    score = score_candidate(
        topic_key="函数",
        title="函数",
        similarity=0.5,
        query="函数",
        topic_filter=True,
        updated_at=NOW,
        now=NOW,
    )
    expected = (
        SCORE_EXACT_TOPIC_KEY
        + SCORE_EXACT_TITLE
        + 0.5 * SCORE_SIMILARITY_WEIGHT
        + SCORE_TOPIC_FILTER
        + SCORE_RECENCY_MAX
    )
    assert score == pytest.approx(expected)


# ---------------------------------------------------------------------------
# similarity 阈值（§12.2：>= 0.20 才进入候选）
# ---------------------------------------------------------------------------


def test_candidate_threshold() -> None:
    assert not is_candidate(topic_key=None, title="无关", similarity=0.19, query="查询词")
    assert is_candidate(topic_key=None, title="无关", similarity=SIMILARITY_THRESHOLD, query="q")
    # 精确/前缀命中不受阈值阻断（§12.1 精确匹配优先）
    assert is_candidate(topic_key="q", title="无关", similarity=0.0, query="q")
    assert is_candidate(topic_key=None, title="q 进阶", similarity=0.0, query="q")


def test_matched_excerpt_window() -> None:
    summary = "甲" * 60 + "目标词" + "乙" * 60
    excerpt = matched_excerpt(summary, "目标词", window=10)
    assert excerpt is not None
    assert excerpt.startswith("…") and excerpt.endswith("…")
    assert "目标词" in excerpt
    assert matched_excerpt("完全不相关", "目标词") is None


# ---------------------------------------------------------------------------
# POST /memory/search 端点（§19.4 / §19.9 / §18.5）
# ---------------------------------------------------------------------------


def _install_candidates(monkeypatch: Any, rows: list[dict[str, Any]]) -> None:
    async def fake_candidates(session: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return rows

    monkeypatch.setattr(index_repo, "search_candidates", fake_candidates)


def test_search_endpoint_returns_hits_sorted(tmp_path: Any, monkeypatch: Any) -> None:
    rows = [
        _entry(memory_id="mastery:low", topic_key="low", title="低分", similarity=0.21),
        _entry(memory_id="mastery:high", topic_key="high", title="高相似", similarity=0.9),
    ]
    _install_candidates(monkeypatch, rows)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.post("/api/v1/memory/search", json={"query": "函数"}, headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert [item["memory_id"] for item in body["items"]] == [
        "mastery:high",
        "mastery:low",
    ]
    assert body["items"][0]["version"] == 3
    assert body["items"][0]["confidence"] == pytest.approx(0.9)
    assert body["has_more"] is False
    assert body["next_cursor"] is None


def test_search_endpoint_excludes_below_threshold(tmp_path: Any, monkeypatch: Any) -> None:
    # 仓储层可能漏回低相似行，服务层仍按 §12.2 阈值拦截
    rows = [_entry(title="无关标题", topic_key="other", similarity=0.1)]
    _install_candidates(monkeypatch, rows)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.post("/api/v1/memory/search", json={"query": "函数"}, headers=_auth())
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_search_cursor_binds_filters(tmp_path: Any, monkeypatch: Any) -> None:
    rows = [
        _entry(memory_id=f"mastery:t{i}", topic_key=f"t{i}", title=f"标题{i}", similarity=0.9)
        for i in range(3)
    ]
    _install_candidates(monkeypatch, rows)
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    first = client.post(
        "/api/v1/memory/search", json={"query": "标题", "limit": 2}, headers=_auth()
    )
    assert first.status_code == 200
    body = first.json()
    assert body["has_more"] is True
    assert len(body["items"]) == 2
    # 同一筛选翻页成功
    second = client.post(
        "/api/v1/memory/search",
        json={"query": "标题", "limit": 2, "cursor": body["next_cursor"]},
        headers=_auth(),
    )
    assert second.status_code == 200
    page2 = second.json()
    assert len(page2["items"]) == 1
    assert page2["has_more"] is False
    # 筛选条件变化 → CURSOR_INVALID
    changed = client.post(
        "/api/v1/memory/search",
        json={"query": "别的", "limit": 2, "cursor": body["next_cursor"]},
        headers=_auth(),
    )
    assert changed.status_code == 422
    assert changed.json()["error"]["code"] == "CURSOR_INVALID"


def test_search_rate_limit_60_per_minute(tmp_path: Any, monkeypatch: Any) -> None:
    _install_candidates(monkeypatch, [])
    app, *_ = build_test_app(
        _settings(tmp_path, rate_limit_search_per_minute=2), monkeypatch=monkeypatch
    )
    client = TestClient(app)
    for _ in range(2):
        assert (
            client.post("/api/v1/memory/search", json={"query": "q"}, headers=_auth()).status_code
            == 200
        )
    third = client.post("/api/v1/memory/search", json={"query": "q"}, headers=_auth())
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "RATE_LIMITED"


def test_search_requires_read_scope(tmp_path: Any, monkeypatch: Any) -> None:
    _install_candidates(monkeypatch, [])
    app, *_ = build_test_app(
        _settings(tmp_path, dev_auth_allow_scope_override=True), monkeypatch=monkeypatch
    )
    client = TestClient(app)
    headers = {
        "X-Dev-User-Id": str(USER_ID),
        "X-Dev-Scopes": "memory:context",
    }
    response = client.post("/api/v1/memory/search", json={"query": "q"}, headers=headers)
    assert response.status_code == 403


def test_search_rejects_extra_field(tmp_path: Any, monkeypatch: Any) -> None:
    _install_candidates(monkeypatch, [])
    app, *_ = build_test_app(_settings(tmp_path), monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.post(
        "/api/v1/memory/search",
        json={"query": "q", "user_id": str(USER_ID)},
        headers=_auth(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_EXTRA_FIELD"
