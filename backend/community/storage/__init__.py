"""Community 域对象存储抽象（local / 七牛 Kodo）。

Storage 设计灵感参考 bbs-go v4.4.5 server/ 附件多存储设计（GPL-3.0，仅参考、未复制代码）。
"""

from __future__ import annotations

from backend.community.storage.base import StorageBackend, StorageResult
from backend.community.storage.factory import get_storage_backend

__all__ = ["StorageBackend", "StorageResult", "get_storage_backend"]
