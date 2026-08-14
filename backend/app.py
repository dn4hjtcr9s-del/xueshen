"""FastAPI 应用入口（规格 §14.8 健康检查 / §18.5 CORS / §19 API / §22 指标）。

- 业务路由在 backend/memory/api/ 下；启动时装配运行时依赖（模式同
  backend/memory/worker/main.py：settings/Database/service/graph/checkpointer）。
- 错误映射：MemoryError → §7.2 状态码 + §7.3 PublicError（含 trace_id）；
  Pydantic 校验失败 422，extra 字段与其他校验失败按 REQUEST_EXTRA_FIELD /
  INVALID_PAYLOAD 区分。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.auth.verifier import AuthError
from backend.auth_service.api import router as auth_router
from backend.auth_service.errors import AuthServiceError
from backend.auth_service.mapping_consumer import IdentityMappingConsumer
from backend.auth_service.runtime import AuthRuntime, build_auth_runtime
from backend.community.contracts.errors import CommunityError
from backend.conversation.contracts.errors import ConversationError
from backend.memory import metrics
from backend.memory.api import (
    graph_states,
    internal,
    memories,
    notifications,
    operations,
    reviews,
)
from backend.memory.api.dependencies import (
    ApiRuntime,
    FixedWindowRateLimiter,
    trace_id_from_headers,
)
from backend.memory.contracts.errors import MemoryError, PublicError
from backend.memory.logging_config import configure_logging
from backend.memory.maintenance_gate import MaintenanceGate, MaintenanceGateError
from backend.memory.persistence import break_glass as bg_repo
from backend.settings import Settings, get_settings
from backend.shared.auth_context import AuthRuntimeUnavailableError

logger = logging.getLogger("memory.api")


def _effective_origins(settings: Settings) -> list[str]:
    """§18.5：本地开发默认允许 Vite 源（5173 dev / 4173 preview，§9.4）；
    生产只允许显式配置的域名。"""
    if settings.memory_allowed_origins:
        return list(settings.memory_allowed_origins)
    if settings.app_env in ("development", "test"):
        return ["http://localhost:5173", "http://localhost:4173"]
    return []


def _public_error_body(
    code: str,
    message: str,
    *,
    retryable: bool,
    trace_id: str,
    field: str | None = None,
    current_version: int | None = None,
) -> dict[str, object]:
    return {
        "error": PublicError(
            code=code,
            message=message,
            retryable=retryable,
            field=field,
            trace_id=trace_id,
            current_version=current_version,
        ).model_dump(mode="json", exclude_none=True)
    }


async def _build_runtime(settings: Settings, app: FastAPI) -> ApiRuntime:
    """装配 API 进程运行时依赖（§14.2：与 Worker 复用同一 runner/claim 语义）。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from backend.memory.graph.openai_client import RealMemoryLLMClient
    from backend.memory.graph.runner import LocalLangGraphRunner
    from backend.memory.graph.state import (
        MemoryRuntimeContext,
        SystemClock,
        SystemIdGenerator,
        default_registry_factory,
    )
    from backend.memory.persistence.database import Database
    from backend.memory.services.graph_state_service import KnowledgeGraphStateService
    from backend.memory.services.memory_service import MemoryService
    from backend.memory.storage.local_markdown import LocalMarkdownStore
    from backend.memory.worker.checkpoint import CheckpointCleanupAdapter
    from backend.memory.worker.main import (
        _psycopg_conninfo,
        _UnavailableActivityReader,
        _worker_config,
    )
    from backend.memory.worker.worker import Worker

    db = Database(settings)
    maintenance_gate = MaintenanceGate(db.engine)
    stack = AsyncExitStack()
    app.state.db_exit_stack = stack
    store = LocalMarkdownStore(settings.memory_storage_root)
    memory_service = MemoryService(
        settings=settings, session_factory=db.session_factory, store=store
    )
    saver = await stack.enter_async_context(
        AsyncPostgresSaver.from_conn_string(_psycopg_conninfo(settings))
    )
    async with maintenance_gate.traffic():
        await saver.setup()
    conversation_reader = await _conversation_reader_for_runtime(
        settings, stack, db.session_factory
    )
    # Community（§13.1）：HttpActivityReader 同时装配到 app.py 与 memory worker，
    # 必须带 DeletionAwareActivityReader 删除抑制包装（§10.4 第 6 条/§15.4：
    # deletion read suppression = 100%；与 conversation 侧同规则）。
    # 配置不完整（无 base_url/token）时退回 _UnavailableActivityReader。
    activity_reader: Any = _UnavailableActivityReader()
    if settings.community_reader_base_url and settings.community_reader_service_token:
        from backend.integrations.activity_reader import HttpActivityReader
        from backend.memory.readers.filtering import DeletionAwareActivityReader

        http_reader = HttpActivityReader(
            settings.community_reader_base_url,
            token=settings.community_reader_service_token,
        )
        await stack.enter_async_context(http_reader)
        activity_reader = DeletionAwareActivityReader(
            inner=http_reader,
            session_factory=db.session_factory,
        )
    context = MemoryRuntimeContext(
        settings=settings,
        memory_service=memory_service,
        graph_state_service=KnowledgeGraphStateService(
            settings=settings, session_factory=db.session_factory
        ),
        conversation_reader=conversation_reader,
        activity_reader=activity_reader,
        graph_registry_factory=default_registry_factory,
        openai_client=RealMemoryLLMClient(settings=settings),
        session_factory=db.session_factory,
        clock=SystemClock(),
        id_generator=SystemIdGenerator(),
        logger=logger,
        checkpoint_cleanup=CheckpointCleanupAdapter(saver=saver),
    )
    runner = LocalLangGraphRunner(context=context, checkpointer=saver)
    gateway_worker = Worker(
        session_factory=db.session_factory,
        runner=runner,
        config=_worker_config(settings),
        worker_id=f"gateway-{id(app):08x}",
        logger=logger,
        maintenance_gate=maintenance_gate,
    )
    return ApiRuntime(
        settings=settings,
        session_factory=db.session_factory,
        memory_service=memory_service,
        runner=runner,
        gateway_worker=gateway_worker,
        db=db,
        maintenance_gate=maintenance_gate,
    )


