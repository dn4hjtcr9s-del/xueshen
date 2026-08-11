"""Worker/Scheduler/Consumer 单元测试共享 fake：内存会话工厂与调用记录。

仓储函数一律由测试 monkeypatch，fake session 只提供 async 上下文管理器协议。
"""

from __future__ import annotations

from typing import Any, Self


class FakeSession:
    """空会话：execute 不应被调用（仓储已 monkeypatch）；被调用即报错。"""

    def begin(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def execute(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("FakeSession.execute 不应被调用：请 monkeypatch 仓储函数")


class FakeSessionFactory:
    """模仿 async_sessionmaker：同步调用返回异步上下文管理器会话。"""

    def __call__(self) -> FakeSession:
        return FakeSession()


def make_session_factory() -> Any:
    return FakeSessionFactory()
