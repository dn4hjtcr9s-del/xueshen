"""Evidence 批次契约（memory-rebuild 计划 §5.2 Phase 0-A / §2.6 / §5.8）。

证据池复用 ``memory_operations`` 单表（§2.6 决议"方案 A 修订版"）：普通证据落
``pending_batch`` 状态，并用现成的 ``next_run_at`` 做最短沉淀门控；0 点批量任务按
``user_id`` 聚合生成一个 ``summarize_user_memory_batch`` 批量 operation，成员行写
``batch_operation_id`` 建立归属。不新增独立 evidence inbox 表。

**为什么 ``pending_batch`` 对 Worker 天然不可见**：共享认领查询
（``backend/memory/persistence/operations.py::claim_operation``）只认
``status IN ('queued', 'retry_wait') AND next_run_at <= now()``，
部分索引 ``ix_memory_operations_claim`` 也带同样的谓词。新增状态值不需要改认领
查询，也不需要改索引，因此普通 Worker / Gateway P0 快速路径不会把待批量证据
单独领走——这正是选择复用单表而非新建表的关键前提。

兼容规则：本模块只新增状态值与新 operation 类型，不删除、不重命名任何既有枚举；
``pending_batch`` 属于非终态，故不进入 ``TERMINAL_STATUSES``。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.shared.cursor import canonical_json

#: 批量 operation 类型（`OperationType` 新增值，Phase 6 由 Scheduler 生成）。
BATCH_OPERATION_TYPE = "summarize_user_memory_batch"

#: 批次幂等键模板（§2.6：`summarize:{user_id}:{date}`）。
BATCH_IDEMPOTENCY_KEY_TEMPLATE = "summarize:{user_id}:{date}"

#: 单批证据上限默认值（§4.5-① 决议 B 组：默认 50 条，图内不分批 commit）。
EVIDENCE_BATCH_MAX_DEFAULT = 50

#: 证据最短沉淀小时数默认值（§2.6 D4）。
EVIDENCE_MIN_AGE_HOURS_DEFAULT = 6

#: 每日批量总结触发时刻默认值（§2.6 D4；时区走 memory_scheduler_timezone）。
SUMMARY_DAILY_TIME_DEFAULT = "00:00"

#: 单 run 最多处理用户数默认值（Phase 0 决策：50）。
SUMMARY_MAX_USERS_PER_RUN_DEFAULT = 50

#: 批量总结 LLM 并发默认值（§2.6 引用 codex stage_one 常量：并发 8、租约 3600s）。
SUMMARY_LLM_CONCURRENCY_DEFAULT = 8


#: 批次成员（evidence operation）在其生命周期内允许出现的状态。
#: ``pending_batch`` 是入批前的沉淀态；批量 op 成功后转 ``succeeded``，
#: 批量 op 失败期间保持 ``pending_batch``（由父批次 lease/retry 承接），
#: 批量 op 进死信时成员一并转 ``dead_letter``。
BatchMemberStatus = Literal["pending_batch", "succeeded", "dead_letter"]

#: 合法状态迁移（含自环，表示"保持"）。用于 Phase 6 的成员状态断言。
BATCH_MEMBER_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending_batch": frozenset({"pending_batch", "succeeded", "dead_letter"}),
    "succeeded": frozenset({"succeeded"}),
    "dead_letter": frozenset({"dead_letter"}),
}


#: 触发"下一个 0 点"豁免的 evidence trigger（§2.6 D5：用户显式记住不受沉淀时长约束）。
EXEMPT_TRIGGERS: frozenset[str] = frozenset({"explicit_remember"})


def next_daily_gate(now: datetime, *, daily_time: time, timezone: str) -> datetime:
    """``daily_time``（``timezone`` 本地时间）的下一次出现，返回 UTC 时刻。

    恰好等于当前时刻时取**次日**——门控必须是"未来"，否则刚提交的证据会在同一秒入批，
    "固定 0 点批量"的语义就没了。Scheduler 的 ``_next_daily`` 与本函数同源，
    避免"提交侧算的门控"和"调度侧算的触发点"用两套时间逻辑。
    """
    tz = ZoneInfo(timezone)
    local = now.astimezone(tz)
    candidate = local.replace(
        hour=daily_time.hour, minute=daily_time.minute, second=0, microsecond=0
    )
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def evidence_gate(
    *,
    now: datetime,
    trigger: str,
    min_age_hours: int,
    daily_time: time,
    timezone: str,
) -> datetime:
    """证据的最早可入批时刻（§2.6 状态机第 1 步）。

    - 普通证据：``submitted_at + min_age_hours``（最短沉淀时长，D4 参数化）；
    - ``explicit_remember``（D5 豁免）：下一个 0 点，不受沉淀时长约束。
    """
    if trigger in EXEMPT_TRIGGERS:
        return next_daily_gate(now, daily_time=daily_time, timezone=timezone)
    return now + timedelta(hours=max(min_age_hours, 0))


class BatchContractError(ValueError):
    """批次不满足契约（超上限、跨用户混批、非法状态迁移、游标损坏）。"""


def validate_member_transition(current: str, target: str) -> str:
    """校验成员状态迁移合法；不允许把已终态成员改回在途态。"""
    allowed = BATCH_MEMBER_TRANSITIONS.get(current)
    if allowed is None:
        raise BatchContractError(f"未知批次成员状态: {current}")
    if target not in allowed:
        raise BatchContractError(f"非法批次成员状态迁移: {current} -> {target}")
    return target


class EvidenceBatchLimits(BaseModel):
    """有界认领三件套 + 单批证据上限（§2.6 D4）。"""

    model_config = ConfigDict(extra="forbid")

    max_evidence: int = Field(default=EVIDENCE_BATCH_MAX_DEFAULT, ge=1, le=1000)
    max_users_per_run: int = Field(default=SUMMARY_MAX_USERS_PER_RUN_DEFAULT, ge=1, le=10_000)
    llm_concurrency: int = Field(default=SUMMARY_LLM_CONCURRENCY_DEFAULT, ge=1, le=64)


class EvidenceBatchMember(BaseModel):
    """批次成员：一条待沉淀的 evidence operation。

    ``eligible_at`` 对应 ``memory_operations.next_run_at``——沉淀门控到点后才可入批。
    """

    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    user_id: UUID
    eligible_at: datetime
    created_at: datetime
    status: BatchMemberStatus = "pending_batch"

    @field_validator("eligible_at", "created_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("批次成员时间必须带时区（UTC）")
        return value.astimezone(UTC)


class BatchCursor(BaseModel):
    """批次续跑游标。

    排序键固定为 ``(eligible_at, created_at, operation_id)``（§5.8"批次构造按稳定
    排序…避免多副本/重试时批次成员漂移"）；``operation_id`` 作为最后一级保证全序，
    使游标在完全相同的 ``next_run_at`` 下也能确定性推进。

    载体是 ``memory_maintenance_runs.cursor``（varchar(500)）的不透明字符串，
    与既有 maintenance 任务的续跑机制一致，不新增游标存储。
    """

    model_config = ConfigDict(extra="forbid")

    eligible_at: datetime
    created_at: datetime
    operation_id: UUID

    @field_validator("eligible_at", "created_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("游标时间必须带时区（UTC）")
        return value.astimezone(UTC)

    def encode(self) -> str:
        """序列化为不透明字符串（canonical JSON，保证同值同串）。"""
        return canonical_json(
            {
                "eligible_at": self.eligible_at.isoformat(),
                "created_at": self.created_at.isoformat(),
                "operation_id": str(self.operation_id),
            }
        )

    @classmethod
    def decode(cls, value: str) -> BatchCursor:
        """从 ``maintenance_runs.cursor`` 还原；损坏的游标按契约错误处理。"""
        try:
            raw: dict[str, Any] = json.loads(value)
        except json.JSONDecodeError as exc:
            raise BatchContractError(f"批次游标不是合法 JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise BatchContractError("批次游标必须是 JSON 对象")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise BatchContractError(f"批次游标不满足契约: {exc}") from exc


class EvidenceBatchPlan(BaseModel):
    """一个批量 operation 的成员集合（每用户一批，§2.6 D6）。"""

    model_config = ConfigDict(extra="forbid")

    batch_operation_id: UUID
    user_id: UUID
    member_operation_ids: list[UUID] = Field(min_length=1)
    limits: EvidenceBatchLimits = Field(default_factory=EvidenceBatchLimits)
    cursor: BatchCursor | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at 必须带时区（UTC）")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _check_limits_and_isolation(self) -> EvidenceBatchPlan:
        if len(self.member_operation_ids) > self.limits.max_evidence:
            raise ValueError(
                f"批次成员数 {len(self.member_operation_ids)} 超过上限 {self.limits.max_evidence}"
            )
        if len(set(self.member_operation_ids)) != len(self.member_operation_ids):
            raise ValueError("批次成员 operation_id 不得重复")
        return self

    def is_member(self, operation_id: UUID) -> bool:
        """归属判定：一个证据只进一个批（§2.6「单 FK 足够」）。"""
        return operation_id in set(self.member_operation_ids)
