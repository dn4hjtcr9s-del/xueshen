"""Community 域对象存储抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import IO


@dataclass(frozen=True)
class StorageResult:
    """存储操作结果。"""

    storage_key: str
    success: bool
    error_message: str | None = None
    #: HTTP 状态码（Kodo 响应）；None = 无结构化状态（超时/连接/未知异常）。
    #: 用于上传失败的可重试分类（§7.9/§7.11）。
    status_code: int | None = None


class StorageBackend(ABC):
    """社区图片存储后端抽象。"""

    @abstractmethod
    async def upload(
        self,
        key: str,
        data: IO[bytes],
        mime: str,
        size_bytes: int,
    ) -> StorageResult:
        """上传对象；data 已定位到开头。"""

    @abstractmethod
    async def delete(self, key: str) -> StorageResult:
        """删除对象；对象不存在视为成功（612 / 文件不存在）。"""

    @abstractmethod
    def public_url(self, key: str) -> str:
        """返回客户端可访问的 URL（kodo=绝对 URL，local=相对路径）。"""
