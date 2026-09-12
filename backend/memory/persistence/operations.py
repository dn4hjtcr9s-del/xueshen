"""memory_operations 仓储（规格 §13.2 / §11 / §14.2）。

Gateway 与 Worker 复用同一个 claim_operation（FOR UPDATE SKIP LOCKED）。

memory-rebuild §2.6 / §5.8 追加证据池批次（复用本表，不建新表）：

- 证据提交时落 ``pending_batch``，``next_run_at`` 承担最短沉淀门控；
  :func:`claim_operation` 只认 ``('queued','retry_wait')``，因此该状态对
  Worker / Gateway 快速路径**天然不可见**，认领查询与索引都不需要改。
- ``batch_operation_id`` 建立"成员证据 → 批次 operation"归属，**一个证据只进一个批**：
  扫描只取 ``batch_operation_id IS NULL`` 的行，失败重试期间成员归属不变，不会被第二个
  批次重复领走。
- 批次终态在同一事务内回写成员状态（:func:`settle_batch_members`），保证"成员状态与
  父批次状态一致"这一验收项不需要额外对账。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.contracts.common import max_attempts_for_priority
from backend.memory.contracts.errors import OperationCancelNotAllowedError
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.persistence.database import exec_rowcount

#: 失败成员释放的三种结果（评审新发现 13）：释放回池子 / 升级 dead_letter / 没命中。
BATCH_MEMBER_RELEASED = "released"
BATCH_MEMBER_DEAD_LETTERED = "dead_letter"
BATCH_MEMBER_NOT_FOUND = "not_found"


@dataclass(frozen=True)
class BatchMemberReleaseOutcome:
    """:func:`release_batch_member` 的结果（含递增后的尝试计数）。"""

    status: str
    #: 递增后的 ``memory_operations.attempt_count``（``not_found`` 时为 0）。
    attempt_count: int

    @property
    def released(self) -> bool:
        """是否真的回到了证据池（dead_letter / not_found 都不算）。"""
        return self.status == BATCH_MEMBER_RELEASED


INSERT_SQL = text(
    """
    INSERT INTO memory_operations (
        operation_id, user_id, actor_type, input_kind, operation_type,
        idempotency_key, idempotency_payload_hash, priority, status,
        payload, result, public_error, trace_id, graph_thread_id,
        occurred_at, max_attempts, next_run_at
    ) VALUES (
        :operation_id, :user_id, :actor_type, :input_kind, :operation_type,
        :idempotency_key, :idempotency_payload_hash, :priority, :status,
        CAST(:payload AS jsonb), NULL, NULL, :trace_id, :graph_thread_id,
        :occurred_at, :max_attempts, COALESCE(:next_run_at, now())
    )
    ON CONFLICT ON CONSTRAINT uq_memory_operation_idempotency DO NOTHING
    """
)


async def insert_operation(
    session: AsyncSession,
    operation: MemoryOperation,
    *,
    idempotency_payload_hash: str,
    status: str = "queued",
    next_run_at: datetime | None = None,
) -> bool:
    """插入 operation；幂等冲突时返回 False（调用方应读取原 operation）。

    ``status`` / ``next_run_at`` 供证据池使用（§2.6）：证据落 ``pending_batch`` 并把
    ``next_run_at`` 设为"最短沉淀到点时刻"。两者都有与 DDL 默认值一致的缺省值，
    既有调用方不传时行为逐字不变。
    """
    rowcount = await exec_rowcount(
        session,
        INSERT_SQL,
        {
            "operation_id": operation.operation_id,
            "user_id": operation.user_id,
            "actor_type": operation.actor_type,
            "input_kind": operation.input_kind,
            "operation_type": operation.operation_type,
            "idempotency_key": operation.idempotency_key,
            "idempotency_payload_hash": idempotency_payload_hash,
            "priority": operation.priority,
            "payload": json.dumps(operation.payload.model_dump(mode="json"), ensure_ascii=False),
            "trace_id": operation.trace_id,
            "graph_thread_id": operation.graph_thread_id,
            "occurred_at": operation.occurred_at,
            "max_attempts": max_attempts_for_priority(operation.priority),
            "status": status,
            "next_run_at": next_run_at,
        },
    )
    return rowcount == 1


async def get_by_idempotency(
    session: AsyncSession, *, user_id: UUID, actor_type: str, idempotency_key: str
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT * FROM memory_operations "
            "WHERE user_id = :user_id AND actor_type = :actor_type "
            "AND idempotency_key = :idempotency_key"
        ),
        {"user_id": user_id, "actor_type": actor_type, "idempotency_key": idempotency_key},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def get_operation(session: AsyncSession, operation_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM memory_operations WHERE operation_id = :operation_id"),
        {"operation_id": operation_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def claim_operation(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    operation_id: UUID | None = None,
    batch_size: int = 10,
) -> list[dict[str, Any]]:
    """领取 operation 并设置 Lease，必须在同一数据库事务中完成（§14.2）。

    operation_id 为 None 时按 priority DESC, created_at ASC 批量领取。
    """
    if operation_id is not None:
        select_sql = text(
            """
            SELECT operation_id FROM memory_operations
            WHERE operation_id = :operation_id
              AND status IN ('queued', 'retry_wait')
              AND next_run_at <= now()
            FOR UPDATE SKIP LOCKED
            """
        )
        params: dict[str, Any] = {"operation_id": operation_id}
    else:
        select_sql = text(
            """
            SELECT operation_id FROM memory_operations
            WHERE status IN ('queued', 'retry_wait')
              AND next_run_at <= now()
            ORDER BY priority DESC, created_at ASC
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
            """
        )
        params = {"batch_size": batch_size}
    rows = (await session.execute(select_sql, params)).scalars().all()
    if not rows:
        return []
    lease_expires = datetime.now(UTC) + timedelta(seconds=lease_seconds)
    await session.execute(
        text(
            """
            UPDATE memory_operations
            SET status = 'running', locked_by = :worker_id,
                lease_expires_at = :lease_expires, started_at = now(),
                last_heartbeat_at = now(), attempt_count = attempt_count + 1,
                lease_generation = lease_generation + 1,
                updated_at = now()
            WHERE operation_id = ANY(:ids)
            """
        ),
        {"worker_id": worker_id, "lease_expires": lease_expires, "ids": list(rows)},
    )
    claimed: list[dict[str, Any]] = []
    for op_id in rows:
        row = await get_operation(session, op_id)
        if row:
            claimed.append(row)
    return claimed


async def heartbeat(
    session: AsyncSession,
    *,
    operation_id: UUID,
    worker_id: str,
    lease_seconds: int,
    generation: int,
) -> bool:
    """续约 Lease（评审 #7）：按 operation_id + locked_by + generation CAS。

    generation 是 claim 时递增的 fencing token；Lease 被回收并由其他 Worker
    重新领取后，旧持有者的续约必然失败（rowcount 0）。
    """
    rowcount = await exec_rowcount(
        session,
        text(
            """
            UPDATE memory_operations
            SET last_heartbeat_at = now(),
                lease_expires_at = :lease_expires, updated_at = now()
            WHERE operation_id = :operation_id AND locked_by = :worker_id
              AND lease_generation = :generation
              AND status = 'running'
            """
        ),
        {
            "operation_id": operation_id,
            "worker_id": worker_id,
            "generation": generation,
            "lease_expires": datetime.now(UTC) + timedelta(seconds=lease_seconds),
        },
    )
    return rowcount == 1


async def mark_commit_started(
    session: AsyncSession,
    *,
    operation_id: UUID,
    expected_worker: str | None = None,
    expected_generation: int | None = None,
) -> bool:
    """进入 commit 副作用前打标记（§11.6 取消仲裁 + 评审二轮 #3 fencing CAS）。

    携带 fencing token 时按 (operation_id, locked_by, lease_generation,
    status='running') CAS：Lease 易主后旧持有者的标记必然失败（返回 False），
    调用方必须终止执行、不得进入 commit_plans 的业务提交路径。
    必须在独立短事务中调用，与提交事务分离：进程在 commit 中崩溃时标记残留，
    由 Lease 回收后新持有者在执行开始时清除（clear_commit_started）。
    """
    if expected_worker is not None and expected_generation is not None:
        rowcount = await exec_rowcount(
            session,
            text(
                """
                UPDATE memory_operations SET commit_started_at = now(), updated_at = now()
                WHERE operation_id = :operation_id AND locked_by = :expected_worker
                  AND lease_generation = :expected_generation AND status = 'running'
                """
            ),
            {
                "operation_id": operation_id,
                "expected_worker": expected_worker,
                "expected_generation": expected_generation,
            },
        )
        return rowcount == 1
    # 无 fencing 的直调路径（测试/内部维护）：保持 §11.6 原语义
    rowcount = await exec_rowcount(
        session,
        text(
            "UPDATE memory_operations SET commit_started_at = now(), updated_at = now() "
            "WHERE operation_id = :operation_id AND status = 'running'"
        ),
        {"operation_id": operation_id},
    )
    return rowcount == 1


async def clear_commit_started(
    session: AsyncSession,
    *,
    operation_id: UUID,
    expected_worker: str | None = None,
    expected_generation: int | None = None,
) -> bool:
    """清除 commit 标记（§11.6 + 评审二轮 #3 fencing CAS）。

    携带 fencing token 时按 (operation_id, locked_by, lease_generation,
    status='running') CAS：不论标记是否存在，只要仍是当前持有者即返回 True；
    Lease 易主后旧持有者的迟到清除必然失败（返回 False），不得覆盖新持有者
    刚设置的标记。无 fencing 的直调路径保持原语义（仅在有标记时清除）。
    """
    if expected_worker is not None and expected_generation is not None:
        # 单条 CAS：持有 Lease 即匹配（无论标记是否存在，rowcount 按匹配行计数）；
        # Lease 易主后旧持有者的迟到清除必然 rowcount=0
        rowcount = await exec_rowcount(
            session,
            text(
                """
                UPDATE memory_operations SET commit_started_at = NULL, updated_at = now()
                WHERE operation_id = :operation_id AND locked_by = :expected_worker
                  AND lease_generation = :expected_generation AND status = 'running'
                """
            ),
            {
                "operation_id": operation_id,
                "expected_worker": expected_worker,
                "expected_generation": expected_generation,
            },
        )
        return rowcount == 1
    await session.execute(
        text(
            "UPDATE memory_operations SET commit_started_at = NULL, updated_at = now() "
            "WHERE operation_id = :operation_id AND commit_started_at IS NOT NULL"
        ),
        {"operation_id": operation_id},
    )
    return True


async def complete_operation(
    session: AsyncSession,
    *,
    operation_id: UUID,
    status: str,
    result: dict[str, Any] | None,
    public_error: dict[str, Any] | None,
    llm_call_count: int = 0,
    expected_worker: str,
    expected_generation: int,
) -> bool:
    """终态写回（评审 #7）：fencing CAS，旧 lease 持有者的迟到写回失败。

    按 operation_id + locked_by + lease_generation + status='running' 更新并
    检查 rowcount；返回是否真正写入。Lease 已易主时返回 False，调用方必须
    丢弃结果而不是覆盖新持有者状态。
    """
    rowcount = await exec_rowcount(
        session,
        text(
            """
            UPDATE memory_operations
            SET status = :status, result = CAST(:result AS jsonb),
                public_error = CAST(:public_error AS jsonb),
                completed_at = now(), updated_at = now(),
                locked_by = NULL, lease_expires_at = NULL,
                commit_started_at = NULL,
                llm_call_count = llm_call_count + :llm_call_count
            WHERE operation_id = :operation_id
              AND locked_by = :expected_worker
              AND lease_generation = :expected_generation
              AND status = 'running'
            """
        ),
        {
            "operation_id": operation_id,
            "status": status,
            "result": json.dumps(result, ensure_ascii=False) if result is not None else None,
            "public_error": (
                json.dumps(public_error, ensure_ascii=False) if public_error is not None else None
            ),
            "llm_call_count": llm_call_count,
            "expected_worker": expected_worker,
            "expected_generation": expected_generation,
        },
    )
    if rowcount != 1:
        return False
    # memory-rebuild §2.6 状态机第 3/4 步：批次终态在同一事务内回写成员状态
    # （succeeded→成员 succeeded；dead_letter/needs_review→成员 dead_letter；
    # cancelled→释放成员回证据池）。非批次 operation 走这里恒为 0 行，代价是一次
    # 命中 ix_memory_operations_batch_operation 的空扫描。
    await settle_batch_members(session, batch_operation_id=operation_id, status=status)
    return True


async def reschedule_operation(
    session: AsyncSession,
    *,
    operation_id: UUID,
    next_run_at: datetime,
    status: str,
    expected_worker: str,
    expected_generation: int,
) -> bool:
    """任务级重试退避后重新排队（§11.2 / 评审 #7 fencing CAS）。

    同 complete_operation：旧 lease 持有者的迟到重排返回 False，不得覆盖
    新持有者已写入的状态。
    """
    rowcount = await exec_rowcount(
        session,
        text(
            """
            UPDATE memory_operations
            SET status = :status, next_run_at = :next_run_at, updated_at = now(),
                locked_by = NULL, lease_expires_at = NULL, commit_started_at = NULL
            WHERE operation_id = :operation_id
              AND locked_by = :expected_worker
              AND lease_generation = :expected_generation
              AND status = 'running'
            """
        ),
        {
            "operation_id": operation_id,
            "status": status,
            "next_run_at": next_run_at,
            "expected_worker": expected_worker,
            "expected_generation": expected_generation,
        },
    )
    return rowcount == 1


async def recover_expired_leases(session: AsyncSession) -> int:
    """Scheduler 回收过期 Lease：running → retry_wait（§14.3）。"""
    rowcount = await exec_rowcount(
        session,
        text(
            """
            UPDATE memory_operations
            SET status = 'retry_wait', next_run_at = now(), updated_at = now(),
                locked_by = NULL, lease_expires_at = NULL
            WHERE status = 'running' AND lease_expires_at < now()
            """
        ),
    )
    return rowcount


async def request_cancel(session: AsyncSession, *, operation_id: UUID) -> dict[str, Any] | None:
    """取消规则（§11.6）。返回更新后的行；不可取消返回 None（调用方区分 409）。

    批次 operation 被立即取消时（``queued`` / ``retry_wait`` / ``pending_batch`` /
    ``needs_review``），**同一事务内**把成员释放回证据池（review I-8）：不释放的话成员
    会停在 ``pending_batch AND batch_operation_id IS NOT NULL``——批次已取消不再处理它，
    证据池扫描又只取 ``batch_operation_id IS NULL``，于是永久搁浅，`_sweep_batch_runs`
    还会把该 run 收尾成 succeeded 掩盖问题。释放语义直接复用
    :func:`settle_batch_members`（成员状态仍 ``pending_batch``、归属置 NULL），不写第二套。
    """
    row = await get_operation(session, operation_id)
    if row is None:
        return None
    status = row["status"]
    immediate_cancel = False
    if status in ("queued", "retry_wait", "pending_batch"):
        # pending_batch 与 queued/retry_wait 同属"尚未开始执行"的在途态
        # （memory-rebuild §2.6）：账号删除必须能取消它，否则待批量证据
        # 会逃过删除合规（§21.3）。
        await session.execute(
            text(
                """
                UPDATE memory_operations
                SET status = 'cancelled', completed_at = now(), updated_at = now(),
                    locked_by = NULL, lease_expires_at = NULL
                WHERE operation_id = :operation_id
                """
            ),
            {"operation_id": operation_id},
        )
        immediate_cancel = True
    elif status == "running":
        if row.get("commit_started_at") is not None:
            # 已进入 commit 副作用，不允许取消（§11.6，裁决 2026-08-11）
            raise OperationCancelNotAllowedError(
                "operation 已进入 commit，不允许取消", field="status"
            )
        # 协作取消：Runner 在节点入口/commit 前检查 cancel_requested_at。
        # running 的批次**不在此处释放成员**：它在跑，成员仍归它；终态取消由
        # complete_operation(status='cancelled') 走同一套 settle。
        await session.execute(
            text(
                "UPDATE memory_operations SET cancel_requested_at = now(), "
                "updated_at = now() WHERE operation_id = :operation_id"
            ),
            {"operation_id": operation_id},
        )
    elif status == "needs_review":
        await session.execute(
            text(
                """
                UPDATE memory_operations
                SET status = 'cancelled', completed_at = now(), updated_at = now()
                WHERE operation_id = :operation_id
                """
            ),
            {"operation_id": operation_id},
        )
        immediate_cancel = True
    else:
        return None
    if immediate_cancel:
        # 非批次 operation 恒为 0 行（WHERE 命中不到成员）；批次则把成员放回池子
        await settle_batch_members(session, batch_operation_id=operation_id, status="cancelled")
    return await get_operation(session, operation_id)


async def get_cancel_requested(session: AsyncSession, operation_id: UUID) -> bool:
    result = await session.execute(
        text(
            "SELECT cancel_requested_at IS NOT NULL FROM memory_operations "
            "WHERE operation_id = :operation_id"
        ),
        {"operation_id": operation_id},
    )
    return bool(result.scalar())


async def list_user_operations(
    session: AsyncSession, *, user_id: UUID, operation_id: UUID
) -> dict[str, Any] | None:
    """用户只能访问自己的 operation（§19.3）。"""
    result = await session.execute(
        text(
            "SELECT * FROM memory_operations "
            "WHERE operation_id = :operation_id AND user_id = :user_id"
        ),
        {"operation_id": operation_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# 证据池批次（memory-rebuild §2.6 / §5.8）
# ---------------------------------------------------------------------------

#: 证据沉淀态（§2.6 D4）：已提交但未到最短沉淀时长，等待 0 点批量入批。
PENDING_BATCH_STATUS = "pending_batch"

#: 批量 operation 类型（与 contracts/batch.py 的 BATCH_OPERATION_TYPE 同值）。
BATCH_OPERATION_TYPE = "summarize_user_memory_batch"


async def list_pending_batch_user_ids(
    session: AsyncSession, *, now: datetime, limit: int
) -> list[UUID]:
    """列出有可入批证据的用户（按最早到点时间排序，保证多副本/续跑下顺序稳定）。

    只取 ``batch_operation_id IS NULL`` 的行：已归属某个存活批次的证据不会被第二个批次
    领走（§5.8「每条成员 evidence 只关联一个批次」）。排序键与批次内成员排序一致，
    因此"先到点的用户先处理"是确定性的，不依赖应用内存集合。
    """
    result = await session.execute(
        text(
            """
            SELECT user_id
            FROM memory_operations
            WHERE status = :status
              AND next_run_at <= :now
              AND batch_operation_id IS NULL
            GROUP BY user_id
            ORDER BY MIN(next_run_at) ASC, user_id ASC
            LIMIT :limit
            """
        ),
        {"status": PENDING_BATCH_STATUS, "now": now, "limit": limit},
    )
    return [UUID(str(row[0])) for row in result.all()]


async def list_pending_batch_members(
    session: AsyncSession,
    *,
    user_id: UUID,
    now: datetime,
    limit: int,
    after: tuple[datetime, datetime, UUID] | None = None,
) -> list[dict[str, Any]]:
    """按稳定序取该用户可入批的证据行（§5.8：排序键 eligible/created/operation_id）。

    ``after`` 是该用户上一次批次消费到的位置（三元组），用于续跑时**严格**跳过已入批
    的行——不用 OFFSET，避免并发插入导致的漂移。
    """
    params: dict[str, Any] = {
        "status": PENDING_BATCH_STATUS,
        "now": now,
        "user_id": user_id,
        "limit": limit,
    }
    cursor_clause = ""
    if after is not None:
        eligible_at, created_at, operation_id = after
        params.update(
            {
                "after_eligible": eligible_at,
                "after_created": created_at,
                "after_operation": operation_id,
            }
        )
        # 行值比较：三级排序键整体大于游标（SQL 标准行构造器，PostgreSQL 原生支持）
        cursor_clause = (
            "AND (next_run_at, created_at, operation_id) > "
            "(:after_eligible, :after_created, :after_operation)"
        )
    result = await session.execute(
        text(
            f"""
            SELECT operation_id, user_id, next_run_at, created_at
            FROM memory_operations
            WHERE status = :status
              AND next_run_at <= :now
              AND batch_operation_id IS NULL
              AND user_id = :user_id
              {cursor_clause}
            ORDER BY next_run_at ASC, created_at ASC, operation_id ASC
            LIMIT :limit
            """
        ),
        params,
    )
    return [dict(row) for row in result.mappings().all()]


async def assign_batch_members(
    session: AsyncSession, *, batch_operation_id: UUID, member_operation_ids: list[UUID]
) -> int:
    """把成员证据归属到批次（状态保持 ``pending_batch``，§2.6 状态机第 2 步）。

    带 ``batch_operation_id IS NULL AND status='pending_batch'`` 守卫：并发下另一个
    Scheduler 实例已领走同一批证据时，这里只会写回更少的行，调用方按 rowcount 判定
    是否真的拿到了这批（拿到 0 行说明整批被抢，应当作废刚建的批次 operation）。
    """
    if not member_operation_ids:
        return 0
    rowcount = await exec_rowcount(
        session,
        text(
            """
            UPDATE memory_operations
            SET batch_operation_id = :batch_operation_id, updated_at = now()
            WHERE operation_id = ANY(:ids)
              AND status = :status
              AND batch_operation_id IS NULL
            """
        ),
        {
            "batch_operation_id": batch_operation_id,
            "ids": list(member_operation_ids),
            "status": PENDING_BATCH_STATUS,
        },
    )
    return rowcount


async def settle_batch_members(
    session: AsyncSession, *, batch_operation_id: UUID, status: str
) -> int:
    """批次终态回写成员（§2.6 状态机第 3/4 步），返回受影响成员数。

    语义（与 ``contracts/batch.py::BATCH_MEMBER_TRANSITIONS`` 一致）：

    - ``succeeded`` → 成员 ``succeeded``（同一事务，成功即成员完成）；
    - ``dead_letter`` / ``needs_review`` → 成员 ``dead_letter``（批次进人工审，
      成员不能再被自动重排，避免与人工结论冲突）；
    - ``cancelled`` → **释放**成员（``status`` 仍为 ``pending_batch``、
      ``batch_operation_id`` 置 NULL），让它回到证据池等待下一个批次；
    - 其它（``running``/``retry_wait`` 等非终态）不动成员：批次会自行重试。

    非批次 operation 调用本函数恒为 0 行（WHERE 命中不到任何成员）。
    """
    if status == "cancelled":
        return await exec_rowcount(
            session,
            text(
                """
                UPDATE memory_operations
                SET batch_operation_id = NULL, updated_at = now()
                WHERE batch_operation_id = :batch_operation_id
                  AND status = :status
                """
            ),
            {"batch_operation_id": batch_operation_id, "status": PENDING_BATCH_STATUS},
        )
    member_status = {
        "succeeded": "succeeded",
        "dead_letter": "dead_letter",
        "needs_review": "dead_letter",
    }.get(status)
    if member_status is None:
        return 0
    return await exec_rowcount(
        session,
        text(
            """
            UPDATE memory_operations
            SET status = :member_status, updated_at = now()
            WHERE batch_operation_id = :batch_operation_id
              AND status = :status
            """
        ),
        {
            "batch_operation_id": batch_operation_id,
            "member_status": member_status,
            "status": PENDING_BATCH_STATUS,
        },
    )


async def list_batch_member_operations(
    session: AsyncSession, *, batch_operation_id: UUID
) -> list[dict[str, Any]]:
    """读取**可处理**的批次成员（供批量图逐条处理）；按稳定序返回，保证可重放。

    review I-8：只返回 ``status='pending_batch'`` 的成员。``request_cancel`` 明确允许
    取消处于 ``pending_batch`` 的证据，被取消的成员行状态是 ``cancelled``，但**归属字段
    仍在**——若这里不过滤，批次会把它照常捞出来处理，用户取消掉的证据照样写进长期记忆。
    这同时是 ``begin_batch_member`` 的防线：它拿到的成员一定处于可写（未被取消/未终结）态。
    """
    result = await session.execute(
        text(
            """
            SELECT *
            FROM memory_operations
            WHERE batch_operation_id = :batch_operation_id
              AND status = :status
            ORDER BY next_run_at ASC, created_at ASC, operation_id ASC
            """
        ),
        {"batch_operation_id": batch_operation_id, "status": PENDING_BATCH_STATUS},
    )
    return [dict(row) for row in result.mappings().all()]


async def list_operations_by_ids(
    session: AsyncSession, *, operation_ids: list[UUID]
) -> list[dict[str, Any]]:
    """按 operation_id 集合批量取行（不分状态）。

    用途（评审新发现 14）：批次收尾要把"payload 声明了但本批不处理"的成员分成两类——
    行还在（已取消/已释放/已终态）与行不存在（真·归属丢失）。前者是正常语义，后者才是
    数据不一致，措辞与告警级别都不同，因此需要一次"不管状态"的回查。
    """
    if not operation_ids:
        return []
    result = await session.execute(
        text("SELECT * FROM memory_operations WHERE operation_id = ANY(:operation_ids)"),
        {"operation_ids": list(operation_ids)},
    )
    return [dict(row) for row in result.mappings().all()]


async def count_pending_batch_evidence(session: AsyncSession, *, now: datetime) -> tuple[int, int]:
    """返回 (待入批总数, 已到点待入批数)，供 gauge 使用（§2.6）。

    "已到点"= ``next_run_at <= now`` 且尚未归属任何批次——即下一次 0 点扫描真正会认领的
    量；两者之差就是仍在沉淀窗口里的量。
    """
    result = await session.execute(
        text(
            """
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE next_run_at <= :now) AS due
            FROM memory_operations
            WHERE status = :status
              AND batch_operation_id IS NULL
            """
        ),
        {"status": PENDING_BATCH_STATUS, "now": now},
    )
    row = result.mappings().one()
    return int(row["total"]), int(row["due"])


async def release_batch_member(
    session: AsyncSession, *, operation_id: UUID
) -> BatchMemberReleaseOutcome:
    """把一个"处理失败"的成员释放回证据池（review I-9 的重试出口）。

    批次 `succeeded` 时 `settle_batch_members` 会把**所有**归属成员置 succeeded——包括被
    成员级隔离捕获、实际没写成功的那些。那样它们永远不会再被处理。因此批量图在收尾前
    先对失败成员调用本函数：清掉 `batch_operation_id`（状态仍是 `pending_batch`），
    于是 `settle_batch_members` 的 `WHERE batch_operation_id = :batch` 自然不再命中它，
    它回到池子里等下一个批次；`next_run_at` 推后到 now()，避免立刻被同一个 run 重新领走
    形成忙循环（真正的重试节奏由 0 点批量任务决定）。

    **尝试计数与终态出口**（评审新发现 13）：成员从未被 claim，`claim_operation` 里那句
    `attempt_count = attempt_count + 1` 对它永远不生效，因此旧实现的"失败 → 释放"是一条
    没有计数、没有终点的环：确定性失败的证据会每晚重新入批、每晚失败、每晚再被释放，
    持续烧 LLM 预算直到人工介入。这里在释放的同一条 UPDATE 里把成员自己的
    `attempt_count` 加一（复用既有列，**不需要新迁移**），并在达到该行自己的
    `max_attempts`（证据类 operation 是 P2 → 4 次）时不再释放，而是转 `dead_letter`
    交人工审核——与任务级重试上限同一把尺子。

    返回 :class:`BatchMemberReleaseOutcome`：调用方据此区分"已释放回池子"
    "已升级 dead_letter" "没命中（不是本批的可写成员）"。
    """
    result = await session.execute(
        text(
            """
            WITH bumped AS (
                SELECT operation_id, attempt_count + 1 AS attempt_count, max_attempts
                FROM memory_operations
                WHERE operation_id = :operation_id
                  AND status = :status
                  AND batch_operation_id IS NOT NULL
                FOR UPDATE
            )
            UPDATE memory_operations AS op
            SET attempt_count = b.attempt_count,
                status = CASE
                    WHEN b.attempt_count >= b.max_attempts THEN 'dead_letter'
                    ELSE op.status
                END,
                -- dead_letter 时**保留**归属：保住"它死在哪一批"的可追溯性；
                -- 释放成功时必须清空，否则 settle_batch_members 会把它一起置 succeeded。
                batch_operation_id = CASE
                    WHEN b.attempt_count >= b.max_attempts THEN op.batch_operation_id
                    ELSE NULL
                END,
                public_error = CASE
                    WHEN b.attempt_count >= b.max_attempts
                        THEN CAST(:public_error AS jsonb)
                    ELSE op.public_error
                END,
                completed_at = CASE
                    WHEN b.attempt_count >= b.max_attempts THEN now()
                    ELSE op.completed_at
                END,
                next_run_at = CASE
                    WHEN b.attempt_count >= b.max_attempts THEN op.next_run_at
                    ELSE now()
                END,
                updated_at = now()
            FROM bumped AS b
            WHERE op.operation_id = b.operation_id
            RETURNING op.status, op.attempt_count
            """
        ),
        {
            "operation_id": operation_id,
            "status": PENDING_BATCH_STATUS,
            "public_error": json.dumps(
                {
                    "code": "OPERATION_DEAD_LETTER",
                    "message": (
                        f"证据 operation {operation_id} 连续多批处理失败，已达 max_attempts，"
                        "转入人工审核（不再自动重新入批）"
                    ),
                    "retryable": False,
                },
                ensure_ascii=False,
            ),
        },
    )
    row = result.mappings().first()
    if row is None:
        return BatchMemberReleaseOutcome(status=BATCH_MEMBER_NOT_FOUND, attempt_count=0)
    attempt_count = int(row["attempt_count"])
    if str(row["status"]) == "dead_letter":
        return BatchMemberReleaseOutcome(
            status=BATCH_MEMBER_DEAD_LETTERED, attempt_count=attempt_count
        )
    return BatchMemberReleaseOutcome(status=BATCH_MEMBER_RELEASED, attempt_count=attempt_count)
