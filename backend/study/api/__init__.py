"""Study API 包：Router 组装（方案 §12，v1.2）。

build_study_routers(app) 在 FastAPI 运行时装配（对齐 Conversation/Community 模式）：
- STUDY_DOMAIN_ENABLED=false 或未配置 STUDY_DATABASE_URL → 返回 None，路由不挂载（§21）；
- 启用并配置后创建 StudyDatabase + StudyRuntime，挂到
  app.state.study_db / app.state.study_runtime 供依赖使用；
- 内部 purge 路由由 app.py 仅在配置 STUDY_ACCOUNT_PURGE_SERVICE_TOKEN 时挂载（D19）。
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from backend.study.api import home, intakes, operations, plans, sessions, tasks
from backend.study.api.dependencies import StudyRuntime
from backend.study.persistence.database import StudyDatabase


def build_study_routers(app: FastAPI) -> APIRouter | None:
    """按 app.state.settings 装配 Study Router（未启用/未配置库时返回 None，§21）。

    读取 app.state.settings 而非全局 get_settings()：create_app 允许注入
    自定义 Settings（测试/运维），装配必须与注入配置一致。
    """
    settings = app.state.settings
    if not settings.study_domain_enabled:
        return None
    if not settings.study_database_url:
        # §21：域开启但未配置库 → 不挂载路由，readiness 以
        # study_database_not_configured fail-closed（语义与 Community D25 同源）。
        return None

    db = StudyDatabase(settings)
    runtime = StudyRuntime(settings=settings, database=db)
    app.state.study_db = db
    app.state.study_runtime = runtime
    router = APIRouter(prefix="/api/v1/study", tags=["study"])
    router.include_router(intakes.router)
    router.include_router(plans.router)
    router.include_router(tasks.router)
    router.include_router(sessions.router)
    router.include_router(home.router)
    router.include_router(operations.router)
    return router
