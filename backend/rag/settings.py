"""RAG 独立运行配置：只接受 RAG_* 环境变量，不复用 Memory 配置。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RAGSettings(BaseSettings):
    """RAG 数据库、artifact 导入和检索参数。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(..., alias="RAG_DATABASE_URL")
    database_schema: Literal["rag"] = Field(default="rag", alias="RAG_DATABASE_SCHEMA")
    statement_timeout_ms: int = Field(default=150_000, alias="RAG_STATEMENT_TIMEOUT_MS")
    lock_timeout_ms: int = Field(default=10_000, alias="RAG_LOCK_TIMEOUT_MS")
    embedding_model: Literal["text-embedding-v4"] = Field(
        default="text-embedding-v4", alias="RAG_EMBEDDING_MODEL"
    )
    embedding_dimensions: Literal[1024] = Field(default=1024, alias="RAG_EMBEDDING_DIMENSIONS")
    lexical_pipeline_version: Literal["zh-bigram-formula/v1"] = Field(
        default="zh-bigram-formula/v1", alias="RAG_LEXICAL_PIPELINE_VERSION"
    )
    import_batch_size: int = Field(default=100, alias="RAG_IMPORT_BATCH_SIZE")
    hnsw_ef_search: int = Field(default=100, alias="RAG_HNSW_EF_SEARCH")
    rrf_k: int = Field(default=60, alias="RAG_RRF_K")


@lru_cache
def get_rag_settings() -> RAGSettings:
    """返回进程级缓存配置；测试可直接实例化 RAGSettings(_env_file=None)。"""
    # RAG_DATABASE_URL 由 BaseSettings 在运行时读取，mypy 无法识别环境变量别名注入。
    return RAGSettings()  # type: ignore[call-arg]
