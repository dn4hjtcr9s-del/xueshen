"""Break-glass 安全测试（§13.15 / §23.5）：限时、限用户、完整审计。

仓储函数通过 FakeBreakGlassRepo monkeypatch，会话使用 FakeSessionFactory，
不起真实 PostgreSQL（SQL 正确性由 tests/integration/test_break_glass.py 覆盖）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.memory.break_glass import validate_grant_creation
from backend.memory.persistence import break_glass as bg_repo
from backend.memory.storage.markdown_schema import LearnerDocument
from backend.settings import Settings
from tests.unit.api_fakes import build_test_app

ADMIN_ID = uuid4()
OTHER_ADMIN_ID = uuid4()
TARGET_USER_ID = uuid4()


def _settings(tmp_path: Any, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "development",
        "dev_auth_enabled": True,
        "dev_auth_allow_scope_override": True,
        "memory_storage_root": str(tmp_path / "storage"),
    }
    # production 覆盖时补齐 §6.3 认证配置（评审 P1-4 要求密钥文件真实存在）
    if overrides.get("app_env") == "production":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_file = tmp_path / "auth_private.pem"
        private_file.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        public_pem = (
            key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        base["dev_auth_enabled"] = False
        base["dev_auth_allow_scope_override"] = False
        base["auth_private_key_file"] = str(private_file)
        base["auth_public_key"] = public_pem
        base["auth_database_url"] = "postgresql+psycopg://auth:auth@db:5432/auth"
    base.update(overrides)
    return Settings(**base)


def _admin_auth(
    admin_id: UUID = ADMIN_ID,
    grant_id: UUID | None = None,
    scopes: str = "memory:read memory:break_glass",
) -> dict[str, str]:
    headers = {
        "X-Dev-User-Id": str(admin_id),
        "X-Dev-Actor-Type": "admin",
        "X-Dev-Scopes": scopes,
    }
    if grant_id is not None:
        headers["X-Break-Glass-Grant-Id"] = str(grant_id)
    return headers


def _grant(
    *,
    admin: UUID = ADMIN_ID,
    target: UUID = TARGET_USER_ID,
    expires_delta: timedelta = timedelta(minutes=30),
    revoked: bool = False,
) -> dict[str, Any]:
    return {
        "grant_id": uuid4(),
        "admin_user_id": admin,
        "target_user_id": target,
        "reason": "用户申诉：误删恢复核查",
        "scopes": ["memory:read"],
        "approved_by": None,
        "expires_at": datetime.now(UTC) + expires_delta,
        "revoked_at": datetime.now(UTC) if revoked else None,
        "created_at": datetime.now(UTC),
    }


class FakeBreakGlassRepo:
    def __init__(self, grants: list[dict[str, Any]]) -> None:
        self.grants = {g["grant_id"]: g for g in grants}
        self.audits: list[dict[str, Any]] = []
        self.fail_actions: set[str] = set()

    async def get_grant(self, session: Any, grant_id: UUID) -> dict[str, Any] | None:
        return self.grants.get(grant_id)

    async def insert_audit(
        self,
        session: Any,
        *,
        audit_id: UUID,
        grant_id: UUID,
        admin_user_id: UUID,
        target_user_id: UUID,
        action: str,
        resource_type: str,
        resource_id: str | None,
        trace_id: str,
    ) -> None:
        if action in self.fail_actions:
            raise RuntimeError(f"injected: audit write failure ({action})")
        self.audits.append(
            {
                "grant_id": grant_id,
                "admin_user_id": admin_user_id,
                "target_user_id": target_user_id,
                "action": action,
                "resource_type": resource_type,
                "trace_id": trace_id,
            }
        )

    def install(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(bg_repo, "get_grant", self.get_grant)
        monkeypatch.setattr(bg_repo, "insert_audit", self.insert_audit)


def _learner_doc() -> LearnerDocument:
    return LearnerDocument(
        user_id=TARGET_USER_ID,
        version=3,
        updated_at=datetime(2026, 8, 10, tzinfo=UTC),
        preferences=["喜欢例题"],
        goals=["期中 90 分"],
        plans=["每天 30 分钟"],
        evidence_refs=["conv:t1:m1"],
        confidence=0.9,
    )


def _build(
    tmp_path: Any,
    monkeypatch: Any,
    grants: list[dict[str, Any]],
    **settings_overrides: Any,
) -> tuple[TestClient, FakeBreakGlassRepo, Any, list[UUID]]:
    repo = FakeBreakGlassRepo(grants)
    repo.install(monkeypatch)
    app, _, _, service = build_test_app(
        _settings(tmp_path, **settings_overrides), monkeypatch=monkeypatch
    )
    service.learner = _learner_doc()
    seen_users: list[UUID] = []
    original = service.get_learner

    async def _spy(*, user_id: UUID) -> Any:
        seen_users.append(user_id)
        return await original(user_id=user_id)

    monkeypatch.setattr(service, "get_learner", _spy)
    return TestClient(app), repo, service, seen_users


# ---------------------------------------------------------------------------
# 使用路径：限用户、限时、审计（§13.15 / §23.5）
# ---------------------------------------------------------------------------


def test_valid_grant_allows_admin_body_read(tmp_path: Any, monkeypatch: Any) -> None:
    """有效 grant：admin 以目标用户身份读正文，写 use + read_body 审计。"""
    grant = _grant()
    client, repo, _, seen = _build(tmp_path, monkeypatch, [grant])
    response = client.get("/api/v1/memory/learner", headers=_admin_auth(grant_id=grant["grant_id"]))
    assert response.status_code == 200
    assert response.json()["memory_type"] == "learner"
    # 限用户：实际读取的是 grant 绑定的目标用户，而不是 admin 自己
    assert seen == [TARGET_USER_ID]
    assert [a["action"] for a in repo.audits] == ["use", "read_body"]
    assert all(a["admin_user_id"] == ADMIN_ID for a in repo.audits)
    assert all(a["target_user_id"] == TARGET_USER_ID for a in repo.audits)


def test_body_audit_failure_aborts_response_fail_closed(tmp_path: Any, monkeypatch: Any) -> None:
    """评审 #10：正文审计写入失败时 fail-closed——不得返回 2xx 敏感正文。"""
    grant = _grant()
    client, repo, _, _ = _build(tmp_path, monkeypatch, [grant])
    repo.fail_actions.add("read_body")
    response = client.get("/api/v1/memory/learner", headers=_admin_auth(grant_id=grant["grant_id"]))
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "AUDIT_WRITE_FAILED"
    assert "memory_type" not in body, "审计失败时不得返回敏感正文"
    # 授权阶段的 use 审计已成功；失败的 read_body 未落库
    assert [a["action"] for a in repo.audits] == ["use"]


