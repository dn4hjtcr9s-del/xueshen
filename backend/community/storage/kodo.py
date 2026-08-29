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
            )
        if info is not None and info.status_code >= 400:
            return StorageResult(
                storage_key=key,
                success=False,
                error_message=f"Kodo 客户端错误: {info.status_code}",
            )
        if not ret or "key" not in ret:
            return StorageResult(
                storage_key=key,
                success=False,
                error_message="Kodo 返回缺少 key",
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
