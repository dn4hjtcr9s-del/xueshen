"""rollout 装配路径与热缓存删除能力单元测试（评审 C-3 / I-6 / I-7）。

三组断言：

1. **热缓存删除能力**：Local 真的 unlink 热段路径且幂等，删不掉（权限/IO）必须抛错
   而不是静默成功；**factory 产出的 kodo store 也必须能删本地热段**（review-2 新发现 15：
   recorder 在 kodo 模式照样写本地热段），未挂载热缓存的 kodo 实现显式报错而不是
   假装删掉了。
2. **worker 装配**（I-6/I-7）：对象存储**始终**经 ``build_rollout_object_store``
   构造（不受 ``CONVERSATION_ROLLOUT_ENABLED`` 约束），recorder 仍然只在 flag 打开时
   装配；kodo 缺凭据时启动即失败，绝不静默降级为 local。
3. **app 装配**（I-7）：``create_app(settings)`` 用**入参** Settings 构造对象存储，
   不再回退全局 ``get_settings()``。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from backend.app import create_app
from backend.conversation.contracts.object_store import (
    ObjectStoreNonRetryableError,
    RolloutObjectStore,
)
from backend.conversation.rollout import RolloutRecorder
from backend.conversation.rollout.factory import build_rollout_object_store
from backend.conversation.rollout.object_store import (
    FakeRolloutObjectStore,
    LocalRolloutObjectStore,
    build_object_key,
    delete_hot_segment,
    local_path_for_object_key,
)
from backend.conversation.rollout.qiniu_kodo import QiniuKodoRolloutObjectStore
from backend.conversation.worker.main import build_rollout_runtime
from backend.settings import Settings

_LOGGER = logging.getLogger("test.rollout.assembly")


class _StubKodoClient:
    """最小 Kodo client 门面：本文件不触网，只要构造期不调用 SDK 即可。"""

    def put(self, **kwargs: Any) -> Any:  # pragma: no cover - 不应被调用
        raise AssertionError("本用例不应触网")

    def fetch(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("本用例不应触网")

    def stat(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("本用例不应触网")

    def list_prefix(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("本用例不应触网")

    def delete(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("本用例不应触网")

    def private_url(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("本用例不应触网")


_OBJECT_KEY = build_object_key(
    thread_created_at=datetime(2026, 9, 10, 5, 0, tzinfo=UTC),
    thread_id=uuid4(),
    ordinal_start=0,
    segment_id=uuid4(),
)


def _worker_settings(**overrides: Any) -> Settings:
    """worker 装配用的最小 Settings（app_env=test 不做生产强校验）。"""
    return Settings(app_env="test", **overrides)


# ---------------------------------------------------------------------------
# 热缓存删除能力
# ---------------------------------------------------------------------------


def test_local_store_delete_hot_segment_unlinks_and_is_idempotent(tmp_path: Path) -> None:
    """Local store 必须真的删掉 ``{root}/threads/...`` 下的热段文件。"""
    store = LocalRolloutObjectStore(root=tmp_path)
    hot_path = local_path_for_object_key(root=tmp_path, object_key=_OBJECT_KEY)
    hot_path.parent.mkdir(parents=True, exist_ok=True)
    hot_path.write_bytes('{"payload": {"content": "用户原文"}}\n'.encode())
    assert hot_path.is_file()

    asyncio.run(store.delete_hot_segment(key=_OBJECT_KEY))
    assert not hot_path.exists(), "热缓存段必须被物理删除"
    # 幂等：重复删除不得报错（retention / 删除 Job 会重跑）
    asyncio.run(store.delete_hot_segment(key=_OBJECT_KEY))


def test_local_store_delete_hot_segment_propagates_failure(tmp_path: Path) -> None:
    """删不掉（这里是目录占位）必须抛错，不能退化成"看起来删了"。"""
    store = LocalRolloutObjectStore(root=tmp_path)
    hot_path = local_path_for_object_key(root=tmp_path, object_key=_OBJECT_KEY)
    hot_path.mkdir(parents=True, exist_ok=True)

    with pytest.raises(OSError):
        asyncio.run(store.delete_hot_segment(key=_OBJECT_KEY))


def test_delete_hot_segment_helper_records_fake_calls() -> None:
    """Fake 实现记录调用，便于断言删除路径确实要求删热段。"""
    store = FakeRolloutObjectStore()
    assert isinstance(store, RolloutObjectStore)
    assert asyncio.run(delete_hot_segment(object_store=store, key=_OBJECT_KEY)) is True
    assert store.hot_segments_deleted == [_OBJECT_KEY]


def test_factory_built_kodo_store_deletes_local_hot_segments(tmp_path: Path) -> None:
    """新发现 15：kodo 部署下 recorder 仍写本地热段 → factory 必须挂上清理能力。"""
    store = build_rollout_object_store(
        _worker_settings(
            conversation_rollout_root=str(tmp_path),
            conversation_rollout_object_store="kodo",
            kodo_bucket="b",
            kodo_region="z0",
            kodo_access_key="ak",
            kodo_secret_key="sk",
        )
    )
    assert isinstance(store, QiniuKodoRolloutObjectStore)
    hot_path = local_path_for_object_key(root=tmp_path, object_key=_OBJECT_KEY)
    hot_path.parent.mkdir(parents=True, exist_ok=True)
    hot_path.write_text("用户原文", encoding="utf-8")

    assert asyncio.run(delete_hot_segment(object_store=store, key=_OBJECT_KEY)) is True
    assert not hot_path.exists(), "kodo 模式下本地热段必须真的被删掉"
    # 幂等：再删一次不报错
    assert asyncio.run(delete_hot_segment(object_store=store, key=_OBJECT_KEY)) is True


def test_kodo_store_without_hot_cache_fails_loudly() -> None:
    """未挂载热缓存的 kodo 实现必须显式报错，而不是静默 no-op（新发现 15 的病根）。"""
    store = QiniuKodoRolloutObjectStore(
        bucket="b", access_key="ak", secret_key="sk", region="z0", client=_StubKodoClient()
    )
    with pytest.raises(ObjectStoreNonRetryableError):
        asyncio.run(store.delete_hot_segment(key=_OBJECT_KEY))
    with pytest.raises(ObjectStoreNonRetryableError):
        asyncio.run(store.delete_hot_thread(thread_id=uuid4()))


def test_delete_hot_segment_helper_returns_false_without_capability() -> None:
    """完全没有该能力的任意对象仍是安全 no-op（调用方据此判失败，不假装删了）。"""
    assert asyncio.run(delete_hot_segment(object_store=object(), key=_OBJECT_KEY)) is False


# ---------------------------------------------------------------------------
# worker 装配（I-6 / I-7）
# ---------------------------------------------------------------------------


def test_worker_builds_object_store_via_factory_even_when_flag_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """I-6：flag 关闭时对象存储**仍然**经 factory 构造，recorder 保持 None。"""
    sentinel = object()
    calls: list[Any] = []

    def fake_factory(settings: Any) -> Any:
        calls.append(settings)
        return sentinel

    monkeypatch.setattr(
        "backend.conversation.rollout.factory.build_rollout_object_store", fake_factory
    )
    settings = _worker_settings(
        conversation_rollout_root=str(tmp_path), conversation_rollout_enabled=False
    )

    object_store, recorder = build_rollout_runtime(
        settings=settings, session_factory=None, logger=_LOGGER
    )

    assert calls == [settings], "必须把 worker 的 Settings 原样交给 factory"
    assert object_store is sentinel
    assert recorder is None, "flag 关闭时不得装配 recorder（record_rollout 保持 no-op）"


def test_worker_builds_recorder_via_factory_when_flag_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """flag 打开时 recorder 与 sealer 共用同一个 factory 产出的对象存储。"""
    sentinel = object()

    def fake_factory(settings: Any) -> Any:
        return sentinel

    monkeypatch.setattr(
        "backend.conversation.rollout.factory.build_rollout_object_store", fake_factory
    )
    settings = _worker_settings(
        conversation_rollout_root=str(tmp_path), conversation_rollout_enabled=True
    )

    object_store, recorder = build_rollout_runtime(
        settings=settings, session_factory=None, logger=_LOGGER
    )

    assert object_store is sentinel
    assert isinstance(recorder, RolloutRecorder)
    # sealer 必须拿到同一个 store（否则封存会写去另一个后端）；私有属性访问仅用于此断言
    assert recorder._sealer._object_store is sentinel


def test_worker_assembly_fails_loudly_when_kodo_credentials_missing(tmp_path: Path) -> None:
    """I-7/§5.12：kodo 缺凭据时装配直接失败，语义与 factory 单元测试一致。"""
    settings = SimpleNamespace(
        conversation_rollout_object_store="kodo",
        conversation_rollout_root=str(tmp_path),
        conversation_rollout_enabled=False,
        kodo_bucket="b",
        kodo_region="z0",
        kodo_access_key=None,
        kodo_secret_key=None,
    )
    with pytest.raises(ObjectStoreNonRetryableError, match="不会静默降级"):
        build_rollout_runtime(settings=settings, session_factory=None, logger=_LOGGER)


# ---------------------------------------------------------------------------
# app 装配（I-7）
# ---------------------------------------------------------------------------


def test_app_builds_rollout_object_store_from_injected_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """app 读路径装配必须用 create_app 的入参 Settings，且统一走 factory。"""
    sentinel = object()
    calls: list[Any] = []

    def fake_factory(settings: Any) -> Any:
        calls.append(settings)
        return sentinel

    monkeypatch.setattr(
        "backend.conversation.rollout.factory.build_rollout_object_store", fake_factory
    )
    # 故意把进程 env 指向另一个 root：混用 get_settings() 时本断言会失败
    monkeypatch.setenv("CONVERSATION_ROLLOUT_ROOT", str(tmp_path / "env-root"))
    settings = Settings(
        app_env="test",
        memory_storage_root=str(tmp_path / "storage"),
        conversation_rollout_read_enabled=True,
        conversation_rollout_root=str(tmp_path / "injected-root"),
    )

    create_app(settings)

    assert len(calls) == 1, "读路径开启时必须构造一次对象存储"
    assert calls[0] is settings
    assert calls[0].conversation_rollout_root == str(tmp_path / "injected-root")


def test_app_skips_rollout_store_when_read_flag_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """flag 关闭时 app 不构造对象存储（读路径不启用，行为与今天一致）。"""
    called: list[Any] = []

    def fake_factory(settings: Any) -> Any:
        called.append(settings)
        return object()

    monkeypatch.setattr(
        "backend.conversation.rollout.factory.build_rollout_object_store", fake_factory
    )
    settings = Settings(
        app_env="test",
        memory_storage_root=str(tmp_path / "storage"),
        conversation_rollout_read_enabled=False,
    )

    create_app(settings)

    assert called == []
