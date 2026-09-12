"""consolidation 末段的集成测试（memory-rebuild §5.9①）：真实 PG + 真实文件存储。

覆盖 §5.9「Phase 7 验收」里必须落盘才能证明的部分：

- summary 三段按 §2.3 格式写入（首行恰好 ``v1``），旧版本留快照可回滚；
- 同一批次重跑**不重复写 summary 版本**（末段幂等）；
- keywords 经 frontmatter 补丁落进文档（新版本）并投影到 PG 索引表；
- alias 归并把近义主题的名字并进 canonical 的 aliases，**不删除任何文档**；
- 悬空链接第一次出现只登记候选（≥2 批才建档）。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import (
    CommitMutationPlan,
    FrontMatterPatch,
    MasteryPatch,
)
from backend.memory.graph.consolidation import consolidate_user_memory
from backend.memory.graph.llm_schemas import (
    ConsolidationAliasMerge,
    ConsolidationKeywordSet,
    ConsolidationResult,
    ConsolidationTopicRoute,
)
from backend.memory.graph.openai_client import FakeMemoryLLMClient
from backend.memory.graph.state import MemoryRuntimeContext
from backend.memory.services.memory_service import MemoryService
from backend.memory.storage import summary_file
from backend.settings import Settings
from tests.integration.graph_helpers import make_operation, persist_operation

USER = UUID("00000000-0000-4000-8000-000000000051")


def _runtime(
    runtime_context: MemoryRuntimeContext, *, consolidation: bool, kg: bool = False
) -> MemoryRuntimeContext:
    """打开 consolidation / KG 开关（Settings 是 pydantic 模型，用 model_copy 改）。"""
    settings: Settings = runtime_context.settings.model_copy(
        update={
            "memory_consolidation_enabled": consolidation,
            "memory_kg_dual_write_enabled": kg,
        }
    )
    return dataclasses.replace(runtime_context, settings=settings)


def _state(
    *, batch_operation_id: UUID, processed: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "operation": {
            "operation_id": str(batch_operation_id),
            "idempotency_key": "batch-consolidation-it",
            "user_id": str(USER),
            "actor_type": "system",
            "input_kind": "evidence",
            "operation_type": "summarize_user_memory_batch",
            "priority": 50,
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": {
                "kind": "summarize_user_memory_batch",
                "target_user_id": str(USER),
                "batch_operation_id": str(batch_operation_id),
                "member_operation_ids": [str(uuid4())],
                "max_evidence": 50,
            },
            "trace_id": "t" * 32,
            "graph_thread_id": f"memory-op:{batch_operation_id}",
        },
        "batch_active": True,
        "batch_processed": processed
        or [{"operation_id": str(uuid4()), "outcome": "succeeded", "mutation_count": 1}],
        "batch_warnings": [],
        "batch_members": [],
    }


async def _persist_operation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    operation_type: str = "conversation_evidence",
    payload: Any = None,
) -> UUID:
    """落一条真实 operation 行：`memory_commits.operation_id` 是 FK，不能凭空造 id。"""
    from backend.memory.contracts.evidence import ConversationEvidence

    operation = make_operation(
        user_id=USER,
        actor_type="system" if operation_type.endswith("batch") else "conversation_agent",
        input_kind="evidence",
        operation_type=operation_type,  # type: ignore[arg-type]
        priority=50,
        payload=payload
        or ConversationEvidence(
            thread_id="t-consolidation", message_ids=["m1"], trigger="turn_boundary"
        ),
    )
    await persist_operation(session_factory, operation)  # type: ignore[arg-type]
    return operation.operation_id


async def _create_mastery(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    topic_key: str,
    description: str = "一行描述",
    aliases: list[str] | None = None,
    overview: str = "概述",
) -> None:
    """走真实提交路径建一个 mastery 文档（frontmatter v2 由 commit 路径渲染）。"""
    await memory_service.commit_plans(
        operation_id=await _persist_operation(session_factory),
        user_id=USER,
        actor_type="system",
        plans=[
            CommitMutationPlan(
                mutation_id=uuid4(),
                memory_id=f"mastery:{topic_key}",
                target_memory_type="mastery",
                topic_title=topic_key,
                action="create",
                mastery_patch=MasteryPatch(overview=overview, understood_to_add=["定义"]),
                frontmatter_patch=FrontMatterPatch(
                    name=topic_key, description=description, aliases=aliases or []
                ),
            )
        ],
    )


async def _reload_mastery(memory_service: MemoryService, topic_key: str) -> Any:
    return await memory_service.get_mastery(user_id=USER, topic_key=topic_key)


def _result(**overrides: Any) -> ConsolidationResult:
    payload: dict[str, Any] = {
        "user_profile": ["正在系统复习圆锥曲线"],
        "stable_preferences": ["讲解先给结论"],
        "topic_routes": [
            ConsolidationTopicRoute(topic_key="椭圆", status_line="入门", proficiency="learning")
        ],
        "keywords": [
            ConsolidationKeywordSet(memory_id="mastery:椭圆", keywords=["焦点", "离心率"])
        ],
        "alias_merges": [
            ConsolidationAliasMerge(
                canonical_topic_key="椭圆",
                merged_topic_keys=["椭圆形"],
                reason="同义命名",
            )
        ],
        "conflicts": [],
    }
    payload.update(overrides)
    return ConsolidationResult.model_validate(payload)


@pytest.mark.asyncio
async def test_consolidation_writes_summary_keywords_and_dangling_candidate(
    runtime_context: MemoryRuntimeContext,
    memory_service: MemoryService,
    fake_llm: FakeMemoryLLMClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_mastery(memory_service, session_factory, topic_key="椭圆", aliases=["椭圆曲线"])
    await _create_mastery(
        memory_service, session_factory, topic_key="椭圆形", overview="与椭圆重复的命名"
    )
    # 悬空链接：[[抛物线]] 没有对应文档
    await memory_service.commit_plans(
        operation_id=await _persist_operation(session_factory),
        user_id=USER,
        actor_type="system",
        plans=[
            CommitMutationPlan(
                mutation_id=uuid4(),
                memory_id="mastery:椭圆",
                target_memory_type="mastery",
                topic_title="椭圆",
                action="merge",
                expected_version=1,
                mastery_patch=MasteryPatch(overview="与 [[抛物线]] 的焦点性质容易混淆"),
            )
        ],
    )
    runtime = _runtime(runtime_context, consolidation=True)
    fake_llm.consolidate_queue.append(_result())
    batch_id = await _persist_operation(
        session_factory, operation_type="summarize_user_memory_batch"
    )

    outcome = await consolidate_user_memory(
        _state(batch_operation_id=batch_id), _runtime_adapter(runtime)
    )

    consolidation = outcome["batch_consolidation"]
    assert consolidation["status"] == "completed", consolidation
    root = Path(runtime.settings.memory_storage_root)

    # 1. summary 落盘且首行恰好 v1
    raw = summary_file.summary_path(root, USER).read_text(encoding="utf-8")
    assert raw.splitlines()[0] == "v1"
    assert "## 用户画像" in raw and "## 稳定偏好" in raw and "## 主题路由" in raw
    meta = summary_file.read_summary_meta_sync(root, USER)
    assert meta is not None and meta.version == 1
    assert meta.batch_operation_id == str(batch_id)

    # 2. keywords 落进文档（新版本）；投影链路由 keywords 专项测试覆盖
    ellipse = await _reload_mastery(memory_service, "椭圆")
    assert ellipse is not None and list(ellipse.keywords) == ["焦点", "离心率"]
    assert ellipse.version >= 3

    # 3. alias 归并：只加 alias，不删文档
    merged_aliases = [str(alias) for alias in ellipse.aliases]
    assert "椭圆形" in merged_aliases
    assert await _reload_mastery(memory_service, "椭圆形") is not None, "不得删除被并入的文档"
    assert consolidation["aliases"]["mappings"], "映射关系必须留痕"

    # 4. 悬空链接只登记候选（本批第 1 次出现）
    async with runtime.session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT target, sighting_batches, status FROM memory_dangling_links "
                        "WHERE user_id = :user_id"
                    ),
                    {"user_id": USER},
                )
            )
            .mappings()
            .all()
        )
    assert [row["target"] for row in rows] == ["抛物线"]
    assert rows[0]["sighting_batches"] == 1
    assert rows[0]["status"] == "candidate"


@pytest.mark.asyncio
async def test_consolidation_is_idempotent_per_batch(
    runtime_context: MemoryRuntimeContext,
    memory_service: MemoryService,
    fake_llm: FakeMemoryLLMClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """同一批次重跑不再写 summary 版本（§5.9 验收）。"""
    await _create_mastery(memory_service, session_factory, topic_key="导数")
    runtime = _runtime(runtime_context, consolidation=True)
    batch_id = await _persist_operation(
        session_factory, operation_type="summarize_user_memory_batch"
    )
    fake_llm.consolidate_queue.append(
        _result(
            topic_routes=[
                ConsolidationTopicRoute(
                    topic_key="导数", status_line="入门", proficiency="learning"
                )
            ],
            keywords=[ConsolidationKeywordSet(memory_id="mastery:导数", keywords=["切线"])],
            alias_merges=[],
            user_profile=["画像"],
            stable_preferences=[],
        )
    )
    first = await consolidate_user_memory(
        _state(batch_operation_id=batch_id), _runtime_adapter(runtime)
    )
    assert first["batch_consolidation"]["status"] == "completed"

    # 重跑：同一 batch → 直接跳过，且**不消耗 LLM 队列**（队列已空，若再调会抛错）
    second = await consolidate_user_memory(
        _state(batch_operation_id=batch_id), _runtime_adapter(runtime)
    )

    assert second["batch_consolidation"]["status"] == "skipped"
    assert second["batch_consolidation"]["reason"] == "already_consolidated"
    root = Path(runtime.settings.memory_storage_root)
    meta = summary_file.read_summary_meta_sync(root, USER)
    assert meta is not None and meta.version == 1, "重跑不得推进 summary 版本"


@pytest.mark.asyncio
async def test_consolidation_skips_when_flag_disabled(
    runtime_context: MemoryRuntimeContext,
    memory_service: MemoryService,
    fake_llm: FakeMemoryLLMClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """flag 关闭时末段不做任何事（由 batch 节点短路，这里直接验证短路语义）。"""
    from backend.memory.graph.batch import enter_batch_consolidation

    await _create_mastery(memory_service, session_factory, topic_key="数列")
    runtime = _runtime(runtime_context, consolidation=False)
    outcome = await enter_batch_consolidation(
        _state(batch_operation_id=uuid4()),  # type: ignore[arg-type]
        _runtime_adapter(runtime),
    )

    assert outcome["batch_consolidation"] == {"status": "disabled"}
    root = Path(runtime.settings.memory_storage_root)
    assert not summary_file.summary_path(root, USER).exists()
    assert fake_llm.consolidate_queue == []


def _runtime_adapter(runtime: MemoryRuntimeContext) -> Any:
    """把 MemoryRuntimeContext 包成 LangGraph 的 Runtime（节点只读 ``.context``）。"""

    class _Runtime:
        def __init__(self, context: MemoryRuntimeContext) -> None:
            self.context = context

    return _Runtime(runtime)


@pytest.mark.asyncio
async def test_consolidation_degrades_to_routes_only_when_over_budget(
    runtime_context: MemoryRuntimeContext,
    memory_service: MemoryService,
    fake_llm: FakeMemoryLLMClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """超预算：只重写主题路由段，画像/偏好沿用旧摘要（§4.5-② 决议）。"""
    # 用长 overview 把输入撑过 1000 字符下限（setting 的 ge=1000，无法调更低）
    for index in range(4):
        await _create_mastery(
            memory_service,
            session_factory,
            topic_key=f"主题{index}",
            overview="很长的掌握情况描述" * 20,
        )
    settings = runtime_context.settings.model_copy(
        update={
            "memory_consolidation_enabled": True,
            # 压到极小：任何真实输入都会超预算
            "memory_consolidation_input_max_chars": 1_000,
        }
    )
    runtime = dataclasses.replace(runtime_context, settings=settings)
    root = Path(settings.memory_storage_root)
    # 先写一份旧摘要，供降级时保留画像/偏好
    summary_file.write_summary_sync(
        root,
        USER,
        "## 用户画像\n- 旧画像要点\n\n## 稳定偏好\n- 旧偏好要点\n\n## 主题路由\n- 旧路由\n",
        batch_operation_id=None,
        generated_at=datetime.now(UTC),
    )
    fake_llm.consolidate_queue.append(
        _result(
            user_profile=["新画像"],
            stable_preferences=["新偏好"],
            keywords=[],
            alias_merges=[],
        )
    )

    outcome = await consolidate_user_memory(
        _state(batch_operation_id=uuid4()), _runtime_adapter(runtime)
    )

    assert outcome["batch_consolidation"]["degraded"] is True
    raw = summary_file.summary_path(root, USER).read_text(encoding="utf-8")
    assert "旧画像要点" in raw, "降级时必须保留旧画像"
    assert "旧偏好要点" in raw, "降级时必须保留旧偏好"
    assert "新画像" not in raw


@pytest.mark.asyncio
async def test_consolidation_calls_kg_dual_write_without_breaking_long_term_memory(
    runtime_context: MemoryRuntimeContext,
    memory_service: MemoryService,
    fake_llm: FakeMemoryLLMClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """打开 `memory_kg_dual_write_enabled`：KG 侧要么落地、要么优雅跳过，绝不影响长期记忆。

    没有 mastery→KG node 映射时应当是 `skipped`（`no_graph_mapping` 一类）而不是报错——
    这是主控节点与 KG 模块之间的真实接线（其余用例都在 flag 关闭下跑，覆盖不到）。
    """
    await _create_mastery(memory_service, session_factory, topic_key="椭圆")
    runtime = _runtime(runtime_context, consolidation=True, kg=True)
    fake_llm.consolidate_queue.append(_result())

    batch_id = await _persist_operation(
        session_factory, operation_type="summarize_user_memory_batch"
    )
    outcome = await consolidate_user_memory(
        _state(batch_operation_id=batch_id), _runtime_adapter(runtime)
    )

    consolidation = outcome["batch_consolidation"]
    assert consolidation["status"] == "completed"
    kg = consolidation["kg_dual_write"]
    assert kg["status"] in {"applied", "skipped", "failed"}, kg
    # 长期记忆侧不受影响：summary 与 keywords 都已落盘
    root = Path(runtime.settings.memory_storage_root)
    assert summary_file.summary_path(root, USER).exists()
    ellipse = await _reload_mastery(memory_service, "椭圆")
    assert ellipse is not None and list(ellipse.keywords) == ["焦点", "离心率"]
