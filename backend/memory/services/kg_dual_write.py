"""长期记忆 ↔ 知识图谱的双路更新写路径（memory-rebuild §3.5① / §5.9「KG 双路更新」）。

设计约束（来自 §3.5① 与决议 D 组）：

- **不做跨域事务**：长期记忆侧在 consolidation 里已经提交完毕，本模块只负责 KG 侧；
  任一路失败记录最终一致告警，各自幂等重试，不引入 reconciler。
- **幂等可重试**：每条 KG 更新用确定性幂等键
  ``kg-dual-write:{batch_operation_id}:{memory_id}:{version}:{node_id}``，
  并派生确定性的 ``operation_id``（UUIDv5）。重试时先检查
  ``graph_state_audit`` 是否已记录同一批次、同一节点且证据已覆盖，命中即跳过，
  因此不会重复写 Overlay、审计与 Outbox 事件。
- **记录来源**：KG 侧把 ``batch_operation_id`` 落进
  ``graph_state_audit.operation_id``（既有列，来自调用方的批次 operation）、把
  source document version 落进 ``graph_user_states.source_memory_id/version``、
  把带 checksum 的证据快照落进 ``graph_user_states.evidence_snapshot``（既有 JSONB
  扩展位，每条证据含 ``document_checksum``）、把更新时间落进 ``last_evidence_at``
  与 ``graph_state_audit.created_at``。不新建表、不加迁移。
- **失败不抛给调用方**：KG 侧异常一律捕获、记 ``kg_projection_update_failed``
  告警（metrics counter + logger），返回 ``status="failed"``；长期记忆侧的
  ``memory_projection_update_failed`` 由 consolidation 自己记，两边互不阻塞。
- **复用既有通道**：直接复用
  :meth:`backend.memory.services.graph_state_service.KnowledgeGraphStateService.apply_projection`
  （入参 ``ProjectSummaryToGraphCommand``），而不是再经 Outbox 绕一圈——同一条证据
  在 consolidation 里已经是"长期记忆已提交、图谱待投影"的状态，直接投影能拿到
  同步结果与明确失败，Outbox 路径的异步重试语义（幂等键
  ``summary-projection:{memory_id}:{version}:{node_id}``）与其重复。
  ``mapping_method`` 取既有 ``memory_graph_links`` 行原值，不臆造映射。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import ProjectSummaryToGraphCommand
from backend.memory.contracts.common import (
    OPERATION_ROUTING,
    idempotency_payload_hash,
    new_trace_id,
)
from backend.memory.contracts.evidence import GraphProjectionEvidence
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.metrics import memory_kg_dual_write_total
from backend.memory.persistence import operations as ops_repo
from backend.memory.persistence.database import acquire_user_lock
from backend.memory.services.graph_state_service import KnowledgeGraphStateService
from backend.settings import Settings

#: 幂等键前缀（§5.9「KG 双路更新」；最终形态见 `dual_write_idempotency_key`）
IDEMPOTENCY_KEY_PREFIX = "kg-dual-write"

#: 确定性 operation_id 的命名空间（UUIDv5）。固定值，跨进程/跨重试稳定。
KG_DUAL_WRITE_NAMESPACE = UUID("6f2f9a4e-1c3d-4f6b-9a7e-2b5c8d1e4f70")

#: 单条 mastery 对一个节点最多投影的证据条数（对齐
#: ``ProjectSummaryToGraphCommand.evidence`` 与 ``evidence_snapshot`` 的既有上限 50）
MAX_PROJECTION_EVIDENCE = 50

#: consolidation 产出的 direction → 证据契约 direction（§3.5① 契约雏形）
DIRECTION_TO_EVIDENCE: dict[str, str] = {
    "learning": "learning",
    "positive": "positive",
    "strong_positive": "strong_positive",
    "conflict": "conflict",
}


@dataclass(frozen=True)
class DualWriteOutcome:
    """双路更新结果；``status`` 三态见模块 docstring。"""

    status: Literal["applied", "skipped", "failed"]
    reason: str | None = None
    projection_operation_ids: list[UUID] = field(default_factory=list)
    conflicted_memory_ids: list[str] = field(default_factory=list)


def dual_write_idempotency_key(*, batch_operation_id: UUID, memory_id: str, version: int) -> str:
    """批次级确定性幂等键（纯函数）。

    形态：``kg-dual-write:{batch_operation_id}:{memory_id}:{version}``。
    同一 ``(batch, memory_id, version)`` 在任意进程、任意重试下都产生同一字符串。
    """
    return f"{IDEMPOTENCY_KEY_PREFIX}:{batch_operation_id}:{memory_id}:{version}"


def node_idempotency_key(topic_key: str, node_id: str) -> str:
    """节点级确定性幂等键（纯函数）：主题级键 + ``:{node_id}``。

    一个 mastery 主题可能在 ``memory_graph_links`` 里映射到多个 KG 节点，节点维度
    必须进键，否则同一主题的第二个节点会被当成重复投递跳过。
    """
    return f"{topic_key}:{node_id}"


def projection_operation_id(idempotency_key: str) -> UUID:
    """由幂等键派生确定性 operation_id（UUIDv5，纯函数）。

    该值写进 ``graph_state_audit.operation_id``，既是回溯锚点也是重放判定键。
    """
    return uuid5(KG_DUAL_WRITE_NAMESPACE, idempotency_key)


def _evidence_direction(direction: str) -> str:
    """consolidation 的 ``direction`` → 证据契约 direction；未知值降级为 learning。"""
    return DIRECTION_TO_EVIDENCE.get(direction, "learning")


def _evidence_strength(strength: Any) -> float:
    """强度归一化到 [0, 1]；缺失或非法值给保守默认 0.6。"""
    try:
        value = float(strength)
    except (TypeError, ValueError):
        return 0.6
    if value != value:  # NaN
        return 0.6
    return min(1.0, max(0.0, value))


def _as_utc(value: Any, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return fallback


def projection_evidence(
    changed_topics: list[dict[str, Any]],
    *,
    memory_id: str,
    version: int,
    checksum: str | None,
    now: datetime,
) -> list[GraphProjectionEvidence]:
    """``changed_topics`` + 既有 ``memory_graph_links`` 行 → 投影证据列表（纯函数）。

    - 只取 ``memory_id`` / ``version`` 匹配的条目（其余属于同批的其他主题）；
    - ``direction`` 走 :data:`DIRECTION_TO_EVIDENCE` 映射，``strength`` 归一化到 [0, 1]；
    - ``evidence_ref`` 形态 ``{memory_id}:v{version}:{checksum12}:{topic_key}:{occurred_at}``，
      确定性且按 checksum 区分版本，重试时可比对"是否已投影过同一份内容"；
    - 按 evidence_ref 去重后截断到 :data:`MAX_PROJECTION_EVIDENCE`。
    """
    evidence: list[GraphProjectionEvidence] = []
    seen: set[str] = set()
    checksum_part = (checksum or "nochecksum")[:12]
    for topic in changed_topics:
        if str(topic.get("memory_id")) != memory_id:
            continue
        raw_version = topic.get("version")
        if raw_version is None:
            continue
        try:
            topic_version = int(raw_version)
        except (TypeError, ValueError):
            continue
        if topic_version != version:
            continue
        occurred_at = _as_utc(topic.get("occurred_at"), fallback=now)
        topic_key = str(topic.get("topic_key") or "")
        direction = _evidence_direction(str(topic.get("direction") or ""))
        # ref 里带 direction：同主题同 checksum 下"学习证据 + 冲突证据"必须各自成条，
        # 不能被去重吞掉（冲突标注是 §3.5② 的硬要求）
        evidence_ref = (
            f"{memory_id}:v{version}:{checksum_part}:{topic_key}:{direction}:"
            f"{occurred_at.isoformat()}"
        )
        if evidence_ref in seen:
            continue
        seen.add(evidence_ref)
        evidence.append(
            GraphProjectionEvidence(
                evidence_ref=evidence_ref,
                direction=direction,  # type: ignore[arg-type]
                strength=_evidence_strength(topic.get("strength")),
                occurred_at=occurred_at,
            )
        )
    return evidence[:MAX_PROJECTION_EVIDENCE]


async def _load_active_links(
    session: AsyncSession, *, user_id: UUID, memory_id: str, version: int
) -> list[dict[str, Any]]:
    """读既有 ``memory_graph_links`` 活动映射行；空列表即"无 KG 映射"。"""
    result = await session.execute(
        text(
            "SELECT node_id, mapping_method, mapping_confidence FROM memory_graph_links "
            "WHERE user_id = :user_id AND memory_id = :memory_id "
            "AND memory_version = :version AND active = true "
            "ORDER BY node_id ASC"
        ),
        {"user_id": user_id, "memory_id": memory_id, "version": version},
    )
    return [dict(row) for row in result.mappings().all()]


async def _projection_recorded(
    session: AsyncSession, *, operation_id: UUID, evidence: list[GraphProjectionEvidence]
) -> bool:
    """重放判定：``graph_state_audit`` 是否已记录本批本节点的同一份证据。

    命中条件（两者同时成立）：
    1. 存在 ``operation_id`` 相同的审计行（即本批次已经投影过该节点）；
    2. 该审计行至少覆盖本次要写的全部 evidence_ref。

    这样即使同一批次对同一节点重放，也不会重复写 Overlay/审计/Outbox；
    而"同一批次但证据集合变大"（理论上的二次 consolidation）仍会继续叠加。
    """
    if not evidence:
        return False
    result = await session.execute(
        text(
            "SELECT evidence_refs FROM graph_state_audit "
            "WHERE operation_id = :operation_id ORDER BY created_at ASC"
        ),
        {"operation_id": operation_id},
    )
    wanted = {item.evidence_ref for item in evidence}
    for row in result.mappings().all():
        recorded = {str(ref) for ref in (row["evidence_refs"] or [])}
        if wanted <= recorded:
            return True
    return False


async def _record_idempotency_anchor(
    session: AsyncSession,
    *,
    user_id: UUID,
    operation_id: UUID,
    idempotency_key: str,
    command: ProjectSummaryToGraphCommand,
    now: datetime,
) -> None:
    """在 ``memory_operations`` 留一条终态（cancelled）追溯锚，不排队、不被认领。

    为什么需要它：``graph_state_audit.operation_id`` 有指向 ``memory_operations`` 的
    外键，而审计行里必须能看出"这次 KG 写入对应哪个确定性幂等键"，所以先插一条终态
    operation 行（``status='cancelled'`` 是 worker 永不认领的终态），幂等键
    ``kg-dual-write:{batch}:{memory_id}:{version}:{node_id}`` 就落在它的
    ``idempotency_key`` 上。重复调用时 ``ON CONFLICT DO NOTHING`` 直接返回，
    不会产生第二条。

    不排队是刻意的：真正的投影由本模块同步调用 ``apply_projection`` 完成，若把行留在
    ``queued``，worker 可能并发地再跑一遍同一条投影，破坏"重试不重复应用"。
    """
    routing = OPERATION_ROUTING["project_summary_to_graph"]
    input_kind, priority = routing
    operation = MemoryOperation(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        user_id=user_id,
        actor_type="summary_projection",
        input_kind=input_kind,
        operation_type="project_summary_to_graph",
        priority=priority,
        occurred_at=now,
        payload=command,
        trace_id=new_trace_id(),
        graph_thread_id=f"kg-dual-write:{operation_id}",
    )
    await ops_repo.insert_operation(
        session,
        operation,
        idempotency_payload_hash=idempotency_payload_hash(command.model_dump(mode="json")),
        status="cancelled",
    )


def _conflicted_memory_ids(conflicts: list[dict[str, Any]]) -> list[str]:
    """汇总 consolidation 的并列冲突标注 → memory_id 列表（保持首次出现顺序）。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for conflict in conflicts:
        for memory_id in conflict.get("memory_ids") or []:
            key = str(memory_id)
            if key and key not in seen:
                seen.add(key)
                ordered.append(key)
    return ordered


