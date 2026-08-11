"""RAG 数据库连接工厂：连接独立 URL，并在连接上设置 RAG 会话参数。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import Engine

from backend.rag.settings import RAGSettings, get_rag_settings


def create_rag_engine(settings: RAGSettings | None = None) -> Engine:
    """创建 RAG 专用 SQLAlchemy engine，不读取 backend.Settings。"""
    config = settings or get_rag_settings()
    return create_engine(
        config.database_url,
        pool_pre_ping=True,
        future=True,
        connect_args={
            "options": (
                f"-c statement_timeout={config.statement_timeout_ms} "
                f"-c lock_timeout={config.lock_timeout_ms}"
            )
        },
    )


@contextmanager
def rag_connection(settings: RAGSettings | None = None) -> Iterator[Connection]:
    """以事务连接形式访问 RAG 数据库。"""
    engine = create_rag_engine(settings)
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL search_path TO rag, public"))
            yield connection
    finally:
        engine.dispose()
