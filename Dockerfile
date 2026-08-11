# MemoryManagerGraph 后端镜像（本地与云端同构，§2.1 / §14.5）
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv

WORKDIR /app

# 先拷贝依赖清单，利用构建缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY backend ./backend
COPY alembic ./alembic
COPY alembic.ini ./
COPY knowledge_graph ./knowledge_graph

ENV KNOWLEDGE_GRAPH_ROOT=/app/knowledge_graph \
    MEMORY_STORAGE_ROOT=/data/memory \
    BACKUP_ROOT=/backups \
    MEMORY_API_HOST=0.0.0.0 \
    MEMORY_API_PORT=8000

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
