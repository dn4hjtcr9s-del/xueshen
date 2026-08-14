"""Community API 包：Router 组装（方案 §8 / §13.1，v1.6）。

build_community_routers(app) 在 FastAPI 运行时装配（对齐 Conversation 模式）：
- 未配置 COMMUNITY_DATABASE_URL → 返回 None，路由不挂载（D25）；
- 配置后创建 CommunityDatabase + PostReadService + CommunityRuntime，
  挂到 app.state.community_db / app.state.community_runtime 供依赖使用；
- 写路径与内部 Reader/purge 路由在 PR-C/PR-D 追加到同一 router。
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from backend.community.api.community import router as community_router
from backend.community.api.dependencies import CommunityRuntime
from backend.community.persistence.database import CommunityDatabase
from backend.community.services.post_service import PostReadService


def build_community_routers(app: FastAPI) -> APIRouter | None:
    """按 app.state.settings 装配 Community Router（未配置社区库时返回 None，D25）。

    读取 app.state.settings 而非全局 get_settings()：create_app 允许注入
    自定义 Settings（测试/运维），装配必须与注入配置一致。
    """
    settings = app.state.settings
    if not settings.community_database_url:
        return None

    from backend.community.services.post_command_service import PostCommandService
    from backend.community.services.public_user_profile_reader import PublicUserProfileReader
    from backend.community.services.reply_service import ReplyService

    db = CommunityDatabase(settings)
    service = PostReadService(session_factory=db.session_factory)

    # 公开资料 adapter 依赖 auth 库 session；auth_runtime 在 startup 构建，
    # 故使用延迟工厂（闭包读取 app.state.auth_runtime，请求到达时已就绪）。
    def _profile_reader() -> PublicUserProfileReader:
        auth_runtime = app.state.auth_runtime
        if auth_runtime is None:
            raise RuntimeError("Auth 运行时尚未初始化，无法读取用户资料")
        return PublicUserProfileReader(auth_runtime.session_factory)

    # 延迟工厂：auth_runtime 在 startup 构建，请求到达时已就绪
    reply_service = ReplyService(
        session_factory=db.session_factory, profile_reader_factory=_profile_reader
    )
    post_command_service = PostCommandService(
        session_factory=db.session_factory,
        profile_reader_factory=_profile_reader,
        reply_service=reply_service,
    )
    runtime = CommunityRuntime(
        settings=settings,
        database=db,
        post_service=service,
        post_command_service=post_command_service,
        reply_service=reply_service,
        profile_reader_factory=_profile_reader,
    )
    app.state.community_db = db
    app.state.community_runtime = runtime
    router = APIRouter()
    router.include_router(community_router)
    return router
