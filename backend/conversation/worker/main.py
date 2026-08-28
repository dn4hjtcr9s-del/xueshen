"""conversation-worker 入口（附录 A.9：uv run python -m backend.conversation.worker.main）。

装配：ConversationDatabase、OpenAIGateway、MemoryGateway、QueryEmbeddingGateway、
AsyncRetrieverAdapter、ContextService、ConversationGraph、LangGraph PostgreSQL
checkpointer（独立 schema conversation_checkpoints）、GraphWorker、JobWorker。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID, uuid4

from backend.conversation.graph.state import SystemClock, SystemIdGenerator
from backend.conversation.persistence.database import ConversationDatabase
from backend.conversation.worker.graph_worker import (
    ConversationGraphWorker,
    GraphWorkerConfig,
)
from backend.conversation.worker.job_worker import JobWorker
from backend.settings import get_settings


def graph_thread_id_for_turn(turn_id: UUID) -> str:
    """附录 A.3：graph_thread_id 从 turn_id 确定性派生。"""
    return f"conv-turn:{turn_id}"


def _psycopg_conninfo(settings: Any) -> str:
    """SQLAlchemy URL → psycopg conninfo，并把 checkpointer 指向独立 schema（§7）。"""
    url = settings.conversation_database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    separator = "&" if "?" in url else "?"
    return (
        f"{url}{separator}options=-csearch_path%3D{settings.conversation_graph_checkpoint_schema}"
    )


async def _run() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logger = logging.getLogger("conversation.worker")
    db = ConversationDatabase(settings)
    try:
        # Gateways
        from backend.conversation.gateways.embedding import QueryEmbeddingGateway
        from backend.conversation.gateways.memory import MemoryGateway
        from backend.conversation.gateways.openai import OpenAIGateway
        from backend.conversation.gateways.retriever import AsyncRetrieverAdapter
        from backend.memory.client import MemoryClient
        from backend.rag.retrieval import RetrievalService

        openai_gateway = OpenAIGateway(settings=settings, logger=logger)
        memory_client = MemoryClient(
            settings.memory_api_base_url,
            token=settings.memory_agent_token,
            timeout=max(settings.memory_context_timeout_seconds, 10.0),
        )
        memory_gateway = MemoryGateway(client=memory_client, logger=logger)
        embedding_gateway = QueryEmbeddingGateway(settings=settings, logger=logger)
        retriever_gateway = AsyncRetrieverAdapter(
            retrieval_service=RetrievalService(settings=None),
            concurrency=settings.conversation_retrieval_concurrency,
            worker_timeout_seconds=settings.conversation_retrieval_worker_timeout_seconds,
            logger=logger,
        )

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        from backend.conversation.graph.builder import build_conversation_graph
        from backend.conversation.graph.runner import ConversationGraphRunner
        from backend.conversation.graph.state import ConversationRuntimeContext
        from backend.conversation.persistence.event_writer import TurnEventWriter
        from backend.conversation.persistence.repository import ConversationRepository
        from backend.conversation.services.context_service import ContextService
        from backend.conversation.services.token_counter import TokenCounter

        token_counter = TokenCounter()
        context_service = ContextService(settings=settings, token_counter=token_counter)
        repository = ConversationRepository(session_factory=db.session_factory)
        worker_id = f"conversation-worker-{uuid4()}"
        runtime = ConversationRuntimeContext(
            openai_gateway=openai_gateway,
            memory_gateway=memory_gateway,
            embedding_gateway=embedding_gateway,
            retriever_gateway=retriever_gateway,
            conversation_repository=repository,
            turn_event_writer=TurnEventWriter(id_generator=SystemIdGenerator()),
            clock=SystemClock(),
            id_generator=SystemIdGenerator(),
            logger=logger,
            flags=settings.conversation_flags,
            worker_id=worker_id,
        )
        runtime.context_service = context_service
        runtime.settings = settings
        runtime.token_counter = token_counter
        # P1-10（评审）：active corpus 词表从 RAG 库加载并注入 graph（Q9/D15）
        from backend.conversation.services.corpus_vocabulary import (
            ActiveCorpusVocabularyLoader,
        )
        from backend.rag.database import create_rag_engine

        rag_engine = create_rag_engine(settings=None)
        vocabulary_loader = ActiveCorpusVocabularyLoader(rag_engine)
        # D15 / 第三轮评审 P2：启动时与 active corpus manifest 强校验
        # Embedding 模型标识与维度（不一致 → 启动失败，禁止带错维度运行，§12.1 #3）。
        embedding_failures = vocabulary_loader.validate_embedding_profile(
            model=settings.embedding_model,
            dimensions=settings.rag_embedding_dimensions,
        )
        if embedding_failures:
            raise RuntimeError(
                "Embedding profile 与 active corpus 不匹配: " + "; ".join(embedding_failures)
            )
        vocabulary = vocabulary_loader.load()
        async with AsyncPostgresSaver.from_conn_string(_psycopg_conninfo(settings)) as saver:
            await saver.setup()
            graph = build_conversation_graph(runtime_context=runtime, vocabulary=vocabulary)
            compiled = graph.compile(checkpointer=saver)
            runner = ConversationGraphRunner(
                compiled_graph=compiled,
                runtime_context=runtime,
                graph_thread_id_for_turn=graph_thread_id_for_turn,
                logger=logger,
            )
            graph_worker = ConversationGraphWorker(
                session_factory=db.session_factory,
                config=GraphWorkerConfig(settings),
                graph_runner=runner,
                graph_thread_id_for_turn=graph_thread_id_for_turn,
                worker_id=worker_id,
            )
            job_worker = JobWorker(
                session_factory=db.session_factory,
                config=settings,
                openai_gateway=openai_gateway,
                token_counter=token_counter,
                worker_id=f"job-worker-{uuid4()}",
            )
            graph_worker.install_signal_handlers()
            job_worker.install_signal_handlers()
            from backend.conversation.worker.knowledge_summary_maintenance import (
                KnowledgeSummaryMaintenanceWorker,
            )

            knowledge_summary_maintenance = KnowledgeSummaryMaintenanceWorker(
                session_factory=db.session_factory,
                settings=settings,
            )
            worker_tasks = [
                graph_worker.run_forever(),
                job_worker.run_forever(),
                graph_worker.run_maintenance_loop(),
                knowledge_summary_maintenance.run_forever(),
            ]
            # §14.4：仅 generation 开关开启时装配专用 Gateway/Worker；关闭时不发送
            # 探测请求，也不因知识总结模型配置影响既有 Conversation Worker 启动。
            if settings.conversation_knowledge_summary_generation_enabled:
                from backend.conversation.gateways.knowledge_summary_openai import (
                    KnowledgeSummaryOpenAIGateway,
                )
                from backend.conversation.worker.knowledge_summary_worker import (
                    KnowledgeSummaryWorker,
                )

                knowledge_summary_worker = KnowledgeSummaryWorker(
                    session_factory=db.session_factory,
                    config=settings,
                    gateway=KnowledgeSummaryOpenAIGateway(settings=settings, logger=logger),
                    token_counter=token_counter,
                    worker_id=f"knowledge-summary-worker-{uuid4()}",
                )
                knowledge_summary_worker.install_signal_handlers()
                worker_tasks.append(knowledge_summary_worker.run_forever())
            await asyncio.gather(*worker_tasks)
    finally:
        await db.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