async def _conversation_reader_for_runtime(
    settings: Settings,
    stack: AsyncExitStack,
    memory_session_factory: async_sessionmaker[AsyncSession],
) -> Any:
    """装配生产 ConversationReader（方案 §8.5）：真实 Http 实现 + 删除感知包装。

    DeletionAwareConversationReader 需要 Memory 域 source_deletions 表的
    session factory（删除抑制，§8.5：两个 runtime 都必须包装）。
    未配置 READER_BASE_URL/token（内部 transport 未批准或功能关闭）时，
    退回 _UnavailableConversationReader 并维持原语义（§1.3：启用必须等批准）。
    """
    from backend.memory.worker.main import _UnavailableConversationReader

    if not settings.conversation_reader_base_url:
        return _UnavailableConversationReader()
    from backend.integrations.conversation_reader import HttpConversationReader
    from backend.memory.readers.filtering import DeletionAwareConversationReader

    http_reader = HttpConversationReader(
        settings.conversation_reader_base_url,
        token=settings.conversation_reader_service_token,
    )
    await stack.enter_async_context(http_reader)
    return DeletionAwareConversationReader(
        inner=http_reader,
        session_factory=memory_session_factory,
    )


def _issue_community_agent_token(agent_subject: str, delegated_sub: str, scopes: list[str]) -> str:
    """为 Community Publisher 签发短期 delegated activity_agent token（§10.3）。

    actor_type=activity_agent、delegated_sub=事件所属 user_id、
    scope=memory:submit_evidence；token 不返回浏览器、不落库、不写日志。
    """
    from backend.auth_service.agent_tokens import issue_agent_token

    return issue_agent_token(
        agent_subject=agent_subject,
        delegated_sub=delegated_sub,
        actor_type="activity_agent",
        requested_scopes=scopes,
    )


