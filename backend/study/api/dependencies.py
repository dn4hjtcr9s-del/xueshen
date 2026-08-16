"""Study API 运行时依赖：composition root 装配的共享对象（方案 §5.2）。

与 Community 的 CommunityRuntime 同模式：settings + 独立数据库连接池，
Phase 1 起逐步挂接 service/repository；API 路由通过 Depends 工厂注入，
api/ 目录忽略 B008（ruff 约定，与 memory/conversation/community 一致）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.settings import Settings
from backend.study.persistence.database import StudyDatabase


@dataclass
class StudyRuntime:
    """Study 域进程内运行时（app.state.study_runtime，§5.2）。"""

    settings: Settings
    database: StudyDatabase


def get_study_runtime(request: Request) -> StudyRuntime:
    """读取 app.state 上的 Study 运行时（路由只在挂载后可达）。"""
    runtime = getattr(request.app.state, "study_runtime", None)
    if runtime is None:
        raise RuntimeError("Study 运行时未装配，路由不应挂载")
    return cast(StudyRuntime, runtime)


async def get_study_session(
    request: Request,
    runtime: Annotated[StudyRuntime, Depends(get_study_runtime)],
) -> AsyncIterator[AsyncSession]:
    """按请求生命周期提供 Study 数据库会话。"""
    async with runtime.database.session_factory() as session:
        yield session


StudySessionDep = Annotated[AsyncSession, Depends(get_study_session)]
StudyRuntimeDep = Annotated[StudyRuntime, Depends(get_study_runtime)]
