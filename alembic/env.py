"""Alembic 环境：从应用 settings 读取 DATABASE_URL。"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from backend.settings import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = config.attributes.get("database_url")
if database_url is None:
    database_url = get_settings().database_url
config.set_main_option("sqlalchemy.url", str(database_url))

# 第一版以 op.execute 手写 DDL 为准（规格 §13），不使用 ORM autogenerate 目标
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    context.configure(connection=connection)  # type: ignore[arg-type]
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
