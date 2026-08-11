"""Graph State 与 Runtime Context（§10.1 / §10.2）。

State 只保存可序列化数据；OpenAI Client、数据库连接、Reader/Service 实例、
密钥和大型原始对话全文一律通过 Runtime Context 注入，不进入 State。
source_bundle 在进入 Checkpoint 前裁剪到最多 80 KB（由 SourceBundle 契约保证）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypedDict
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.graph.openai_client import MemoryLLMClient
from backend.memory.knowledge_graph.registry import KnowledgeGraphRegistry
from backend.memory.readers.base import ActivityReader, ConversationReader
from backend.memory.services.graph_state_service import KnowledgeGraphStateService
from backend.memory.services.memory_service import MemoryService
from backend.memory.worker.checkpoint import CheckpointCleanupAdapter
from backend.settings import Settings


class MemoryManagerState(TypedDict, total=False):
    """§10.1 原文（dict 具体化为 dict[str, Any] 以满足 mypy strict）。"""

    operation: dict[str, Any]
    route: str
    source_bundle: dict[str, Any]
    candidates: list[dict[str, Any]]
    candidate_graph_nodes: dict[str, list[dict[str, Any]]]
    existing_memories: list[dict[str, Any]]
    mutation_plan_drafts: list[dict[str, Any]]
    commit_mutation_plans: list[dict[str, Any]]
    commit_result: dict[str, Any]
    graph_state_result: dict[str, Any]
    review_candidates: list[dict[str, Any]]
    warnings: list[str]
    errors: list[dict[str, Any]]
    llm_call_count: int
    replan_count: int


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        from datetime import UTC

        return datetime.now(UTC)


class IdGenerator(Protocol):
    def new_uuid(self) -> UUID: ...


class SystemIdGenerator:
    def new_uuid(self) -> UUID:
        from uuid import uuid4

        return uuid4()


@dataclass(frozen=True)
class MemoryRuntimeContext:
    """§10.2 依赖注入容器。

    与规格的两处已报备差异：
    - openai_client 为 MemoryLLMClient 边界（Real 内部包装 AsyncOpenAI），
      使节点可用 fake Runtime Context 测试（§23.2）。
    - context_service（LearningContextService）属步骤 12，届时补充字段。

    checkpoint_cleanup 为步骤 10 接入的 Checkpoint 清理适配器（§11.4），
    仅 cleanup_checkpoints 维护分支使用；未配置时该分支明确报错而非空转。
    """

    settings: Settings
    memory_service: MemoryService
    graph_state_service: KnowledgeGraphStateService
    conversation_reader: ConversationReader
    activity_reader: ActivityReader
    graph_registry_factory: RegistryFactory
    openai_client: MemoryLLMClient
    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    id_generator: IdGenerator
    logger: logging.Logger
    checkpoint_cleanup: CheckpointCleanupAdapter | None = None


class RegistryFactory(Protocol):
    """按会话构造只读图谱注册表（注册表绑定 Session，不跨事务持有）。"""

    def __call__(self, session: AsyncSession) -> KnowledgeGraphRegistry: ...


def default_registry_factory(session: AsyncSession) -> KnowledgeGraphRegistry:
    return KnowledgeGraphRegistry(session)
