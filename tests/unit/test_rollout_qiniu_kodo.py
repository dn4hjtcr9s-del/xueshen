"""七牛 Kodo 适配器单元测试（memory-rebuild §5.5 Phase 3）。

没有七牛账号，因此这里注入假 SDK 门面（``_KodoClient``）覆盖适配器**全部逻辑分支**：
状态码分类（5xx 可重试 / 4xx 不可重试 / 612 不存在 / 614 已存在）、range 长度校验、
head 字段映射、幂等删除、凭据不进错误消息。真实网络 smoke 在
``tests/integration/test_rollout_kodo_smoke.py``，无凭据时自动 skip。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import pytest

from backend.conversation.contracts.object_store import (
    ObjectHashMismatchError,
    ObjectNotFoundError,
    ObjectStoreNonRetryableError,
    ObjectStoreRetryableError,
    RolloutObjectStore,
)
from backend.conversation.rollout.qiniu_kodo import KodoResponse, QiniuKodoRolloutObjectStore

_KEY = "rollouts/threads/2026/09/10/11111111-1111-4111-8111-111111111111/000000000000-a.jsonl"
_DATA = b'{"ordinal": 0, "type": "thread_meta"}\n'


@dataclass
class _Call:
    name: str
    kwargs: dict[str, Any]


class FakeKodoClient:
    """按脚本返回状态的假门面；记录调用以便断言。"""

    def __init__(self, **responses: KodoResponse) -> None:
        self._responses = responses
        self.calls: list[_Call] = []
        self.delete_calls: list[str] = []

    def _next(self, name: str, **kwargs: Any) -> KodoResponse:
        self.calls.append(_Call(name=name, kwargs=kwargs))
        return self._responses.get(name, KodoResponse(ret=None, status_code=200))

    def put(self, *, key: str, data: bytes, insert_only: bool) -> KodoResponse:
        return self._next("put", key=key, data=data, insert_only=insert_only)

    def fetch(self, *, key: str, offset: int | None, length: int | None) -> KodoResponse:
        return self._next("fetch", key=key, offset=offset, length=length)

    def stat(self, *, key: str) -> KodoResponse:
        return self._next("stat", key=key)

    def list_prefix(self, *, prefix: str, limit: int) -> KodoResponse:
        return self._next("list_prefix", prefix=prefix, limit=limit)

    def delete(self, *, key: str) -> KodoResponse:
        self.delete_calls.append(key)
        return self._next("delete", key=key)

    def private_url(self, *, key: str, expires_seconds: int) -> str:
        self._next("private_url", key=key, expires_seconds=expires_seconds)
        return f"https://example.test/{key}?e={expires_seconds}"


def _store(client: FakeKodoClient) -> QiniuKodoRolloutObjectStore:
    return QiniuKodoRolloutObjectStore(bucket="b", access_key="AK", secret_key="SK", client=client)


def _stat_response(**overrides: Any) -> KodoResponse:
    payload = {"fsize": len(_DATA), "hash": "Fetag123", "mimeType": "application/x-ndjson"}
    payload.update(overrides)
    return KodoResponse(ret=payload, status_code=200)


# ---------------------------------------------------------------------------
# put_immutable
# ---------------------------------------------------------------------------


async def test_put_returns_ref_with_local_content_hash() -> None:
    """ETag 来自服务端、sha256 本地算，两者不同源但都要正确。"""
    client = FakeKodoClient(
        put=KodoResponse(ret={"key": _KEY}, status_code=200), stat=_stat_response()
    )
    ref = await _store(client).put_immutable(key=_KEY, data=_DATA)
    assert ref.key == _KEY
    assert ref.size == len(_DATA)
    assert ref.sha256 == hashlib.sha256(_DATA).hexdigest()
    assert ref.etag == "Fetag123"
    assert ref.etag != ref.sha256


async def test_put_requests_insert_only_for_immutability() -> None:
    """不可变语义必须靠服务端 restrict，不能只依赖应用层判断。"""
    client = FakeKodoClient(
        put=KodoResponse(ret={"key": _KEY}, status_code=200), stat=_stat_response()
    )
    await _store(client).put_immutable(key=_KEY, data=_DATA)
    put_call = next(call for call in client.calls if call.name == "put")
    assert put_call.kwargs["insert_only"] is True


async def test_put_on_existing_object_reports_hash_conflict() -> None:
    client = FakeKodoClient(put=KodoResponse(ret=None, status_code=614))
    with pytest.raises(ObjectHashMismatchError):
        await _store(client).put_immutable(key=_KEY, data=_DATA)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (500, ObjectStoreRetryableError),
        (503, ObjectStoreRetryableError),
        (403, ObjectStoreNonRetryableError),
    ],
)
async def test_put_classifies_status_codes(status: int, expected: type[Exception]) -> None:
    client = FakeKodoClient(put=KodoResponse(ret=None, status_code=status))
    with pytest.raises(expected):
        await _store(client).put_immutable(key=_KEY, data=_DATA)


# ---------------------------------------------------------------------------
# get / get_range
# ---------------------------------------------------------------------------


async def test_get_returns_bytes() -> None:
    client = FakeKodoClient(fetch=KodoResponse(ret=_DATA, status_code=200))
    assert await _store(client).get(key=_KEY) == _DATA


async def test_get_missing_object_raises_not_found() -> None:
    client = FakeKodoClient(fetch=KodoResponse(ret=None, status_code=612))
    with pytest.raises(ObjectNotFoundError):
        await _store(client).get(key=_KEY)


async def test_get_range_passes_offset_and_length() -> None:
    client = FakeKodoClient(fetch=KodoResponse(ret=b"abc", status_code=206))
    assert await _store(client).get_range(key=_KEY, offset=7, length=3) == b"abc"
    fetch_call = next(call for call in client.calls if call.name == "fetch")
    assert fetch_call.kwargs == {"key": _KEY, "offset": 7, "length": 3}


async def test_get_range_rejects_short_read() -> None:
    """Kodo 少给了字节 → 不可重试错误，绝不把截断内容当完整区间返回。"""
    client = FakeKodoClient(fetch=KodoResponse(ret=b"ab", status_code=206))
    with pytest.raises(ObjectStoreNonRetryableError, match="长度不符"):
        await _store(client).get_range(key=_KEY, offset=0, length=5)


async def test_get_range_rejects_negative_arguments() -> None:
    client = FakeKodoClient()
    with pytest.raises(ObjectStoreNonRetryableError):
        await _store(client).get_range(key=_KEY, offset=-1, length=5)


# ---------------------------------------------------------------------------
# head / list_prefix
# ---------------------------------------------------------------------------


async def test_head_maps_kodo_fields() -> None:
    client = FakeKodoClient(stat=_stat_response(fsize=1234, hash="Fxyz"))
    stat = await _store(client).head(key=_KEY)
    assert stat.size == 1234
    assert stat.etag == "Fxyz"
    assert stat.content_type == "application/x-ndjson"
    assert "sha256" not in stat.model_dump(), "head 不承诺内容哈希"


async def test_head_missing_object() -> None:
    client = FakeKodoClient(stat=KodoResponse(ret=None, status_code=612))
    with pytest.raises(ObjectNotFoundError):
        await _store(client).head(key=_KEY)


async def test_list_prefix_maps_items_and_skips_malformed() -> None:
    client = FakeKodoClient(
        list_prefix=KodoResponse(
            ret={
                "items": [
                    {"key": _KEY, "fsize": 10, "hash": "F1"},
                    {"fsize": 5},  # 缺 key → 跳过
                    "not-a-dict",  # 非法项 → 跳过
                ]
            },
            status_code=200,
        )
    )
    stats = await _store(client).list_prefix(prefix="rollouts/")
    assert [stat.key for stat in stats] == [_KEY]
    assert stats[0].size == 10


# ---------------------------------------------------------------------------
# delete / presign_read / health_check
# ---------------------------------------------------------------------------


async def test_delete_is_idempotent_on_missing_object() -> None:
    """612 视为成功：retention 重跑不应因对象已删而报错。"""
    client = FakeKodoClient(delete=KodoResponse(ret=None, status_code=612))
    await _store(client).delete(key=_KEY)


async def test_delete_classifies_server_error_as_retryable() -> None:
    client = FakeKodoClient(delete=KodoResponse(ret=None, status_code=503))
    with pytest.raises(ObjectStoreRetryableError):
        await _store(client).delete(key=_KEY)


async def test_presign_read_returns_signed_url() -> None:
    client = FakeKodoClient()
    url = await _store(client).presign_read(key=_KEY, expires_seconds=120)
    assert url.startswith("https://")


async def test_health_check_does_not_write_any_object() -> None:
    """健康检查不能往真实 bucket 里写探测对象。"""
    client = FakeKodoClient(list_prefix=KodoResponse(ret={"items": []}, status_code=200))
    assert await _store(client).health_check() is True
    assert [call.name for call in client.calls] == ["list_prefix"]


async def test_health_check_false_on_server_error() -> None:
    client = FakeKodoClient(list_prefix=KodoResponse(ret=None, status_code=500))
    assert await _store(client).health_check() is False


async def test_health_check_false_on_exception() -> None:
    class _Boom(FakeKodoClient):
        def list_prefix(self, *, prefix: str, limit: int) -> KodoResponse:
            raise RuntimeError("network down")

    assert await _store(_Boom()).health_check() is False


# ---------------------------------------------------------------------------
# 安全与协议
# ---------------------------------------------------------------------------


async def test_error_messages_never_contain_credentials() -> None:
    """凭据绝不能出现在异常消息里（异常会进日志与告警）。"""
    client = FakeKodoClient(put=KodoResponse(ret=None, status_code=403))
    store = QiniuKodoRolloutObjectStore(
        bucket="b", access_key="SUPER_SECRET_AK", secret_key="SUPER_SECRET_SK", client=client
    )
    with pytest.raises(ObjectStoreNonRetryableError) as excinfo:
        await store.put_immutable(key=_KEY, data=_DATA)
    message = str(excinfo.value)
    assert "SUPER_SECRET_AK" not in message
    assert "SUPER_SECRET_SK" not in message


def test_missing_bucket_is_rejected() -> None:
    with pytest.raises(ObjectStoreNonRetryableError):
        QiniuKodoRolloutObjectStore(
            bucket="", access_key="a", secret_key="b", client=FakeKodoClient()
        )


async def test_satisfies_rollout_object_store_protocol() -> None:
    assert isinstance(_store(FakeKodoClient()), RolloutObjectStore)


@pytest.mark.parametrize("bad_key", ["/abs.jsonl", "rollouts/../escape.jsonl", ""])
async def test_rejects_illegal_keys(bad_key: str) -> None:
    with pytest.raises(ObjectStoreNonRetryableError):
        await _store(FakeKodoClient()).get(key=bad_key)
