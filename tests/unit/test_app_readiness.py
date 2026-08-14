"""D25 readiness 语义测试：Community DB 未配置不挂载不报错；已配置但不可达 fail-closed。

与 tests/unit/api_fakes.py 的 build_test_app 同模式（fake runtime），
避免真实 LLM client 与数据库依赖。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.settings import Settings
from tests.unit.api_fakes import build_test_app


def _client(settings: Settings, *, monkeypatch):
    app, _store, _runner, _service = build_test_app(settings, monkeypatch=monkeypatch)
    return TestClient(app)


def test_readiness_ok_without_community_config(monkeypatch) -> None:
    """D25：未配置 COMMUNITY_DATABASE_URL → readiness 无任何 community 失败项。"""
    settings = Settings(_env_file=None, APP_ENV="test")
    client = _client(settings, monkeypatch=monkeypatch)
    with client:
        r = client.get("/health/ready")
        body = r.json()
        failures = body.get("failures", [])
        # fake runtime 下 memory/conversation 等域会失败（既有行为），
        # 但 Community 未配置时不得出现任何 community_* 失败项
        assert not any(f.startswith("community") for f in failures), failures


def test_readiness_fail_closed_when_community_db_unreachable(monkeypatch) -> None:
    """D25：已配置但社区库不可达 → 503 community_database_unavailable。"""
    settings = Settings(
        _env_file=None,
        APP_ENV="test",
        COMMUNITY_DATABASE_URL=(
            "postgresql+psycopg://community:community@127.0.0.1:59999/community_test"
        ),
    )
    app, _store, _runner, _service = build_test_app(settings, monkeypatch=monkeypatch)
    client = TestClient(app)
    with client:
        r = client.get("/health/ready")
        body = r.json()
        assert r.status_code == 503, body
        assert "community_database_unavailable" in body.get("failures", [])