def test_expired_grant_rejected_with_expired_check_audit(tmp_path: Any, monkeypatch: Any) -> None:
    """限时：过期 grant 拒绝且写 expired_check 审计。"""
    grant = _grant(expires_delta=timedelta(minutes=-1))
    client, repo, _, seen = _build(tmp_path, monkeypatch, [grant])
    response = client.get("/api/v1/memory/learner", headers=_admin_auth(grant_id=grant["grant_id"]))
    assert response.status_code == 403
    assert seen == []
    assert [a["action"] for a in repo.audits] == ["expired_check"]


def test_revoked_grant_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    grant = _grant(revoked=True)
    client, repo, _, _ = _build(tmp_path, monkeypatch, [grant])
    response = client.get("/api/v1/memory/learner", headers=_admin_auth(grant_id=grant["grant_id"]))
    assert response.status_code == 403
    assert repo.audits == []


def test_grant_bound_to_other_admin_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    """限用户：grant 只能由属主 admin 使用。"""
    grant = _grant(admin=OTHER_ADMIN_ID)
    client, _, _, _ = _build(tmp_path, monkeypatch, [grant])
    response = client.get("/api/v1/memory/learner", headers=_admin_auth(grant_id=grant["grant_id"]))
    assert response.status_code == 403


def test_grant_header_requires_admin_actor(tmp_path: Any, monkeypatch: Any) -> None:
    grant = _grant()
    client, _, _, _ = _build(tmp_path, monkeypatch, [grant])
    response = client.get(
        "/api/v1/memory/learner",
        headers={
            "X-Dev-User-Id": str(ADMIN_ID),
            "X-Dev-Actor-Type": "user",
            "X-Dev-Scopes": "memory:read memory:break_glass",
            "X-Break-Glass-Grant-Id": str(grant["grant_id"]),
        },
    )
    assert response.status_code == 403


