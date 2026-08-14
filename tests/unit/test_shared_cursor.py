"""backend/shared/cursor.py 共享化回归测试（D13/D24/D40）。

- 与 memory/contracts/common.py 原实现签名/序列化逐字节兼容；
- D13：bind_principal=False 的公共游标不写 principal_hash，验签要求该字段不存在；
- 绑定/不绑定两种游标互斥，不能跨模式复用。
"""

from __future__ import annotations

import time
from uuid import UUID

import pytest

from backend.settings import Settings
from backend.shared.cursor import (
    CursorError,
    canonical_json,
    cursor_principal_hash,
    issue_cursor,
    resolve_cursor,
    sign_cursor,
    verify_cursor,
)

KEY = "test-cursor-key"


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        cursor_hmac_key=KEY,
        cursor_ttl_seconds=900,
    )


# ---------------------------------------------------------------------------
# 与 common.py 原实现的字节级一致性（签名兼容性，改动会破坏既有游标）
# ---------------------------------------------------------------------------


def test_shared_cursor_identical_to_common_impl() -> None:
    """shared 与 memory.contracts.common 的 canonical/sign 实现输出必须一致。"""
    from backend.memory.contracts.common import canonical_json as common_canonical
    from backend.memory.contracts.common import cursor_principal_hash as common_hash
    from backend.memory.contracts.common import sign_cursor as common_sign

    payload = {
        "cursor_version": 1,
        "route": "memory.search",
        "principal_hash": cursor_principal_hash(KEY, "user-1"),
        "normalized_filters": {"q": "极限\n换行", "b": True},
        "sort_key": ["2026-08-10T08:00:00Z", 1.0, "mastery:x"],
        "expires_at": time.time() + 900,
    }
    assert canonical_json(payload) == common_canonical(payload)
    assert cursor_principal_hash(KEY, "user-1") == common_hash(KEY, "user-1")
    assert sign_cursor(KEY, payload) == common_sign(KEY, payload)


# ---------------------------------------------------------------------------
# D13：可选 principal binding
# ---------------------------------------------------------------------------


def test_issue_cursor_default_binds_principal() -> None:
    token = issue_cursor(
        _settings(),
        route="community.posts",
        user_id=UUID(int=1),
        filters={"sort": "latest"},
        sort_key=["2026-08-14T00:00:00Z", "post:2"],
    )
    payload = verify_cursor(
        KEY,
        token,
        route="community.posts",
        principal_hash=cursor_principal_hash(KEY, str(UUID(int=1))),
        normalized_filters={"sort": "latest"},
        now_epoch=time.time(),
    )
    assert payload["route"] == "community.posts"


def test_issue_cursor_public_no_principal() -> None:
    """D13：公共游标 payload 不写 principal_hash。"""
    token = issue_cursor(
        _settings(),
        route="community.posts",
        user_id=UUID(int=1),
        filters={"sort": "latest"},
        sort_key=["2026-08-14T00:00:00Z", "post:2"],
        bind_principal=False,
    )
    payload = verify_cursor(
        KEY,
        token,
        route="community.posts",
        principal_hash=None,
        normalized_filters={"sort": "latest"},
        now_epoch=time.time(),
    )
    assert "principal_hash" not in payload


def test_public_cursor_rejected_with_principal_binding() -> None:
    """D13：公共游标（无 principal）不得在绑定路由中验签通过。"""
    token = issue_cursor(
        _settings(),
        route="community.posts",
        user_id=UUID(int=1),
        filters={"sort": "latest"},
        sort_key=["2026-08-14T00:00:00Z", "post:2"],
        bind_principal=False,
    )
    with pytest.raises(CursorError):
        verify_cursor(
            KEY,
            token,
            route="community.posts",
            principal_hash=cursor_principal_hash(KEY, str(UUID(int=1))),
            normalized_filters={"sort": "latest"},
            now_epoch=time.time(),
        )


def test_bound_cursor_rejected_without_principal() -> None:
    """D13：绑定 principal 的游标不得用于公共验签（要求字段不存在）。"""
    token = issue_cursor(
        _settings(),
        route="community.posts",
        user_id=UUID(int=1),
        filters={"sort": "latest"},
        sort_key=["2026-08-14T00:00:00Z", "post:2"],
    )
    with pytest.raises(CursorError):
        verify_cursor(
            KEY,
            token,
            route="community.posts",
            principal_hash=None,
            normalized_filters={"sort": "latest"},
            now_epoch=time.time(),
        )


def test_public_cursor_user_agnostic() -> None:
    """D13：公共游标与查看者无关——另一用户解析同一游标成功。"""
    token = issue_cursor(
        _settings(),
        route="community.posts",
        user_id=UUID(int=1),
        filters={"sort": "latest"},
        sort_key=["2026-08-14T00:00:00Z", "post:2"],
        bind_principal=False,
    )
    resolve_cursor(
        _settings(),
        token,
        route="community.posts",
        user_id=UUID(int=999),
        filters={"sort": "latest"},
        bind_principal=False,
    )


# ---------------------------------------------------------------------------
# 绑定项：route / filters / sort / expiry（§8.2）
# ---------------------------------------------------------------------------


def test_cursor_binds_route_and_filters() -> None:
    s = _settings()
    token = issue_cursor(
        s,
        route="community.posts",
        user_id=UUID(int=1),
        filters={"sort": "latest"},
        sort_key=["k"],
        bind_principal=False,
    )
    with pytest.raises(CursorError):
        resolve_cursor(
            s,
            token,
            route="community.notifications",
            user_id=UUID(int=1),
            filters={"sort": "latest"},
            bind_principal=False,
        )
    with pytest.raises(CursorError):
        resolve_cursor(
            s,
            token,
            route="community.posts",
            user_id=UUID(int=1),
            filters={"sort": "unanswered"},
            bind_principal=False,
        )


def test_cursor_expiry() -> None:
    s = _settings()
    token = issue_cursor(
        s,
        route="community.posts",
        user_id=UUID(int=1),
        filters={"sort": "latest"},
        sort_key=["k"],
        bind_principal=False,
    )
    with pytest.raises(CursorError) as excinfo:
        verify_cursor(
            KEY,
            token,
            route="community.posts",
            principal_hash=None,
            normalized_filters={"sort": "latest"},
            now_epoch=time.time() + 10_000,
        )
    assert excinfo.value.code == "CURSOR_EXPIRED"
