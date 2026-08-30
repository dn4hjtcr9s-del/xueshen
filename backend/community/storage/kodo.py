"""七牛 Kodo 存储后端。"""

from __future__ import annotations

from typing import IO, Any, cast

import anyio

from backend.community.storage.base import StorageBackend, StorageResult
from backend.settings import Settings

# 延迟导入七牛 SDK，避免未配置时强依赖报错
_qiniu_sdk: tuple[Any, Any] | None = None


def _import_qiniu() -> tuple[Any, Any]:
    global _qiniu_sdk
    if _qiniu_sdk is not None:
        return _qiniu_sdk
    from qiniu import Auth, BucketManager

    _qiniu_sdk = (Auth, BucketManager)
    return _qiniu_sdk


class KodoStorage(StorageBackend):
    """七牛 Kodo 对象存储后端（异步包装）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        Auth, BucketManager = _import_qiniu()
        self.auth: Any = Auth(settings.kodo_access_key, settings.kodo_secret_key)
        self.bucket_manager: Any = BucketManager(self.auth)
        self.bucket = settings.kodo_bucket
        self.cdn_domain = settings.kodo_cdn_domain
        self.connect_timeout = settings.kodo_connect_timeout_seconds
        self.read_timeout = settings.kodo_read_timeout_seconds
        # qiniu 7.18 的 put_data / BucketManager.delete 均不接受 timeout 参数
        # （旧代码按旧版 SDK 传参会 TypeError，P5 冒烟实测踩坑）；超时只能走全局配置，
        # 且 SDK 只有 connection_timeout 一项，取两者较大值兜底。
        from qiniu import config

        config.set_default("connection_timeout", max(self.connect_timeout, self.read_timeout))

    async def upload(
        self,
        key: str,
        data: IO[bytes],
        mime: str,
        size_bytes: int,
    ) -> StorageResult:
        from qiniu import put_data

        token = self.auth.upload_token(self.bucket, key)
        file_data = data.read()

        def _do_upload() -> tuple[Any, Any]:
            return cast(
                tuple[Any, Any],
                put_data(
                    token,
                    key,
                    file_data,
                    mime_type=mime,
                    check_crc=True,
                ),
            )

        try:
            ret, info = await anyio.to_thread.run_sync(
                _do_upload,
                limiter=None,
                cancellable=False,
            )
        except TimeoutError as exc:
            return StorageResult(
                storage_key=key,
                success=False,
                error_message=f"上传超时: {exc}",
            )
        except Exception as exc:
            return StorageResult(
                storage_key=key,
                success=False,
                error_message=str(exc),
            )

        if info is not None and info.status_code >= 500:
            return StorageResult(
                storage_key=key,
                success=False,
                error_message=f"Kodo 服务错误: {info.status_code}",
                status_code=int(info.status_code),
            )
        if info is not None and info.status_code >= 400:
            return StorageResult(
                storage_key=key,
                success=False,
                error_message=f"Kodo 客户端错误: {info.status_code}",
                status_code=int(info.status_code),
            )
        if not ret or "key" not in ret:
            # HTTP 200 但响应体缺 key → retryable=false（§7.9/§7.11）
            return StorageResult(
                storage_key=key,
                success=False,
                error_message="Kodo 返回缺少 key",
                status_code=int(info.status_code) if info is not None else 200,
            )
        return StorageResult(storage_key=key, success=True)

    async def delete(self, key: str) -> StorageResult:
        def _do_delete() -> tuple[Any, Any]:
            return cast(tuple[Any, Any], self.bucket_manager.delete(self.bucket, key))

        try:
            _ret, info = await anyio.to_thread.run_sync(
                _do_delete,
                limiter=None,
                cancellable=False,
            )
        except TimeoutError as exc:
            return StorageResult(
                storage_key=key,
                success=False,
                error_message=f"删除超时: {exc}",
            )
        except Exception as exc:
            return StorageResult(
                storage_key=key,
                success=False,
                error_message=str(exc),
            )

        # 612 对象不存在视为成功
        if info is not None and info.status_code == 612:
            return StorageResult(storage_key=key, success=True)
        if info is not None and info.status_code >= 500:
            return StorageResult(
                storage_key=key,
                success=False,
                error_message=f"Kodo 删除服务错误: {info.status_code}",
            )
        if info is not None and info.status_code >= 400:
            return StorageResult(
                storage_key=key,
                success=False,
                error_message=f"Kodo 删除客户端错误: {info.status_code}",
            )
        return StorageResult(storage_key=key, success=True)

    def public_url(self, key: str) -> str:
        return f"https://{self.cdn_domain}/{key}"