def test_grant_header_requires_break_glass_scope(tmp_path: Any, monkeypatch: Any) -> None:
    grant = _grant()
    client, _, _, _ = _build(tmp_path, monkeypatch, [grant])
    response = client.get(
        "/api/v1/memory/learner",
        headers=_admin_auth(grant_id=grant["grant_id"], scopes="memory:read"),
    )
    assert response.status_code == 403


def test_break_glass_disabled_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    grant = _grant()
    client, _, _, _ = _build(tmp_path, monkeypatch, [grant], break_glass_enabled=False)
    response = client.get("/api/v1/memory/learner", headers=_admin_auth(grant_id=grant["grant_id"]))
    assert response.status_code == 403


def test_unknown_grant_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    client, _, _, _ = _build(tmp_path, monkeypatch, [])
    response = client.get("/api/v1/memory/learner", headers=_admin_auth(grant_id=uuid4()))
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 创建校验（§13.15：必填 reason/scopes、最长 60 分钟、生产申请者≠批准者）
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 8, 11, tzinfo=UTC)


def _creation_settings(tmp_path: Any, **overrides: Any) -> Settings:
    return _settings(tmp_path, **overrides)


def test_creation_valid(tmp_path: Any) -> None:
    validate_grant_creation(
        settings=_creation_settings(tmp_path),
        reason="故障核查",
        scopes=["memory:read"],
        expires_at=_NOW + timedelta(minutes=30),
        now=_NOW,
        admin_user_id=ADMIN_ID,
        approved_by=None,
    )


def test_creation_rejects_over_60_minutes(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="有效期超过上限"):
        validate_grant_creation(
            settings=_creation_settings(tmp_path),
            reason="故障核查",
            scopes=["memory:read"],
            expires_at=_NOW + timedelta(minutes=61),
            now=_NOW,
            admin_user_id=ADMIN_ID,
            approved_by=None,
        )


def test_creation_requires_reason(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="reason 必填"):
        validate_grant_creation(
            settings=_creation_settings(tmp_path),
            reason="  ",
            scopes=["memory:read"],
            expires_at=_NOW + timedelta(minutes=30),
            now=_NOW,
            admin_user_id=ADMIN_ID,
            approved_by=None,
        )


def test_creation_rejects_unknown_scope(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="未知 scope"):
        validate_grant_creation(
            settings=_creation_settings(tmp_path),
            reason="故障核查",
            scopes=["memory:read", "memory:everything"],
            expires_at=_NOW + timedelta(minutes=30),
            now=_NOW,
            admin_user_id=ADMIN_ID,
            approved_by=None,
        )


def test_creation_production_requires_approver(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="必须指定批准者"):
        validate_grant_creation(
            settings=_creation_settings(
                tmp_path,
                app_env="production",
                dev_auth_enabled=False,
                dev_auth_allow_scope_override=False,
            ),
            reason="故障核查",
            scopes=["memory:read"],
            expires_at=_NOW + timedelta(minutes=30),
            now=_NOW,
            admin_user_id=ADMIN_ID,
            approved_by=None,
        )


def test_creation_production_rejects_self_approval(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="申请者与批准者必须不同"):
        validate_grant_creation(
            settings=_creation_settings(
                tmp_path,
                app_env="production",
                dev_auth_enabled=False,
                dev_auth_allow_scope_override=False,
            ),
            reason="故障核查",
            scopes=["memory:read"],
            expires_at=_NOW + timedelta(minutes=30),
            now=_NOW,
            admin_user_id=ADMIN_ID,
            approved_by=ADMIN_ID,
        )
