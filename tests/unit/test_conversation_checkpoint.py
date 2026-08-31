"""Conversation 检查点连接池测试。

验证 worker 使用可检测失效连接的异步连接池，而不是只绑定一条不可恢复的长连接。
"""

from __future__ import annotations

from typing import cast

import pytest
from psycopg.rows import dict_row

from backend.conversation.persistence import checkpoint


@pytest.mark.asyncio
async def test_open_checkpoint_saver_uses_reconnecting_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据库重启后连接池配置应允许检测并替换失效连接。"""

    class FakePool:
        def __init__(self, conninfo: str, **kwargs: object) -> None:
            self.created = {"conninfo": conninfo, **kwargs}

        async def __aenter__(self) -> FakePool:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def wait(self) -> None:
            return None

        @staticmethod
        async def check_connection(_connection: object) -> None:
            return None

    class FakeSaver:
        def __init__(self, pool: FakePool) -> None:
            self.pool = pool
            self.setup_called = False

        async def setup(self) -> None:
            self.setup_called = True

    monkeypatch.setattr(checkpoint, "AsyncConnectionPool", FakePool)
    monkeypatch.setattr(checkpoint, "AsyncPostgresSaver", FakeSaver)

    async with checkpoint.open_checkpoint_saver(
        "postgresql://conversation@postgres/conversation"
    ) as raw_saver:
        saver = cast(FakeSaver, raw_saver)  # monkeypatch 后运行时为 FakeSaver
        pool = saver.pool
        assert saver.setup_called is True

    assert pool.created["min_size"] == 1
    assert pool.created["max_size"] == 4
    assert pool.created["open"] is False
    assert pool.created["check"] is FakePool.check_connection
    assert pool.created["kwargs"] == {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
    }
