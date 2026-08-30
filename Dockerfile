# MemoryManagerGraph 后端镜像（本地与云端同构，§2.1 / §14.5）
FROM python:3.13.15-slim AS base

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv

WORKDIR /app

# 放宽 uv 下载超时（生产 1Mbps 带宽下默认 30s 不足，P5 实测）。
# 注：uv.lock 的 registry 已固定为阿里云 PyPI 镜像（--frozen 严格按锁文件源下载，
# UV_INDEX_URL 对锁定 registry 无效，P5 实测），无需再注入镜像地址。
ENV UV_HTTP_TIMEOUT=300

# 构建期正向代理（P5：CDN 对 python/uv 客户端指纹识别限速，宿主机 squid 满速）。
# 仅声明 ARG 供 RUN 环境使用（docker 预定义代理参数需显式 ARG 才进 RUN 环境，
# P5 实测未声明时 squid access.log 零命中），不写入镜像层、不影响缓存键。
ARG HTTP_PROXY
ARG HTTPS_PROXY

# 先拷贝依赖清单，利用构建缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY backend ./backend
COPY alembic ./alembic
COPY alembic.ini ./
# 认证服务独立迁移链（方案 §5.1 / 评审 P1-5：readiness 需解析 auth head）
COPY auth_alembic.ini ./
COPY auth_migrations ./auth_migrations
# Conversation 与 Community 使用独立迁移链；readiness 需要在镜像内解析各自 head。
COPY conversation_alembic.ini ./
COPY conversation_migrations ./conversation_migrations
COPY community_alembic.ini ./
COPY community_migrations ./community_migrations
COPY knowledge_graph ./knowledge_graph

ENV KNOWLEDGE_GRAPH_ROOT=/app/knowledge_graph \
    MEMORY_STORAGE_ROOT=/data/memory \
    BACKUP_ROOT=/backups \
    MEMORY_API_HOST=0.0.0.0 \
    MEMORY_API_PORT=8000

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
