"""Local 文件存储后端（开发/测试用）。"""

from __future__ import annotations

from pathlib import Path
from typing import IO

from backend.community.storage.base import StorageBackend, StorageResult


class LocalStorage(StorageBackend):
    """本地文件存储：文件直接写入 upload_dir/community/... 目录。

    key 本身以 `community/` 开头，因此 base_path 不再额外追加 community，
    避免写成 upload_dir/community/community/... 的重复目录。
    """

    def __init__(self, upload_dir: Path) -> None:
        self.upload_dir = upload_dir
        self.base_path = upload_dir
        self.base_path.mkdir(parents=True, mode=0o755, exist_ok=True)

    async def upload(
        self,
        key: str,
        data: IO[bytes],
        mime: str,
        size_bytes: int,
    ) -> StorageResult:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
        with target.open("wb") as f:
            while True:
                chunk = data.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        return StorageResult(storage_key=key, success=True)

    async def delete(self, key: str) -> StorageResult:
        target = self._resolve(key)
        try:
            target.unlink()
        except FileNotFoundError:
            # 文件不存在视为成功
            return StorageResult(storage_key=key, success=True)
        except OSError as exc:
            return StorageResult(storage_key=key, success=False, error_message=str(exc))
        return StorageResult(storage_key=key, success=True)

    def public_url(self, key: str) -> str:
        """返回相对路径 /api/v1/community/local-uploads/{key}。"""
        return f"/api/v1/community/local-uploads/{key}"

    def _resolve(self, key: str) -> Path:
        """解析并防路径穿越。"""
        # key 格式如 community/2026-08/uuid.jpg
        base = self.base_path.resolve()
        target = (base / key).resolve()
        # 确保最终路径仍在 base_path 下（is_relative_to 可防止同级目录穿越）
        if not target.is_relative_to(base):
            raise ValueError(f"非法 storage_key: {key}")
        return target

    def resolve_file(self, key: str) -> Path:
        """供 local-uploads 路由使用。"""
        return self._resolve(key)
