"""RAG 与 Memory 的仓库级隔离测试：Compose、migration 和环境变量互不复用。"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_rag_compose_uses_independent_service_port_database_and_volume() -> None:
    rag_compose = (_PROJECT_ROOT / "docker-compose.rag.yml").read_text(encoding="utf-8")

    assert "rag-postgres:" in rag_compose
    assert "pgvector/pgvector:pg17" in rag_compose
    assert '"55433:5432"' in rag_compose
    assert "POSTGRES_DB: rag" in rag_compose
    assert "rag-postgres-data:" in rag_compose
    assert "\n  postgres-data:" not in rag_compose


def test_memory_compose_does_not_gain_rag_service() -> None:
    memory_compose = (_PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "rag-postgres" not in memory_compose
    assert '"55432:5432"' in memory_compose
