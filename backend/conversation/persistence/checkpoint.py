"""Conversation LangGraph 检查点连接管理。

生产进程使用可自愈的 psycopg 异步连接池，而不是绑定单条长连接。这样 PostgreSQL
容器重启或网络短暂中断后，连接池会丢弃失效连接并重新建立连接，避免 worker 持续以
``the connection is closed`` 失败直到人工重启。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


@asynccontextmanager
async def open_checkpoint_saver(conninfo: str) -> AsyncIterator[AsyncPostgresSaver]:
    """打开具备断线恢复能力的 LangGraph PostgreSQL saver。"""

    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        conninfo,
        min_size=1,
        max_size=4,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        # 每次借出连接前做轻量存活检查；数据库重启后的旧连接会被自动替换。
        check=AsyncConnectionPool.check_connection,
        name="conversation-checkpoints",
    )
    async with pool:
        await pool.wait()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        yield saver
