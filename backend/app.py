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
from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from backend.auth.verifier import AuthError
from backend.auth_service.api import router as auth_router
from backend.auth_service.errors import AuthServiceError
from backend.auth_service.mapping_consumer import IdentityMappingConsumer
from backend.auth_service.runtime import AuthRuntime, build_auth_runtime
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

logger = logging.getLogger("memory.api")


def _effective_origins(settings: Settings) -> list[str]:
    """§18.5：本地开发默认允许 Vite 源；生产只允许显式配置的域名。"""
    if settings.memory_allowed_origins:
        return list(settings.memory_allowed_origins)
    if settings.app_env in ("development", "test"):
        return ["http://localhost:5173"]
    return []


def _public_error_body(
    code: str, message: str, *, retryable: bool, trace_id: str, field: str | None = None
) -> dict[str, object]:
    return {
        "error": PublicError(
            code=code, message=message, retryable=retryable, field=field, trace_id=trace_id
        ).model_dump(mode="json")
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
        _UnavailableConversationReader,
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
    context = MemoryRuntimeContext(
        settings=settings,
        memory_service=memory_service,
        graph_state_service=KnowledgeGraphStateService(
            settings=settings, session_factory=db.session_factory
        ),
        conversation_reader=_UnavailableConversationReader(),
        activity_reader=_UnavailableActivityReader(),
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
            app.state.auth_migration_head = ScriptDirectory.from_config(
                Config("auth_alembic.ini")
            ).get_current_head()
        except Exception:
            logger.warning("无法解析 alembic head revision", exc_info=True)
        app.state.startup_complete = True

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        task: asyncio.Task[None] | None = getattr(app.state, "mapping_consumer_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        stack: AsyncExitStack | None = getattr(app.state, "db_exit_stack", None)
        if stack is not None:
            await stack.aclose()
        current: ApiRuntime | None = app.state.runtime
        if current is not None and current.db is not None:
            await current.db.close()
        auth_current: AuthRuntime | None = app.state.auth_runtime
        if auth_current is not None:
            await auth_current.database.close()

    app.include_router(memories.router)
    app.include_router(reviews.router)
    app.include_router(operations.router)
    app.include_router(graph_states.router)
    app.include_router(notifications.router)
    app.include_router(internal.router)
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
                async with current.db.session_factory() as session:
                    head = request.app.state.migration_head
                    if head is not None:
                        row = await session.execute(text("SELECT version_num FROM alembic_version"))
                        current_version = row.scalar_one_or_none()
                        if current_version != head:
                            failures.append("migration_version_mismatch")
                    count = await session.execute(
                        text("SELECT COUNT(*) FROM knowledge_graph_nodes")
                    )
                    if int(count.scalar_one()) == 0:
                        failures.append("knowledge_graph_registry_not_loaded")

        if settings.app_env == "production" and not settings.production_auth_ready():
            failures.append("production_auth_not_configured")
        # §6.3：readiness 同时检查 memory 与 auth 两条迁移链
        auth_current: AuthRuntime | None = getattr(request.app.state, "auth_runtime", None)
        if auth_current is None:
            failures.append("auth_database_unavailable")
        elif not await auth_current.database.ping():
            failures.append("auth_database_unavailable")
        else:
            auth_head = getattr(request.app.state, "auth_migration_head", None)
            if auth_head is not None:
                async with auth_current.session_factory() as session:
                    row = await session.execute(
                        text("SELECT version_num FROM auth_alembic_version")
                    )
                    auth_version = row.scalar_one_or_none()
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
