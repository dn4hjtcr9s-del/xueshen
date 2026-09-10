"""对象存储契约测试（memory-rebuild §5.2 Phase 0-A / §5.5）。

覆盖 ObjectRef / ObjectStat 的字段语义、sha256 规范化、域级错误分类的
``retryable`` 标记，以及 RolloutObjectStore 协议的运行时可判定性
（Phase 3 的 Local/Fake/Kodo 三个实现都要靠它做结构校验）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.conversation.contracts.object_store import (
    ObjectHashMismatchError,
    ObjectNotFoundError,
    ObjectRef,
    ObjectStat,
    ObjectStoreError,
    ObjectStoreNonRetryableError,
    ObjectStoreRetryableError,
    RolloutObjectStore,
)

_SHA256 = "ab" * 32


def test_object_ref_carries_etag_and_content_hash_separately() -> None:
    """etag 与 sha256 分开：Kodo/S3 的 ETag 不等于内容 sha256，不能互相冒充。"""
    ref = ObjectRef(
        key="rollouts/threads/2026/08/28/t1/0-abc.jsonl",
        etag="FQ3xM2Vl-NotAContentHash",
        sha256=_SHA256,
        size=2048,
    )
    assert ref.etag != ref.sha256
    assert ref.content_type == "application/x-ndjson"


def test_object_ref_normalizes_sha256_to_lowercase() -> None:
    ref = ObjectRef(key="k", etag="e", sha256=_SHA256.upper(), size=1)
    assert ref.sha256 == _SHA256


@pytest.mark.parametrize(
    "bad_sha",
    ["", "abc", "z" * 64, "a" * 63, "a" * 65],
)
def test_object_ref_rejects_malformed_sha256(bad_sha: str) -> None:
    with pytest.raises(ValidationError):
        ObjectRef(key="k", etag="e", sha256=bad_sha, size=1)


def test_object_ref_rejects_negative_size() -> None:
    with pytest.raises(ValidationError):
        ObjectRef(key="k", etag="e", sha256=_SHA256, size=-1)


def test_object_stat_has_no_content_hash() -> None:
    """head/list 只能拿到服务端元数据，因此 ObjectStat 不承诺 sha256。"""
    stat = ObjectStat(key="k", etag="e", size=10)
    assert "sha256" not in stat.model_dump()
    with pytest.raises(ValidationError):
        ObjectStat(key="k", etag="e", size=10, sha256=_SHA256)


def test_object_ref_is_an_object_stat() -> None:
    assert issubclass(ObjectRef, ObjectStat)


# ---------------------------------------------------------------------------
# 错误分类（§5.12：可重试/不可重试/不存在必须可区分）
# ---------------------------------------------------------------------------


def test_error_classification_flags() -> None:
    assert ObjectStoreRetryableError.retryable is True
    assert ObjectStoreNonRetryableError.retryable is False
    assert ObjectNotFoundError.retryable is False
    assert ObjectHashMismatchError.retryable is False


def test_all_object_store_errors_share_a_base() -> None:
    for error in (
        ObjectStoreRetryableError,
        ObjectStoreNonRetryableError,
        ObjectNotFoundError,
        ObjectHashMismatchError,
    ):
        assert issubclass(error, ObjectStoreError)
    assert ObjectStoreError.retryable is False


# ---------------------------------------------------------------------------
# 协议形状
# ---------------------------------------------------------------------------


class _FakeObjectStore:
    async def put_immutable(self, *, key: str, data: bytes) -> ObjectRef: ...

    async def get(self, *, key: str) -> bytes: ...

    async def get_range(self, *, key: str, offset: int, length: int) -> bytes: ...

    async def head(self, *, key: str) -> ObjectStat: ...

    async def list_prefix(self, *, prefix: str, limit: int = 1000) -> list[ObjectStat]: ...

    async def delete(self, *, key: str) -> None: ...

    async def presign_read(self, *, key: str, expires_seconds: int) -> str: ...

    async def health_check(self) -> bool: ...


def test_complete_implementation_satisfies_protocol() -> None:
    assert isinstance(_FakeObjectStore(), RolloutObjectStore)


def test_incomplete_implementation_fails_protocol_check() -> None:
    class _Partial:
        async def put_immutable(self, *, key: str, data: bytes) -> ObjectRef: ...

    assert not isinstance(_Partial(), RolloutObjectStore)
