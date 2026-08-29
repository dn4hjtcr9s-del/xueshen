"""Storage 后端工厂。"""

from __future__ import annotations

from pathlib import Path

from backend.community.storage.base import StorageBackend
from backend.community.storage.kodo import KodoStorage
from backend.community.storage.local import LocalStorage
from backend.settings import Settings


class StorageFactory:
    """按 settings 创建并缓存 StorageBackend 实例。"""

    _instance: StorageBackend | None = None

    @classmethod
    def get_backend(cls, settings: Settings) -> StorageBackend:
        if cls._instance is None:
            if settings.community_storage_backend == "kodo":
                cls._instance = KodoStorage(settings)
            else:
                cls._instance = LocalStorage(Path(settings.community_local_upload_dir))
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None


def get_storage_backend(settings: Settings) -> StorageBackend:
    return StorageFactory.get_backend(settings)