def create_app(
    settings: Settings | None = None,
    *,
    runtime: ApiRuntime | None = None,
    auth_runtime: AuthRuntime | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    app = FastAPI(title="MemoryManagerGraph API", version="0.1.0")
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.auth_runtime = auth_runtime
    app.state.mapping_consumer_task = None
    app.state.auth_migration_head = None
    app.state.rate_limiter = FixedWindowRateLimiter()
    app.state.startup_complete = False
    app.state.migration_head = None
    # D29：shared/auth_context 只依赖 app.state 注入的身份映射工厂与
    # break-glass 存储/校验器（shared 不 import memory persistence 实现）。
    from backend.memory.break_glass import validate_grant_for_use
    from backend.memory.persistence.identity import IdentityMappingRepository

    app.state.identity_resolver_factory = IdentityMappingRepository
    app.state.break_glass_validator = validate_grant_for_use

    class _BreakGlassStore:
        async def get_grant(self, session: AsyncSession, grant_id: UUID) -> Any:
            return await bg_repo.get_grant(session, grant_id)

        async def insert_audit(self, session: AsyncSession, **kwargs: Any) -> None:
            await bg_repo.insert_audit(session, **kwargs)

    app.state.break_glass_store = _BreakGlassStore()
    # Community（§4.2/D25）：未配置 COMMUNITY_DATABASE_URL 时保持 None，
    # 不挂载路由、readiness 跳过；启动时按配置构建独立连接池。
    app.state.community_db = None
    app.state.community_migration_head = None
    if runtime is not None and runtime.maintenance_gate is None and runtime.db is not None:
        runtime.maintenance_gate = MaintenanceGate(runtime.db.engine)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_effective_origins(settings),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-Dev-User-Id",
            "X-Dev-Actor-Type",
            "X-Dev-Scopes",
            "X-Break-Glass-Grant-Id",
            "traceparent",
        ],
    )

    @app.middleware("http")
    async def observability(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.trace_id = trace_id_from_headers(request.headers)
        current: ApiRuntime | None = getattr(request.app.state, "runtime", None)
        gate = current.maintenance_gate if current is not None else None

        async def _serve_request() -> Response:
            response = cast(Response, await call_next(request))
            # §13.15：break-glass 会话的所有正文读取/修改必须写审计（fail-closed，
            # 评审 #10）。审计也属于请求生命周期，必须继续受 maintenance shared
            # lock 保护，避免 call_next 返回后恢复已开始但审计仍访问 public schema。
            break_glass = getattr(request.state, "break_glass", None)
            if (
                break_glass is not None
                and request.url.path.startswith("/api/v1/memory")
                and response.status_code < 400
            ):
                try:
                    await _write_break_glass_body_audit(
                        request,
                        break_glass,
                        action=("read_body" if request.method == "GET" else "modify_body"),
                        resource_type=(
                            getattr(request.scope.get("route"), "path", None) or "unmatched"
                        ),
                    )
                except Exception:
                    logger.error(
                        "break-glass 正文审计写入失败，按 fail-closed 中止响应: %s %s",
                        request.method,
                        request.url.path,
                        exc_info=True,
                    )
                    return JSONResponse(
                        status_code=500,
                        content=_public_error_body(
                            "AUDIT_WRITE_FAILED",
                            "break-glass 审计写入失败，请求结果已按安全策略中止",
                            retryable=True,
                            trace_id=request.state.trace_id,
                        ),
                    )
            return response

        try:
            if gate is None:
                response = await _serve_request()
            else:
                async with gate.traffic():
                    response = await _serve_request()
        except MaintenanceGateError:
            logger.warning("全局维护门禁拒绝 API 请求: %s %s", request.method, request.url.path)
            response = JSONResponse(
                status_code=503,
                content=_public_error_body(
                    "MAINTENANCE_MODE",
                    "系统正在维护，当前请求已按安全策略拒绝",
                    retryable=True,
                    trace_id=request.state.trace_id,
                ),
            )
        route = request.scope.get("route")
        route_path = getattr(route, "path", None) or "unmatched"
        metrics.memory_http_requests_total.labels(
            route=route_path, method=request.method, status=str(response.status_code)
        ).inc()
        if request.url.path.startswith("/api/v1/community"):
            # §12.3：community_api_requests_total（D42：同一 REGISTRY）
            from backend.community import metrics as community_metrics

            community_metrics.community_api_requests_total.labels(
                route=route_path, status=str(response.status_code)
            ).inc()
        return response

    async def _write_break_glass_body_audit(
        request: Request,
        break_glass: dict[str, object],
        *,
        action: str,
        resource_type: str,
    ) -> None:
        """在当前 maintenance traffic lock 生命周期内持久化 break-glass 正文审计。"""
        from uuid import uuid4

        current: ApiRuntime | None = getattr(request.app.state, "runtime", None)
        if current is None:
            raise RuntimeError("API 运行时未初始化，无法写入 break-glass 审计")
        async with current.session_factory() as session:
            await bg_repo.insert_audit(
                session,
                audit_id=uuid4(),
                grant_id=break_glass["grant_id"],  # type: ignore[arg-type]
                admin_user_id=break_glass["admin_user_id"],  # type: ignore[arg-type]
                target_user_id=break_glass["target_user_id"],  # type: ignore[arg-type]
                action=action,
                resource_type=resource_type,
                resource_id=None,
                trace_id=request.state.trace_id,
            )
            await session.commit()

    @app.exception_handler(MemoryError)
    async def memory_error_handler(request: Request, exc: MemoryError) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", None) or ""
        return JSONResponse(
            status_code=exc.http_status,
            content=_public_error_body(
                exc.code,
                exc.message,
                retryable=exc.retryable,
                trace_id=trace_id,
                field=exc.field,
            ),
        )

    @app.exception_handler(ConversationError)
    async def conversation_error_handler(request: Request, exc: ConversationError) -> JSONResponse:
        """Conversation 域错误（附录 A.8）：复用 PublicError 信封。

        THREAD_VERSION_CONFLICT 时在 error 对象内部携带 current_version（附录 A.4）。
        """
        trace_id = getattr(request.state, "trace_id", None) or ""
        current_version = getattr(exc, "current_version", None)
        return JSONResponse(
            status_code=exc.http_status,
            content=_public_error_body(
                exc.code,
                exc.message,
                retryable=exc.retryable,
                trace_id=trace_id,
                field=exc.field,
                current_version=current_version,
            ),
        )

    @app.exception_handler(CommunityError)
    async def community_error_handler(request: Request, exc: CommunityError) -> JSONResponse:
        """Community 域错误（§8.7）：复用 PublicError 信封；429 时带 Retry-After。"""
        trace_id = getattr(request.state, "trace_id", None) or ""
        retry_after = getattr(exc, "retry_after", None)
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        return JSONResponse(
            status_code=exc.http_status,
            content=_public_error_body(
                exc.code,
                exc.message,
                retryable=exc.retryable,
                trace_id=trace_id,
                field=exc.field,
            ),
            headers=headers,
        )

    @app.exception_handler(AuthRuntimeUnavailableError)
    async def auth_runtime_unavailable_handler(
        request: Request, exc: AuthRuntimeUnavailableError
    ) -> JSONResponse:
        """共享认证入口的运行时缺失（D29）：与原 DatabaseUnavailableError 同语义 503。"""
        trace_id = getattr(request.state, "trace_id", None) or ""
        return JSONResponse(
            status_code=503,
            content=_public_error_body(
                "DATABASE_UNAVAILABLE",
                str(exc),
                retryable=True,
                trace_id=trace_id,
            ),
        )

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", None) or ""
        return JSONResponse(
            status_code=403 if exc.forbidden else 401,
            content=_public_error_body(exc.code, str(exc), retryable=False, trace_id=trace_id),
        )

    @app.exception_handler(AuthServiceError)
    async def auth_service_error_handler(request: Request, exc: AuthServiceError) -> JSONResponse:
        """认证服务错误（方案 §4.2）：PublicError 体 + 429 时带 Retry-After。"""
        trace_id = getattr(request.state, "trace_id", None) or ""
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return JSONResponse(
            status_code=exc.http_status,
            content=_public_error_body(
                exc.code,
                exc.message,
                retryable=exc.retryable,
                trace_id=trace_id,
                field=exc.field,
            ),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """422：extra 字段 → REQUEST_EXTRA_FIELD，其余 → INVALID_PAYLOAD（§6.4/§7.3）。"""
        trace_id = getattr(request.state, "trace_id", None) or ""
        errors = exc.errors()
        extra = any(err.get("type") == "extra_forbidden" for err in errors)
        first = errors[0] if errors else {}
        field = ".".join(str(p) for p in first.get("loc", [])) or None
        code = "REQUEST_EXTRA_FIELD" if extra else "INVALID_PAYLOAD"
        message = "请求包含契约外字段" if extra else "请求参数校验失败"
        return JSONResponse(
            status_code=422,
            content=_public_error_body(
                code, message, retryable=False, trace_id=trace_id, field=field
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常: %s", request.url.path)
        trace_id = getattr(request.state, "trace_id", None) or ""
        return JSONResponse(
            status_code=500,
            content=_public_error_body(
                "INTERNAL_ERROR", "内部错误", retryable=False, trace_id=trace_id
            ),
        )

    @app.on_event("startup")
    async def _startup() -> None:
        if app.state.runtime is None:
            app.state.runtime = await _build_runtime(settings, app)
        if app.state.auth_runtime is None:
            # 认证服务与 memory-api 同进程（方案 §2.3）：独立 auth 库连接池 + 签发器；
            # 附带 memory 会话工厂供登录映射兜底与补偿消费任务使用（方案 §3.2）。
            memory_factory = getattr(app.state.runtime, "session_factory", None)
            app.state.auth_runtime = build_auth_runtime(
                settings, memory_session_factory=memory_factory
            )
        # 身份映射补偿消费任务：只在 API 进程运行（附录 A.2 #7）
        if app.state.mapping_consumer_task is None:
            consumer = IdentityMappingConsumer(
                auth_session_factory=app.state.auth_runtime.session_factory,
                memory_session_factory=app.state.runtime.session_factory,
            )
            app.state.mapping_consumer_task = asyncio.create_task(consumer.run_forever())
        try:
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            app.state.migration_head = ScriptDirectory.from_config(
                Config("alembic.ini")
            ).get_current_head()
        except Exception:
            logger.warning("无法解析 alembic head revision", exc_info=True)
            app.state.migration_head = None
        try:
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            app.state.auth_migration_head = ScriptDirectory.from_config(
                Config("auth_alembic.ini")
            ).get_current_head()
        except Exception:
            logger.warning("无法解析 auth alembic head revision", exc_info=True)
            app.state.auth_migration_head = None
        # Conversation 迁移链 head（§17.1：进入 readiness；未装配 Conversation 时跳过）
        try:
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            app.state.conversation_migration_head = ScriptDirectory.from_config(
                Config("conversation_alembic.ini")
            ).get_current_head()
        except Exception:
            logger.warning("无法解析 conversation alembic head revision", exc_info=True)
            app.state.conversation_migration_head = None
        # Community（§13.1/D25）：CommunityDatabase 与路由由
        # build_community_routers 在 create_app 时同步装配（对齐 Conversation）；
        # 此处仅解析迁移链 head 供 readiness 使用。未配置时保持 None。
        if app.state.community_db is not None:
            try:
                from alembic.config import Config
                from alembic.script import ScriptDirectory

                app.state.community_migration_head = ScriptDirectory.from_config(
                    Config("community_alembic.ini")
                ).get_current_head()
            except Exception:
                logger.warning("无法解析 community alembic head revision", exc_info=True)
                app.state.community_migration_head = None
        # Community Publisher 与维护任务（§12.1/§12.4）：lifespan background task。
        # 配置了社区库且 COMMUNITY_PUBLISHER_ENABLED=true 才启动 Publisher；
        # 维护清理任务独立低频运行（间隔 COMMUNITY_MAINTENANCE_INTERVAL_SECONDS）。
        app.state.community_publisher_task = None
        app.state.community_maintenance_task = None
        community_runtime = getattr(app.state, "community_runtime", None)
        if community_runtime is not None:
            if settings.community_publisher_enabled:
                from backend.community.services.activity_publisher import (
                    ActivityPublisher,
                    ActivityPublisherConfig,
                )
                from backend.memory.client import MemoryClient

                source_delete_client = None
                if settings.community_source_delete_service_token:
                    source_delete_client = MemoryClient(
                        settings.memory_api_base_url,
                        token=settings.community_source_delete_service_token,
                        timeout=30.0,
                    )
                publisher = ActivityPublisher(
                    session_factory=community_runtime.database.session_factory,
                    config=ActivityPublisherConfig(settings),
                    source_delete_client=source_delete_client,
                    agent_token_factory=(
                        lambda subj, delegated, scopes: _issue_community_agent_token(
                            subj, delegated, scopes
                        )
                    ),
                    worker_id=f"community-publisher-{id(app):08x}",
                )
                # §10.3：evidence 每次请求按事件 user_id 签发短期 delegated
                # activity_agent token（token_provider 依赖 publisher 实例）
                memory_client = MemoryClient(
                    settings.memory_api_base_url,
                    token_provider=publisher._agent_token,
                    timeout=30.0,
                )
                publisher.set_memory_client(memory_client)
                app.state.community_publisher_task = asyncio.create_task(publisher.run_forever())
            if settings.community_maintenance_interval_seconds > 0:
                from backend.community.services.maintenance import CommunityMaintenance

                maintenance = CommunityMaintenance(
                    session_factory=community_runtime.database.session_factory,
                    interval_seconds=settings.community_maintenance_interval_seconds,
                    settings=settings,
                )
                app.state.community_maintenance_task = asyncio.create_task(
                    maintenance.run_forever()
                )
        app.state.startup_complete = True

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        task: asyncio.Task[None] | None = getattr(app.state, "mapping_consumer_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        publisher_task: asyncio.Task[None] | None = getattr(
            app.state, "community_publisher_task", None
        )
        if publisher_task is not None:
            publisher_task.cancel()
            await asyncio.gather(publisher_task, return_exceptions=True)
        maintenance_task: asyncio.Task[None] | None = getattr(
            app.state, "community_maintenance_task", None
        )
        if maintenance_task is not None:
            maintenance_task.cancel()
            await asyncio.gather(maintenance_task, return_exceptions=True)
        stack: AsyncExitStack | None = getattr(app.state, "db_exit_stack", None)
        if stack is not None:
            await stack.aclose()
        current: ApiRuntime | None = app.state.runtime
        if current is not None and current.db is not None:
            await current.db.close()
        auth_current: AuthRuntime | None = app.state.auth_runtime
        if auth_current is not None:
            await auth_current.database.close()
        community_db = app.state.community_db
        if community_db is not None:
            await community_db.close()

    app.include_router(memories.router)
    app.include_router(reviews.router)
    app.include_router(operations.router)
    app.include_router(graph_states.router)
    app.include_router(notifications.router)
    app.include_router(internal.router)
    # 内部 source-deletions（方案 §1.1 D5 / §8.6）：独立 system principal + memory:source_delete。
    # 修复（评审 P2）：默认关闭（§1.3）——只有配置了 source_delete 服务 token 才挂载；
    # 未获 owner 批准时端点保持关闭（不在 readiness 中启用）。
    current_runtime = app.state.runtime
    if current_runtime is not None and current_runtime.session_factory is not None:
        from backend.memory.api.internal import build_source_deletions_router
        from backend.memory.readers.handler import RecordingSourceDeletionHandler

        deletion_handler = RecordingSourceDeletionHandler(
            session_factory=current_runtime.session_factory
        )
        deletion_router = build_source_deletions_router(
            handler=deletion_handler,
            session_factory=current_runtime.session_factory,
            enabled=bool(settings.conversation_source_delete_service_token),
        )
        if deletion_router is not None:
            app.include_router(deletion_router)
    # Conversation Router（方案 §17.1：挂载到现有 FastAPI App）
    from backend.conversation.api import build_conversation_routers

    conversation_router = build_conversation_routers(app)
    if conversation_router is not None:
        app.include_router(conversation_router)
    # Community Router（§13.1/D25：未配置 COMMUNITY_DATABASE_URL 时不挂载，
    # 含写路径与内部 Reader/purge；readiness 不报错、进程正常启动）
    from backend.community.api import build_community_routers

    community_router = build_community_routers(app)
    if community_router is not None:
        app.include_router(community_router)
        # 内部账号 purge（§8.8/D36）：仅配置了 purge 服务 token 才挂载（fail-closed）。
        # 未配置 community:account_purge token 时端点保持关闭。
        if settings.community_account_purge_service_token:
            from backend.community.api.internal_accounts import router as purge_router

            app.include_router(purge_router)
        # 内部 Source Reader（§10.4）：仅配置了 reader 服务 token 才挂载（fail-closed）。
        # community:source_read scope 加入 ALL_SCOPES 但不授予普通用户（§13.3）。
        if settings.community_reader_service_token:
            from backend.community.api.internal_sources import (
                build_reader_router as build_community_reader_router,
            )
            from backend.community.services.source_read_service import (
                CommunitySourceReadService,
            )

            community_reader_service = CommunitySourceReadService(
                session_factory=app.state.community_runtime.database.session_factory
            )
            app.include_router(build_community_reader_router(community_reader_service))
    # 内部 Reader 端点（方案 §8.2，P0 闭环）：Memory 侧 HttpConversationReader 的
    # POST /api/v1/internal/conversation-sources/read 目标。
    # 依赖 conversation DB；未启用 Conversation 时跳过（与 Router 同门控）。
    reader_db = getattr(app.state, "conversation_db", None)
    if reader_db is not None:
        from backend.conversation.api.internal_sources import build_reader_router
        from backend.conversation.services.source_read_service import (
            ConversationSourceReadService,
        )

        reader_service = ConversationSourceReadService(session_factory=reader_db.session_factory)
        app.include_router(build_reader_router(reader_service))
    # 认证服务（方案 §2.3：内嵌同进程，将来可平移为独立服务）
    app.include_router(auth_router, prefix="/api/v1/auth")

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/startup")
    async def health_startup(request: Request) -> Response:
        if not request.app.state.startup_complete:
            return PlainTextResponse("starting", status_code=503)
        return PlainTextResponse("ok")

    @app.get("/health/ready")
    async def health_ready(request: Request) -> Response:
        """PostgreSQL、迁移版本、存储目录可读写、图谱注册表已加载（§14.8）。"""
        failures: list[str] = []

        def _probe_storage() -> bool:
            storage_root = Path(settings.memory_storage_root)
            try:
                storage_root.mkdir(parents=True, exist_ok=True)
                probe = storage_root / ".ready_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError:
                return False
            return True

        if not await asyncio.to_thread(_probe_storage):
            failures.append("storage_not_writable")

        current: ApiRuntime | None = request.app.state.runtime
        if current is None or current.db is None:
            failures.append("database_unavailable")
        else:
            if not await current.db.ping():
                failures.append("database_unavailable")
            else:
                # §6.3 / 评审 P1-5：迁移检查 fail-closed —— head 不可解析、
                # 版本表缺失、revision 不等于 head 均视为未就绪（503）
                head = getattr(request.app.state, "migration_head", None)
                if head is None:
                    failures.append("migration_head_unresolved")
                else:
                    try:
                        async with current.db.session_factory() as session:
                            row = await session.execute(
                                text("SELECT version_num FROM alembic_version")
                            )
                            current_version = row.scalar_one_or_none()
                    except Exception:
                        logger.warning("memory alembic_version 查询失败", exc_info=True)
                        current_version = None
                    if current_version != head:
                        failures.append("migration_version_mismatch")
                try:
                    async with current.db.session_factory() as session:
                        count = await session.execute(
                            text("SELECT COUNT(*) FROM knowledge_graph_nodes")
                        )
                        graph_count = int(count.scalar_one())
                except Exception:
                    logger.warning("knowledge_graph_nodes 查询失败", exc_info=True)
                    graph_count = 0
                if graph_count == 0:
                    failures.append("knowledge_graph_registry_not_loaded")

        if settings.app_env == "production" and not settings.production_auth_ready():
            failures.append("production_auth_not_configured")
        # Conversation 迁移链检查（方案 §17.1：Conversation migration 进入 readiness；
        # 未启用 Conversation 时跳过）
        conversation_ctx = getattr(request.app.state, "conversation_api_context", None)
        if conversation_ctx is not None:
            conversation_db = getattr(request.app.state, "conversation_db", None)
            if conversation_db is None:
                failures.append("conversation_database_unavailable")
            else:
                if not await conversation_db.ping():
                    failures.append("conversation_database_unavailable")
                else:
                    conversation_head = getattr(
                        request.app.state, "conversation_migration_head", None
                    )
                    if conversation_head is None:
                        failures.append("conversation_migration_head_unresolved")
                    else:
                        try:
                            async with conversation_db.session_factory() as session:
                                row = await session.execute(
                                    text("SELECT version_num FROM conversation_alembic_version")
                                )
                                conversation_version = row.scalar_one_or_none()
                        except Exception:
                            logger.warning("conversation_alembic_version 查询失败", exc_info=True)
                            conversation_version = None
                        if conversation_version != conversation_head:
                            failures.append("conversation_migration_version_mismatch")
            # §20.4 / 评审 P1-9：配置开启但依赖未就绪必须失败。
            # Reader/Source-deletion 内部 transport 启用时：token 缺失 → 失败；
            # Embedding：模型/维度未配置 → 失败（D15 启动强校验）。
            if settings.conversation_memory_submit_enabled:
                if not settings.conversation_reader_base_url:
                    failures.append("conversation_reader_not_configured")
                elif not settings.conversation_reader_service_token:
                    failures.append("conversation_reader_token_missing")
                if not settings.memory_api_base_url or not settings.memory_agent_token:
                    failures.append("memory_api_not_configured")
            if (
                settings.conversation_agentic_rag_enabled
                or settings.conversation_memory_read_enabled
            ):
                if not settings.embedding_model:
                    failures.append("embedding_model_not_configured")
                if not settings.embedding_base_url:
                    failures.append("embedding_base_url_not_configured")
                if settings.rag_embedding_dimensions <= 0:
                    failures.append("embedding_dimensions_invalid")
            # §19.1（评审 P1-9）：角色模型能力矩阵——agentic RAG 启用时全部角色必须配置
            if settings.conversation_agentic_rag_enabled:
                for role_field in (
                    "openai_rewrite_model",
                    "openai_evidence_model",
                    "openai_answer_model",
                ):
                    if not getattr(settings, role_field, ""):
                        failures.append(f"{role_field}_not_configured")
            if settings.conversation_streaming_enabled and not settings.openai_answer_model:
                failures.append("openai_answer_model_not_configured")
        # Community 迁移链检查（§13.1/D25）：未配置 COMMUNITY_DATABASE_URL 时
        # community_db 为 None，跳过检查（本地无社区库不影响其余域）；已配置但
        # ping 失败或迁移版本不一致 → fail-closed。
        community_db = getattr(request.app.state, "community_db", None)
        if community_db is not None:
            if not await community_db.ping():
                failures.append("community_database_unavailable")
            else:
                community_head = getattr(request.app.state, "community_migration_head", None)
                if community_head is None:
                    failures.append("community_migration_head_unresolved")
                else:
                    try:
                        async with community_db.session_factory() as session:
                            row = await session.execute(
                                text("SELECT version_num FROM community_alembic_version")
                            )
                            community_version = row.scalar_one_or_none()
                    except Exception:
                        logger.warning("community_alembic_version 查询失败", exc_info=True)
                        community_version = None
                    if community_version != community_head:
                        failures.append("community_migration_version_mismatch")
            # §13.2/D46：evidence/删除链路开启时 Reader base URL 与 token 必须齐备
            # §13.2/D46：submit 与 deletion 链路各自校验依赖（token 与 bool 分离；
            # deletion 消耗的是 source_delete token，非 reader token）
            if settings.community_memory_submit_enabled:
                if not settings.community_reader_base_url:
                    failures.append("community_reader_not_configured")
                if not settings.community_reader_service_token:
                    failures.append("community_reader_token_missing")
            if settings.community_source_deletion_enabled:
                if not settings.community_source_delete_service_token:
                    failures.append("community_source_delete_token_missing")
                if not settings.memory_api_base_url:
                    failures.append("memory_api_not_configured")
        # §6.3：readiness 同时检查 memory 与 auth 两条迁移链（fail-closed）
        auth_current: AuthRuntime | None = getattr(request.app.state, "auth_runtime", None)
        if auth_current is None:
            failures.append("auth_database_unavailable")
        elif not await auth_current.database.ping():
            failures.append("auth_database_unavailable")
        else:
            auth_head = getattr(request.app.state, "auth_migration_head", None)
            if auth_head is None:
                failures.append("auth_migration_head_unresolved")
            else:
                try:
                    async with auth_current.session_factory() as session:
                        row = await session.execute(
                            text("SELECT version_num FROM auth_alembic_version")
                        )
                        auth_version = row.scalar_one_or_none()
                except Exception:
                    logger.warning("auth alembic_version 查询失败", exc_info=True)
                    auth_version = None
                if auth_version != auth_head:
                    failures.append("auth_migration_version_mismatch")
        if failures:
            return JSONResponse(
                status_code=503, content={"status": "not_ready", "failures": failures}
            )
        return JSONResponse(content={"status": "ok"})

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
