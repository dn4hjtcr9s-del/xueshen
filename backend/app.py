"""FastAPI 应用入口（规格 §14.8 健康检查；业务路由在后续步骤挂载）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from backend.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="MemoryManagerGraph API", version="0.1.0")
    app.state.settings = settings
    app.state.startup_complete = False

    @app.on_event("startup")
    async def _startup() -> None:
        app.state.startup_complete = True

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/startup")
    async def health_startup(request: Request) -> dict[str, str]:
        if not request.app.state.startup_complete:
            return PlainTextResponse("starting", status_code=503)  # type: ignore[return-value]
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready(request: Request) -> Response:
        """PostgreSQL、存储目录可读写、图谱注册表已加载（§14.8）。

        持久层在步骤 4 接入；当前检查存储目录与生产认证配置。
        """
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
        if settings.app_env == "production" and not settings.production_auth_ready():
            failures.append("production_auth_not_configured")
        if failures:
            return PlainTextResponse(",".join(failures), status_code=503)
        return PlainTextResponse("ok")

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
