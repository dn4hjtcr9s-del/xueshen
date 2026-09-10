"""七牛 Kodo 对象存储适配器（memory-rebuild §5.5 Phase 3）。

**SDK 隔离**：七牛 SDK 只出现在本文件的 :class:`_QiniuKodoClient` 里，业务层只依赖
``contracts/object_store.py`` 的协议；读者/封存器/reconcile 都不 import qiniu。

**为什么再套一层 client 门面**：没有七牛账号时也要能测适配器逻辑（错误分类、幂等、
range、612 语义），因此把所有 SDK 调用收敛为 :class:`_KodoClient` 的六个方法，
默认实现走真实 SDK，测试注入假实现即可覆盖全部逻辑分支，不触网。

**凭据安全**：access/secret key 只在构造时使用，绝不进日志、异常消息或指标标签。
签名 URL 同样不落日志（§5.5）。

已知 SDK 坑（来自社区域 P5 冒烟，见 ``backend/community/storage/kodo.py``）：
``put_data`` / ``BucketManager.delete`` 在 7.18 **不接受** ``timeout`` 参数；
``config.set_default`` 是具名参数签名，误传位置参数会把字符串写进 ``default_zone``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import anyio

from backend.conversation.contracts.object_store import (
    ObjectHashMismatchError,
    ObjectNotFoundError,
    ObjectRef,
    ObjectStat,
    ObjectStoreNonRetryableError,
    ObjectStoreRetryableError,
)
from backend.conversation.rollout.object_store import (
    ROLLOUT_CONTENT_TYPE,
    ROLLOUT_OBJECT_PREFIX,
    _validate_key,
)

logger = logging.getLogger("conversation.rollout.kodo")

#: 七牛「对象不存在」的状态码。
_KODO_NOT_FOUND = 612
#: 七牛「对象已存在且 insertOnly」的状态码。
_KODO_ALREADY_EXISTS = 614


def _classify(status_code: int | None, *, action: str) -> Exception:
    """把 HTTP 状态映射到域级错误；消息里只带状态码与动作，不带 key 之外的上下文。"""
    if status_code is None:
        return ObjectStoreRetryableError(f"Kodo {action} 无响应（可能超时或网络中断）")
    if status_code >= 500:
        return ObjectStoreRetryableError(f"Kodo {action} 服务端错误: {status_code}")
    if status_code >= 400:
        return ObjectStoreNonRetryableError(f"Kodo {action} 客户端错误: {status_code}")
    return ObjectStoreRetryableError(f"Kodo {action} 未预期响应: {status_code}")


@dataclass(slots=True)
class KodoResponse:
    """把 SDK 的 ``(ret, info)`` 二元组收敛成一个结构。"""

    ret: Any
    status_code: int | None
    error: str | None = None


class _KodoClient(Protocol):
    """Kodo 调用门面：默认走真实 SDK，测试注入假实现。"""

    def put(self, *, key: str, data: bytes, insert_only: bool) -> KodoResponse: ...

    def fetch(self, *, key: str, offset: int | None, length: int | None) -> KodoResponse: ...

    def stat(self, *, key: str) -> KodoResponse: ...

    def list_prefix(self, *, prefix: str, limit: int) -> KodoResponse: ...

    def delete(self, *, key: str) -> KodoResponse: ...

    def private_url(self, *, key: str, expires_seconds: int) -> str: ...


class _QiniuKodoClient:
    """真实七牛 SDK 实现（唯一 import qiniu 的地方）。"""

    def __init__(
        self,
        *,
        bucket: str,
        access_key: str,
        secret_key: str,
        domain: str = "",
        connect_timeout_seconds: int = 10,
        read_timeout_seconds: int = 30,
    ) -> None:
        from qiniu import Auth, BucketManager, config, put_data

        self._put_data = put_data
        self._auth: Any = Auth(access_key, secret_key)
        self._bucket_manager: Any = BucketManager(self._auth)
        self._bucket = bucket
        self._domain = domain
        # qiniu 7.18 的 put_data / delete 不接受 timeout 参数，只能设全局；
        # set_default 必须用具名参数（位置参数会被当成 default_zone）。
        config.set_default(connection_timeout=max(connect_timeout_seconds, read_timeout_seconds))

    def put(self, *, key: str, data: bytes, insert_only: bool) -> KodoResponse:
        # insertOnly 是服务端强制的不可变语义：已存在时返回 614，不会覆盖。
        policy = {"insertOnly": 1} if insert_only else None
        token = self._auth.upload_token(self._bucket, key, policy=policy)
        ret, info = self._put_data(token, key, data, mime_type=ROLLOUT_CONTENT_TYPE, check_crc=True)
        return KodoResponse(ret=ret, status_code=_status_of(info))

    def fetch(self, *, key: str, offset: int | None, length: int | None) -> KodoResponse:
        import requests

        url = self._auth.private_download_url(
            self._public_url(key), expires=300, bucket=self._bucket
        )
        headers: dict[str, str] = {}
        if offset is not None and length is not None:
            headers["Range"] = f"bytes={offset}-{offset + length - 1}"
        try:
            response = requests.get(url, headers=headers, timeout=30)
        except Exception as exc:  # 网络异常统一按可重试处理
            return KodoResponse(ret=None, status_code=None, error=str(exc))
        if response.status_code >= 400:
            return KodoResponse(ret=None, status_code=response.status_code)
        return KodoResponse(ret=response.content, status_code=response.status_code)

    def stat(self, *, key: str) -> KodoResponse:
        ret, info = self._bucket_manager.stat(self._bucket, key)
        return KodoResponse(ret=ret, status_code=_status_of(info))

    def list_prefix(self, *, prefix: str, limit: int) -> KodoResponse:
        ret, _eof, info = self._bucket_manager.list(self._bucket, prefix=prefix, limit=limit)
        return KodoResponse(ret=ret, status_code=_status_of(info))

    def delete(self, *, key: str) -> KodoResponse:
        _ret, info = self._bucket_manager.delete(self._bucket, key)
        return KodoResponse(ret=_ret, status_code=_status_of(info))

    def private_url(self, *, key: str, expires_seconds: int) -> str:
        return str(
            self._auth.private_download_url(
                self._public_url(key), expires=expires_seconds, bucket=self._bucket
            )
        )

    def _public_url(self, key: str) -> str:
        # domain 只作为部署方显式配置的读取入口；默认走服务端 SDK/内部下载。
        if self._domain:
            return f"https://{self._domain}/{key}"
        return f"https://{self._bucket}.kodo.example/{key}"


def _run_kodo(call: Any) -> Any:
    """在 worker 线程里执行阻塞的 SDK 调用。

    用 ``abandon_on_cancel=False``（anyio ≥4.1 的写法）：取消时不放弃线程，
    避免 SDK 请求被中途丢下造成"结果未知"的写入。
    """

    return anyio.to_thread.run_sync(call, abandon_on_cancel=False)


def _status_of(info: Any) -> int | None:
    code = getattr(info, "status_code", None)
    return int(code) if code is not None else None


def _etag_of(stat: dict[str, Any]) -> str:
    """Kodo stat 的 ``hash`` 即服务端 ETag；缺失时退化为空串由上层判定。"""
    for field in ("hash", "etag", "md5"):
        value = stat.get(field)
        if value:
            return str(value)
    return ""


class QiniuKodoRolloutObjectStore:
    """:class:`RolloutObjectStore` 的七牛实现。"""

    def __init__(
        self,
        *,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "",
        domain: str = "",
        connect_timeout_seconds: int = 10,
        read_timeout_seconds: int = 30,
        client: _KodoClient | None = None,
    ) -> None:
        if not bucket:
            raise ObjectStoreNonRetryableError("Kodo bucket 未配置")
        self._bucket = bucket
        self._region = region
        self._client: _KodoClient = client or _QiniuKodoClient(
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
            domain=domain,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
        )

    # ------------------------------------------------------------------
    # 协议实现
    # ------------------------------------------------------------------

    async def put_immutable(self, *, key: str, data: bytes) -> ObjectRef:
        """不可变写入。

        依赖服务端 ``insertOnly``：对象已存在时 Kodo 返回 614，本方法随后 stat 出
        既有对象的 etag/size 回报。由于 Kodo 的 ETag 不等于内容 sha256，**无法**判断
        既有对象是否与本次字节相同，因此一律按"同 key 异内容"拒绝——这与 Local/Fake
        的幂等语义有细微差别，已在 bias 登记中说明。
        """
        _validate_key(key)
        response = await _run_kodo(lambda: self._client.put(key=key, data=data, insert_only=True))
        if response.status_code == _KODO_ALREADY_EXISTS:
            raise ObjectHashMismatchError(f"Kodo 对象已存在，拒绝覆盖（段不可变）: {key}")
        if response.status_code is None or response.status_code >= 400:
            raise _classify(response.status_code, action="put")
        stat = await self.head(key=key)
        import hashlib

        return ObjectRef(
            key=key,
            etag=stat.etag,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            content_type=ROLLOUT_CONTENT_TYPE,
        )

    async def get(self, *, key: str) -> bytes:
        _validate_key(key)
        response = await _run_kodo(lambda: self._client.fetch(key=key, offset=None, length=None))
        if response.status_code == _KODO_NOT_FOUND or response.status_code == 404:
            raise ObjectNotFoundError(f"对象不存在: {key}")
        if response.status_code is None or response.status_code >= 400:
            raise _classify(response.status_code, action="get")
        if not isinstance(response.ret, (bytes, bytearray)):
            raise ObjectStoreRetryableError("Kodo 下载返回体类型异常")
        return bytes(response.ret)

    async def get_range(self, *, key: str, offset: int, length: int) -> bytes:
        _validate_key(key)
        if offset < 0 or length < 0:
            raise ObjectStoreNonRetryableError("offset/length 不得为负")
        response = await _run_kodo(
            lambda: self._client.fetch(key=key, offset=offset, length=length)
        )
        if response.status_code == _KODO_NOT_FOUND or response.status_code == 404:
            raise ObjectNotFoundError(f"对象不存在: {key}")
        if response.status_code is None or response.status_code >= 400:
            raise _classify(response.status_code, action="get_range")
        if not isinstance(response.ret, (bytes, bytearray)):
            raise ObjectStoreRetryableError("Kodo range 返回体类型异常")
        data = bytes(response.ret)
        if len(data) != length:
            raise ObjectStoreNonRetryableError(
                f"Kodo range 长度不符: 期望 {length} 实际 {len(data)}（key={key}）"
            )
        return data

    async def head(self, *, key: str) -> ObjectStat:
        _validate_key(key)
        response = await _run_kodo(lambda: self._client.stat(key=key))
        if response.status_code == _KODO_NOT_FOUND or response.status_code == 404:
            raise ObjectNotFoundError(f"对象不存在: {key}")
        if response.status_code is None or response.status_code >= 400:
            raise _classify(response.status_code, action="head")
        stat = response.ret if isinstance(response.ret, dict) else {}
        return ObjectStat(
            key=key,
            etag=_etag_of(stat) or "unknown",
            size=int(stat.get("fsize") or 0),
            content_type=str(stat.get("mimeType") or ROLLOUT_CONTENT_TYPE),
        )

    async def list_prefix(self, *, prefix: str, limit: int = 1000) -> list[ObjectStat]:
        response = await _run_kodo(lambda: self._client.list_prefix(prefix=prefix, limit=limit))
        if response.status_code is None or response.status_code >= 400:
            raise _classify(response.status_code, action="list_prefix")
        items = (response.ret or {}).get("items") or []
        stats: list[ObjectStat] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            if not key:
                continue
            stats.append(
                ObjectStat(
                    key=key,
                    etag=_etag_of(item) or "unknown",
                    size=int(item.get("fsize") or 0),
                    content_type=str(item.get("mimeType") or ROLLOUT_CONTENT_TYPE),
                )
            )
        return stats

    async def delete(self, *, key: str) -> None:
        """幂等删除：612（对象不存在）视为成功，与 Local/Fake 语义一致。"""
        _validate_key(key)
        response = await _run_kodo(lambda: self._client.delete(key=key))
        if response.status_code == _KODO_NOT_FOUND or response.status_code == 404:
            return
        if response.status_code is None or response.status_code >= 400:
            raise _classify(response.status_code, action="delete")

    async def presign_read(self, *, key: str, expires_seconds: int) -> str:
        _validate_key(key)
        return str(
            await _run_kodo(
                lambda: self._client.private_url(key=key, expires_seconds=expires_seconds)
            )
        )

    async def health_check(self) -> bool:
        """连通性探测：列一次 rollout 前缀。

        刻意**不写探测对象**：真实 bucket 里不该因为健康检查留下垃圾。
        """
        try:
            response = await _run_kodo(
                lambda: self._client.list_prefix(prefix=f"{ROLLOUT_OBJECT_PREFIX}/", limit=1)
            )
        except Exception:
            logger.warning("Kodo 健康检查失败（异常）", exc_info=True)
            return False
        code = response.status_code
        return code is not None and code < 400


__all__ = ["KodoResponse", "QiniuKodoRolloutObjectStore"]
