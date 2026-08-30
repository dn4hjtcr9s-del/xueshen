"""Community API 包：Router 组装（方案 §8 / §13.1，v1.6 + v3.9 增补）。

build_community_routers(app) 在 FastAPI 运行时装配（对齐 Conversation 模式）：
- 未配置 COMMUNITY_DATABASE_URL → 返回 None，路由不挂载（D25）；
- 配置后创建 CommunityDatabase + 各服务 + CommunityRuntime，
  挂到 app.state.community_db / app.state.community_runtime 供依赖使用；
- 写路径与内部 Reader/purge 路由在 PR-C/PR-D 追加到同一 router。
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from backend.community.api.admin import router as admin_router
from backend.community.api.applications import router as applications_router
from backend.community.api.community import router as community_router
from backend.community.api.dependencies import CommunityRuntime
from backend.community.api.local_uploads import router as local_uploads_router
from backend.community.api.uploads import router as uploads_router
from backend.community.persistence.database import CommunityDatabase
from backend.community.services.attachment_service import AttachmentUploadService
from backend.community.services.board_application_service import BoardApplicationService
from backend.community.services.post_service import PostReadService
from backend.community.storage.factory import get_storage_backend


def build_community_routers(app: FastAPI) -> APIRouter | None:
    """按 app.state.settings 装配 Community Router（未配置社区库时返回 None，D25）。

    读取 app.state.settings 而非全局 get_settings()：create_app 允许注入
    自定义 Settings（测试/运维），装配必须与注入配置一致。
    """
    settings = app.state.settings
    if not settings.community_database_url or not settings.community_v2_enabled:
        return None

    from backend.community.services.post_command_service import PostCommandService
    from backend.community.services.public_user_profile_reader import PublicUserProfileReader
    from backend.community.services.reply_service import ReplyService

    db = CommunityDatabase(settings)
    storage = get_storage_backend(settings)
    service = PostReadService(
        session_factory=db.session_factory, settings=settings, storage=storage
    )

    # 公开资料 adapter 依赖 auth 库 session；auth_runtime 在 startup 构建，
    # 故使用延迟工厂（闭包读取 app.state.auth_runtime，请求到达时已就绪）。
    def _profile_reader() -> PublicUserProfileReader:
        auth_runtime = app.state.auth_runtime
        if auth_runtime is None:
            raise RuntimeError("Auth 运行时尚未初始化，无法读取用户资料")
        return PublicUserProfileReader(auth_runtime.session_factory)

    reply_service = ReplyService(
        session_factory=db.session_factory,
        profile_reader_factory=_profile_reader,
        settings=settings,
    )
    post_command_service = PostCommandService(
        session_factory=db.session_factory,
        profile_reader_factory=_profile_reader,
        reply_service=reply_service,
        settings=settings,
        storage=storage,
    )
    attachment_upload_service = AttachmentUploadService(
        settings=settings, storage=storage, session_factory=db.session_factory
    )
    board_application_service = BoardApplicationService(
        settings=settings, session_factory=db.session_factory
    )

    runtime = CommunityRuntime(
        settings=settings,
        database=db,
        post_service=service,
        post_command_service=post_command_service,
        reply_service=reply_service,
        profile_reader_factory=_profile_reader,
        attachment_upload_service=attachment_upload_service,
        board_application_service=board_application_service,
        storage=storage,
    )
    app.state.community_db = db
    app.state.community_runtime = runtime

    router = APIRouter()
    router.include_router(community_router)
    router.include_router(uploads_router)
    router.include_router(applications_router)
    router.include_router(admin_router)
    if settings.community_storage_backend == "local":
        router.include_router(local_uploads_router)
    return router
