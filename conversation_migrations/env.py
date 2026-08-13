"""Conversation 独立 Alembic 环境：只读取 CONVERSATION_DATABASE_URL 与 conversation 版本表。

与 RAG 链同模式：强制专用 URL，拒绝回退到 Memory DATABASE_URL；
离线模式同样不读取 Memory URL（方案 §2.2 边界 1：库/迁移链/凭证隔离）。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import create_engine

from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _conversation_database_url() -> str:
    url = os.environ.get("CONVERSATION_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "CONVERSATION_DATABASE_URL 未设置；拒绝回退到 Memory DATABASE_URL"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_conversation_database_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="conversation_alembic_version",
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_conversation_database_url(), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=None,
                version_table="conversation_alembic_version",
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
