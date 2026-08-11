"""RAG 独立 Alembic 环境：只读取 RAG_DATABASE_URL 和 rag 版本表。"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import create_engine, text

from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _rag_database_url() -> str:
    url = os.environ.get("RAG_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("RAG_DATABASE_URL 未设置；拒绝回退到 Memory DATABASE_URL")
    return url


def run_migrations_offline() -> None:
    """生成 RAG SQL；offline 模式仍不读取 Memory URL。"""
    context.configure(
        url=_rag_database_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="rag_alembic_version",
        version_table_schema="rag",
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """连接独立 RAG 数据库并执行 migration。"""
    engine = create_engine(_rag_database_url(), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("CREATE SCHEMA IF NOT EXISTS rag"))
            connection.commit()
            context.configure(
                connection=connection,
                target_metadata=None,
                version_table="rag_alembic_version",
                version_table_schema="rag",
                include_schemas=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
