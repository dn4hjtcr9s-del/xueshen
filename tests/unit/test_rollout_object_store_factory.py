"""Rollout 对象存储工厂单元测试（memory-rebuild §5.5 Phase 3）。

工厂是"按配置选 Local/Kodo"的唯一入口，三个装配点共用。这里不需要网络也不需要
数据库：构造 Kodo 实现只做 SDK 客户端初始化，不发请求。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from backend.conversation.contracts.object_store import (
    ObjectStoreNonRetryableError,
    RolloutObjectStore,
)
from backend.conversation.rollout.factory import build_rollout_object_store
from backend.conversation.rollout.object_store import LocalRolloutObjectStore
from backend.conversation.rollout.qiniu_kodo import QiniuKodoRolloutObjectStore


def _settings(**overrides: Any) -> Any:
    base = {
        "conversation_rollout_object_store": "local",
        "conversation_rollout_root": "/tmp/rollouts",
        "kodo_bucket": None,
        "kodo_region": None,
        "kodo_access_key": None,
        "kodo_secret_key": None,
        "kodo_cdn_domain": None,
        "kodo_connect_timeout_seconds": 10,
        "kodo_read_timeout_seconds": 30,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_local_mode_builds_local_store() -> None:
    store = build_rollout_object_store(_settings())
    assert isinstance(store, LocalRolloutObjectStore)
    assert isinstance(store, RolloutObjectStore)


def test_kodo_mode_builds_kodo_store_when_config_complete() -> None:
    store = build_rollout_object_store(
        _settings(
            conversation_rollout_object_store="kodo",
            kodo_bucket="b",
            kodo_region="z0",
            kodo_access_key="ak",
            kodo_secret_key="sk",
        )
    )
    assert isinstance(store, QiniuKodoRolloutObjectStore)
    assert isinstance(store, RolloutObjectStore)


@pytest.mark.parametrize(
    "missing",
    ["kodo_bucket", "kodo_region", "kodo_access_key", "kodo_secret_key"],
)
def test_kodo_mode_fails_loudly_when_any_required_field_missing(missing: str) -> None:
    """缺任何一项都必须失败——静默降级会让生产把正文写到本机磁盘。"""
    config = {
        "conversation_rollout_object_store": "kodo",
        "kodo_bucket": "b",
        "kodo_region": "z0",
        "kodo_access_key": "ak",
        "kodo_secret_key": "sk",
    }
    config[missing] = None
    with pytest.raises(ObjectStoreNonRetryableError, match="不会静默降级"):
        build_rollout_object_store(_settings(**config))


def test_unknown_backend_is_rejected() -> None:
    with pytest.raises(ObjectStoreNonRetryableError, match="未知"):
        build_rollout_object_store(_settings(conversation_rollout_object_store="s3"))