def _warn(
    logger: logging.Logger,
    *,
    message: str,
    user_id: UUID,
    batch_operation_id: UUID,
    reason: str,
    memory_id: str | None = None,
) -> None:
    """统一告警格式：``kg_projection_update_failed`` + 结构化字段（§5.9）。"""
    logger.warning(
        "kg_projection_update_failed user=%s batch=%s memory=%s reason=%s msg=%s",
        user_id,
        batch_operation_id,
        memory_id or "-",
        reason,
        message,
    )


async def after_consolidation(
    *,
    user_id: UUID,
    batch_operation_id: UUID,
    operation_id: UUID,  # 批次 operation（幂等键来源）
    changed_topics: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    logger: logging.Logger,
) -> DualWriteOutcome:
    """consolidation 之后的双路更新（§5.9「KG 双路更新」）。

    只在 ``settings.memory_kg_dual_write_enabled`` 为 True 时做事；为 False 时立即
    返回 ``DualWriteOutcome(status="skipped", reason="flag_disabled")``，不产生任何副作用。
    """
    conflicted = _conflicted_memory_ids(conflicts)
    if not settings.memory_kg_dual_write_enabled:
        # flag 关闭：零副作用（不读库、不写指标、不打日志）
        return DualWriteOutcome(status="skipped", reason="flag_disabled")

    if not changed_topics:
        return DualWriteOutcome(
            status="skipped", reason="no_changed_topics", conflicted_memory_ids=conflicted
        )

    now = datetime.now(UTC)
    applied_operation_ids: list[UUID] = []
    applied = 0
    unmapped = 0
    no_change = 0
    service = KnowledgeGraphStateService(settings=settings, session_factory=session_factory)

    for topic in changed_topics:
        memory_id = str(topic.get("memory_id") or "")
        if not memory_id:
            continue
        raw_version = topic.get("version")
        try:
            version = int(raw_version) if raw_version is not None else 0
        except (TypeError, ValueError):
            _warn(
                logger,
                message="changed_topic 缺少合法 version，跳过该条",
                user_id=user_id,
                batch_operation_id=batch_operation_id,
                reason="invalid_version",
                memory_id=memory_id,
            )
            unmapped += 1
            continue

        main_key = dual_write_idempotency_key(
            batch_operation_id=batch_operation_id, memory_id=memory_id, version=version
        )
        checksum = topic.get("checksum")
        evidence = projection_evidence(
            changed_topics,
            memory_id=memory_id,
            version=version,
            checksum=str(checksum) if checksum else None,
            now=now,
        )
        if not evidence:
            unmapped += 1
            continue
        try:
            async with session_factory() as session:
                links = await _load_active_links(
                    session, user_id=user_id, memory_id=memory_id, version=version
                )
            if not links:
                # mastery 主题与 KG node 之间没有映射：优雅跳过（不报错）
                unmapped += 1
                continue

            for link in links:
                node_id = str(link["node_id"])
                anchor_operation_id = projection_operation_id(
                    node_idempotency_key(main_key, node_id)
                )
                async with session_factory() as session:
                    if await _projection_recorded(
                        session, operation_id=operation_id, evidence=evidence
                    ):
                        # 重放：本批次该节点已投影，幂等成功且不重复应用
                        no_change += 1
                        continue
                command = ProjectSummaryToGraphCommand(
                    trigger_event_type="memory.changed",
                    projection_action="apply_active_version",
                    source_memory_id=memory_id,
                    source_version=version,
                    node_id=node_id,
                    mapping_method=link["mapping_method"],
                    mapping_confidence=float(link["mapping_confidence"]),
                    evidence=evidence,
                )
                async with session_factory() as session:
                    async with session.begin():
                        # 锁顺序与 KG 写入一致（§13.18）：先用户锁再写 operation
                        await acquire_user_lock(session, user_id)
                        await _record_idempotency_anchor(
                            session,
                            user_id=user_id,
                            operation_id=anchor_operation_id,
                            idempotency_key=node_idempotency_key(main_key, node_id),
                            command=command,
                            now=now,
                        )
                outcome = await service.apply_projection(
                    operation_id=operation_id, user_id=user_id, command=command
                )
                if outcome.changed:
                    applied += 1
                    applied_operation_ids.append(anchor_operation_id)
                else:
                    # 投影已执行但没有状态变化（权威状态已等于评估结果、stale 投递、
                    # 证据不足、用户 grace 期内）：按 skipped 归因，不算 failed
                    no_change += 1
                memory_kg_dual_write_total.labels(
                    status="applied" if outcome.changed else "skipped"
                ).inc()
        except Exception as exc:  # KG 侧任何失败都不得抛给调用方
            _warn(
                logger,
                message=f"{type(exc).__name__}: {exc}"[:500],
                user_id=user_id,
                batch_operation_id=batch_operation_id,
                reason="kg_projection_failed",
                memory_id=memory_id,
            )
            memory_kg_dual_write_total.labels(status="failed").inc()
            return DualWriteOutcome(
                status="failed",
                reason="kg_projection_failed",
                projection_operation_ids=applied_operation_ids,
                conflicted_memory_ids=conflicted,
            )

    if applied == 0:
        if unmapped:
            reason = "no_graph_mapping"
        elif no_change == 0:
            reason = "no_projectable_topic"
        else:
            reason = "no_effective_change"
        return DualWriteOutcome(status="skipped", reason=reason, conflicted_memory_ids=conflicted)
    return DualWriteOutcome(
        status="applied",
        projection_operation_ids=applied_operation_ids,
        conflicted_memory_ids=conflicted,
    )


__all__ = [
    "DIRECTION_TO_EVIDENCE",
    "IDEMPOTENCY_KEY_PREFIX",
    "KG_DUAL_WRITE_NAMESPACE",
    "MAX_PROJECTION_EVIDENCE",
    "DualWriteOutcome",
    "after_consolidation",
    "dual_write_idempotency_key",
    "node_idempotency_key",
    "projection_evidence",
    "projection_operation_id",
]
