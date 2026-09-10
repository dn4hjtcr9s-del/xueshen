"""Rollout 对象存储协议与域级错误分类（memory-rebuild 计划 §5.2 Phase 0-A / §5.5）。

业务层只依赖本模块的 :class:`RolloutObjectStore` 与 :class:`ObjectRef`；七牛 Kodo
的 SDK 类型只允许出现在 adapter 内部（§5.5"业务层只依赖协议和 ObjectRef"）。
本地目录实现、Fake 实现与 Kodo 适配器都在 Phase 3 落地，Phase 0 只固定接口形状。

``ObjectStat`` 与 ``ObjectRef`` 分开建模的原因：``put_immutable`` 的调用方持有
对象字节，可以本地算出内容 sha256；而 ``head`` / ``list_prefix`` 只能拿到对象存储
的服务端元数据，服务端 ETag 在 Kodo/S3 上可能等于分片 MD5 而非内容 sha256，
不能冒充内容哈希。manifest 表同时保存 ``object_etag`` 与 ``sha256`` 两列即为此。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SHA256_HEX_LENGTH = 64


class ObjectStoreError(Exception):
    """对象存储错误基类；``retryable`` 供调用方决定是否退避重试。"""

    retryable: bool = False


class ObjectStoreRetryableError(ObjectStoreError):
    """可重试：网络抖动、限流、服务端 5xx。"""

    retryable = True


class ObjectStoreNonRetryableError(ObjectStoreError):
    """不可重试：鉴权失败、参数非法、bucket/region 配置错误。"""

    retryable = False


class ObjectNotFoundError(ObjectStoreError):
    """对象不存在：key 合法但对象存储中没有对应对象。"""

    retryable = False


class ObjectHashMismatchError(ObjectStoreError):
    """同一 key 已存在且内容 sha256 不同（§5.5：拒绝覆盖或报告 hash 冲突）。

    ``put_immutable`` 的语义是"段封存后不可变"，因此同 key 异内容必须报错
    而不是静默覆盖；同 key 同内容的重放则视为幂等成功。
    """

    retryable = False


def _validate_sha256(value: str) -> str:
    if len(value) != _SHA256_HEX_LENGTH:
        raise ValueError("sha256 必须是 64 位十六进制")
    lowered = value.lower()
    if any(ch not in "0123456789abcdef" for ch in lowered):
        raise ValueError("sha256 必须是 64 位十六进制")
    return lowered


class ObjectStat(BaseModel):
    """对象存储返回的服务端元数据（不承诺内容 sha256）。"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=1024)
    etag: str = Field(min_length=1, max_length=256)
    size: int = Field(ge=0)
    content_type: str = Field(default="application/x-ndjson", max_length=128)


class ObjectRef(ObjectStat):
    """一次成功写入的对象引用：服务端元数据 + 本地计算的内容 sha256。"""

    sha256: str

    @field_validator("sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        return _validate_sha256(value)


@runtime_checkable
class RolloutObjectStore(Protocol):
    """rollout 段的不可变对象存储边界（§5.5）。

    实现约束（Phase 3 验收）：
    - ``put_immutable`` 必须幂等：同 key 同内容返回既有引用；同 key 异内容抛
      :class:`ObjectHashMismatchError`，不得静默覆盖；
    - 所有方法在失败时抛 :class:`ObjectStoreError` 子类，由调用方按 ``retryable``
      决定退避重试或直接失败，不允许把 SDK 异常泄到业务层；
    - ``get_range`` 的 ``offset``/``length`` 必须与 PG 中记录的字节范围语义一致。
    """

    async def put_immutable(self, *, key: str, data: bytes) -> ObjectRef: ...

    async def get(self, *, key: str) -> bytes: ...

    async def get_range(self, *, key: str, offset: int, length: int) -> bytes: ...

    async def head(self, *, key: str) -> ObjectStat: ...

    async def list_prefix(self, *, prefix: str, limit: int = 1000) -> list[ObjectStat]: ...

    async def delete(self, *, key: str) -> None: ...

    async def presign_read(self, *, key: str, expires_seconds: int) -> str: ...

    async def health_check(self) -> bool: ...
