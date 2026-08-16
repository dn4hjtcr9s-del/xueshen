"""Study 独立 Alembic 环境：只读取 STUDY_DATABASE_URL 与 study 版本表。

与 Conversation/Community 链同模式：强制专用 URL，拒绝回退到 Memory DATABASE_URL；
未配置 STUDY_DATABASE_URL 时直接 raise（迁移必须显式指定目标库，
应用运行期"缺失即不挂载"由方案 §21/readiness 兜底，与本处行为互不替代）。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import create_engine

from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _study_database_url() -> str:
    url = os.environ.get("STUDY_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "STUDY_DATABASE_URL 未设置；拒绝回退到 Memory DATABASE_URL"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_study_database_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="study_alembic_version",
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_study_database_url(), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=None,
                version_table="study_alembic_version",
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
