"""Maintenance 分支（§10.7）：有界 batch + cursor，不长时间持锁。

步骤 9 实现 rebuild_index / purge_tombstones / cleanup_orphan_versions；
步骤 10 接入 cleanup_checkpoints（CheckpointCleanupAdapter，§11.4）；
purge_account_memory 走 account_purge 服务（§13.16/§21.3，评审 P0-1 修复）；
verify_checksums（§14.3，每天 04:00）：校验活动版本 checksum 与解析合法性、
current/ 物化副本与活动版本一致性（漂移时重新物化），损坏项告警并记入 run detail。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.runtime import Runtime
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.contracts.commands import MaintenanceCommand
from backend.memory.contracts.errors import InvalidPayloadError
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.graph.state import MemoryManagerState, MemoryRuntimeContext
from backend.memory.persistence import documents as docs_repo
from backend.memory.persistence import graph_states as graph_states_repo
from backend.memory.persistence import maintenance as maintenance_repo
from backend.memory.persistence.database import exec_rowcount
from backend.memory.storage.base import sha256_hex
from backend.memory.storage.markdown_schema import (
    SCHEMA_VERSION_V2,
    MarkdownParseError,
    document_links,
    parse_learner,
    parse_mastery,
    render_learner,
    render_mastery,
)

logger = logging.getLogger("memory.maintenance")


def _first_line(candidates: list[str]) -> str:
    """取首个非空内容并压成单行（description 必须单行）。"""
    for item in candidates:
        text = " ".join(str(item).split())
        if text:
            return text[:200]
    return ""


def _upgrade_to_schema_v2(*, memory_type: str, text: str) -> str | None:
    """把 v1 文档文本机械升级为 v2；已是 v2 返回 None（幂等）。

    **只做机械投影、不调用 LLM**（§2.7 决议 A 组："迁移 = 后台维护任务把 current/ 按
    v2 重新渲染并走正常 write_immutable_version 追加新版本"）：
    ``name`` 取既有标题，``description`` 取既有概述/偏好的首行，``aliases`` 留空，
    ``links`` 从正文 `[[...]]` 现算。更准确的 description/aliases 由 planner 后续用
    ``frontmatter_patch`` 补——迁移只保证"能读、格式合规"。
    """
    if memory_type == "learner":
        learner = parse_learner(text)
        if learner.schema_version >= SCHEMA_VERSION_V2:
            return None
        learner.name = learner.name or "学习者档案"
        learner.description = (
            learner.description
            or _first_line([*learner.goals, *learner.preferences])
            or "学习偏好与目标"
        )
        learner.links = learner.links or document_links(
            learner.preferences, learner.goals, learner.plans
        )
        learner.schema_version = SCHEMA_VERSION_V2
        return render_learner(learner)
    if memory_type == "mastery":
        mastery = parse_mastery(text)
        if mastery.schema_version >= SCHEMA_VERSION_V2:
            return None
        mastery.name = mastery.name or mastery.topic_title
        mastery.description = (
            mastery.description or _first_line([mastery.overview]) or f"主题：{mastery.topic_title}"
        )
        mastery.links = mastery.links or document_links(
            mastery.overview, mastery.understood, mastery.difficulties, mastery.review_advice
        )
        mastery.schema_version = SCHEMA_VERSION_V2
        return render_mastery(mastery)
    return None


async def run_maintenance(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """validate_maintenance_command → acquire_scheduler_lock → execute_bounded_batch
    → persist_cursor_or_finish → emit_maintenance_metric。"""
    ctx = runtime.context
    operation = MemoryOperation.model_validate(state["operation"])
    payload = operation.payload
    assert isinstance(payload, MaintenanceCommand)
    kind = payload.kind

    # acquire_scheduler_lock：每类维护任务全局互斥（§14.3）。
    # 复审修复（生产静默失效）：改用事务级 pg_try_advisory_xact_lock——
    # 锁随事务提交/回滚自动释放，天然免疫"session 级锁 + 连接归还连接池后
    # unlock 落在另一条连接"的泄漏；泄漏会使同类型维护任务此后永久返回
    # busy 且无任何报错。lock/unlock 现在必然在同一事务边界内成对生效。
    async with ctx.session_factory() as session:
        async with session.begin():
            result = await session.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:name))"),
                {"name": f"maintenance:{kind}"},
            )
            acquired = bool(result.scalar_one())
            if not acquired:
                detail = {"kind": kind, "status": "busy"}
                # busy 也回写 run（保持 running），由 Scheduler 稍后重排同 cursor 批次。
                # 复审 P3：专用 busy 更新不触碰 cursor——避免覆盖持锁实例刚提交的
                # 新 cursor（last-writer-wins 回退导致同批次重跑）
                await maintenance_repo.mark_run_busy_by_operation(
                    session,
                    operation_id=operation.operation_id,
                    result=detail,
                )
                return {
                    "graph_state_result": {"maintenance": detail},
                    "warnings": [*state.get("warnings", []), f"维护任务 {kind} 正在其他实例运行"],
                }
            detail = await _execute_batch(ctx, operation, payload, session)
            # persist_cursor_or_finish：回写 maintenance run（§10.7 / §14.3）
            await maintenance_repo.update_run_by_operation(
                session,
                operation_id=operation.operation_id,
                status="running" if detail.get("status") == "continue" else "succeeded",
                cursor=detail.get("next_cursor"),
                result=detail,
            )
    # 事务已提交（或异常回滚）→ 事务级 advisory lock 自动释放
    return {"graph_state_result": {"maintenance": detail}}


async def _execute_batch(
    ctx: MemoryRuntimeContext,
    operation: MemoryOperation,
    payload: MaintenanceCommand,
    session: AsyncSession,
) -> dict[str, Any]:
    kind = payload.kind
    now = ctx.clock.now()
    store = ctx.memory_service.store
    if kind == "rebuild_index":
        target = payload.target_user_id or operation.user_id
        result = await ctx.memory_service.rebuild_index(
            user_id=target, operation_id=operation.operation_id
        )
        return {"kind": kind, "status": "done", "result": result}

    if kind == "cleanup_checkpoints":
        from backend.memory.worker.checkpoint import (
            list_expired_checkpoint_threads,
            list_orphan_checkpoint_threads,
        )

        if ctx.checkpoint_cleanup is None:
            raise InvalidPayloadError(
                "cleanup_checkpoints 需要 Runtime Context 配置 CheckpointCleanupAdapter"
            )
        rows = await list_expired_checkpoint_threads(
            session, now=now, batch_size=payload.batch_size, cursor=payload.cursor
        )
        deleted = 0
        next_cursor = None
        for row in rows:
            next_cursor = row["cursor"]
            if payload.dry_run:
                continue
            deleted += await ctx.checkpoint_cleanup.delete_threads([row["thread_id"]])
        finished = len(rows) < payload.batch_size
        # 到期扫描完成后，用剩余配额清扫孤儿线程（账号删除自身线程等遗留）
        orphans_deleted = 0
        if finished and not payload.dry_run:
            orphans = await list_orphan_checkpoint_threads(
                session, batch_size=payload.batch_size - len(rows)
            )
            for thread_id in orphans:
                orphans_deleted += await ctx.checkpoint_cleanup.delete_threads([thread_id])
        return {
            "kind": kind,
            "status": "done" if finished else "continue",
            "scanned": len(rows),
            "threads_deleted": deleted,
            "orphan_threads_deleted": orphans_deleted,
            "dry_run": payload.dry_run,
            "next_cursor": None if finished else next_cursor,
        }

    if kind == "purge_tombstones":
        rows = await docs_repo.list_expired_tombstones(
            session, now=now, batch_size=payload.batch_size, cursor=payload.cursor
        )
        purged = 0
        next_cursor = None
        for row in rows:
            next_cursor = f"{row['user_id']}:{row['memory_id']}"
            if payload.dry_run:
                continue
            await store.purge_quarantined(user_id=row["user_id"], memory_id=row["memory_id"])
            orphans = await store.list_orphan_versions(
                user_id=row["user_id"],
                memory_id=row["memory_id"],
                referenced_checksums=set(),
            )
            for key in orphans:
                await store.delete_version_file(user_id=row["user_id"], storage_key=key)
            await exec_rowcount(
                session,
                text(
                    "DELETE FROM memory_documents "
                    "WHERE user_id = :u AND memory_id = :m AND deleted_at IS NOT NULL"
                ),
                {"u": row["user_id"], "m": row["memory_id"]},
            )
            purged += 1
        finished = len(rows) < payload.batch_size
        return {
            "kind": kind,
            "status": "done" if finished else "continue",
            "scanned": len(rows),
            "purged": purged,
            "dry_run": payload.dry_run,
            "next_cursor": None if finished else next_cursor,
        }

    if kind == "cleanup_orphan_versions":
        if payload.target_user_id is None:
            raise InvalidPayloadError("cleanup_orphan_versions 需要 target_user_id")
        target = payload.target_user_id
        docs = await docs_repo.list_active_documents(session, user_id=target)
        from sqlalchemy import text as sql_text

        removed = 0
        for doc in docs[: payload.batch_size]:
            checksum_rows = await session.execute(
                sql_text(
                    "SELECT checksum FROM memory_commits "
                    "WHERE user_id = :u AND memory_id = :m AND checksum IS NOT NULL"
                ),
                {"u": target, "m": doc["memory_id"]},
            )
            referenced = {str(r[0]) for r in checksum_rows.all()}
            orphans = await store.list_orphan_versions(
                user_id=target,
                memory_id=doc["memory_id"],
                referenced_checksums=referenced,
            )
            for key in orphans:
                if not payload.dry_run:
                    await store.delete_version_file(user_id=target, storage_key=key)
                removed += 1
        return {
            "kind": kind,
            "status": "done",
            "documents_scanned": min(len(docs), payload.batch_size),
            "orphans_removed": removed,
            "dry_run": payload.dry_run,
        }

    if kind == "purge_account_memory":
        # §21.3 / §13.16：账号物理删除全流程（评审 P0-1 修复）。
        # 注意：purge 会删除自身的 memory_operations/memory_maintenance_runs 行，
        # 外层 update_run_by_operation 与 complete_operation 均为静默 no-op。
        purge_target = payload.target_user_id
        if purge_target is None:
            raise InvalidPayloadError("purge_account_memory 需要 target_user_id")
        from backend.memory.services.account_purge import (
            AccountPurgeNotDrainedError,
            drain_user_operations,
            purge_user_account,
        )

        async with ctx.session_factory() as drain_session:
            async with drain_session.begin():
                running = await drain_user_operations(
                    drain_session,
                    user_id=purge_target,
                    exclude_operation_id=operation.operation_id,
                )
        if running > 0:
            # 可重试错误：执行层按退避重排，等待运行中任务退出（§21.3 步骤 3）
            raise AccountPurgeNotDrainedError(f"账号删除等待 {running} 个运行中用户任务结束")
        if payload.dry_run:
            return {"kind": kind, "status": "done", "dry_run": True}
        summary = await purge_user_account(
            ctx.session_factory,
            settings=ctx.settings,
            store=ctx.memory_service.store,
            checkpoint_cleanup=ctx.checkpoint_cleanup,
            user_id=purge_target,
            account_deletion_id=None,
            now=now,
            self_operation_id=operation.operation_id,
        )
        return {
            "kind": kind,
            "status": "done",
            "purged_tables": summary.table_counts,
            "checkpoint_threads_deleted": summary.checkpoint_threads_deleted,
            "markdown_tree_deleted": summary.markdown_tree_deleted,
            "break_glass_compressed": summary.break_glass_compressed,
            "completion_proof_checksum": summary.completion_proof_checksum,
        }

    if kind == "verify_checksums":
        # §14.3：校验活动版本 checksum/解析合法性，以及 current/ 物化副本一致性；
        # 漂移副本按活动版本重新物化修复，损坏项告警并记入 detail。
        rows = await docs_repo.list_active_documents_page(
            session, batch_size=payload.batch_size, cursor=payload.cursor
        )
        checked = 0
        rematerialized = 0
        corrupted: list[dict[str, Any]] = []
        next_cursor = None
        for row in rows:
            next_cursor = f"{row['user_id']}:{row['memory_id']}"
            checked += 1
            issue: dict[str, Any] = {
                "user_id": str(row["user_id"]),
                "memory_id": row["memory_id"],
                "reasons": [],
            }
            try:
                content = await store.read_version(
                    user_id=row["user_id"], storage_key=row["active_storage_key"]
                )
            except FileNotFoundError:
                issue["reasons"].append("active_version_missing")
                content = None
            trusted = content is not None
            if content is not None:
                if sha256_hex(content) != row["active_checksum"]:
                    issue["reasons"].append("checksum_mismatch")
                    trusted = False
                if row["memory_type"] == "learner":
                    try:
                        parse_learner(content.decode("utf-8"))
                    except (MarkdownParseError, UnicodeDecodeError):
                        issue["reasons"].append("parse_failed")
                elif row["memory_type"] == "mastery":
                    try:
                        parse_mastery(content.decode("utf-8"))
                    except (MarkdownParseError, UnicodeDecodeError):
                        issue["reasons"].append("parse_failed")
            drift = False
            try:
                current = await store.read_current(
                    user_id=row["user_id"], memory_id=row["memory_id"]
                )
                if content is not None and current != content:
                    drift = True
            except FileNotFoundError:
                drift = True
            if drift and content is not None:
                issue["reasons"].append("current_drift")
                # 仅在校验和可信时重新物化，避免把损坏内容传播到 current/
                if trusted and not payload.dry_run:
                    await store.materialize_current(
                        user_id=row["user_id"], memory_id=row["memory_id"], content=content
                    )
                    rematerialized += 1
            if issue["reasons"]:
                corrupted.append(issue)
                logger.error(
                    "告警：verify_checksums 发现损坏 user_id=%s memory_id=%s reasons=%s",
                    row["user_id"],
                    row["memory_id"],
                    ",".join(issue["reasons"]),
                )
        finished = len(rows) < payload.batch_size
        return {
            "kind": kind,
            "status": "done" if finished else "continue",
            "checked": checked,
            "corrupted": corrupted,
            "rematerialized": rematerialized,
            "dry_run": payload.dry_run,
            "next_cursor": None if finished else next_cursor,
        }

    if kind == "migrate_markdown_schema_v2":
        # memory-rebuild §5.6：把 v1 活动文档**追加**一个 v2 版本并更新 current。
        # 铁律：versions/ 历史文件永不原地改写（§2.7 决议 A 组）——本处理器只调用
        # 正常 write_immutable_version 追加新版本，因此 checksum 级联不会失效、
        # 回滚语义不受损。index 文档由 rebuild_index 重新生成，不在此处理。
        #
        # 本处理器同时是**全量回填入口**（评审新发现 9）：`skipped_already_v2` 分支
        # 也刷新投影。理由：文档升到 v2 有两条路——本任务（v1→v2 追加版本）与
        # `frontmatter_patch` 原生升版；后者从不经过这里，早期版本的迁移也已经把
        # 它们跳过。若只在"本次升级了文档"的分支刷新，这些文档的 aliases/keywords/
        # related 永远补不上，运维"再跑一次任务"完全是 no-op。
        # 代价：每次运行都会对**已是 v2 的活动文档**重算并 upsert 一次投影、标一次
        # index dirty（不写新版本、不改正文、幂等）。批量与 cursor 有界，且本任务受
        # `memory_schema_v2_migration_enabled` 门控、每天最多建一个 run，因此代价可控；
        # 换来的是"无论何时跑迁移都能补齐投影"这一确定性。
        #
        # 链接版本同样在这里收敛（review-3 残留）：本处理器把文档版本 +1 却从不碰
        # `memory_graph_links`，链接会落后文档一格，而 KG overlay 读侧与双写都要求
        # "版本相等"，这段窗口里该主题的映射不可见。于是两个分支都在同一事务里调用
        # `align_active_graph_links`——它只推进已经 active 的行（没有映射的记忆是 no-op、
        # 已取消的映射不复活），顺带把存量 stale 数据也修好（重跑本任务即修复入口）。
        rows = await docs_repo.list_active_documents_page(
            session, batch_size=payload.batch_size, cursor=payload.cursor
        )
        migrated = 0
        skipped_already_v2 = 0
        refreshed_already_v2 = 0
        graph_links_aligned = 0
        failures: list[dict[str, Any]] = []
        next_cursor = None
        for row in rows:
            next_cursor = f"{row['user_id']}:{row['memory_id']}"
            if row["memory_type"] == "index":
                continue
            try:
                content = await store.read_version(
                    user_id=row["user_id"], storage_key=row["active_storage_key"]
                )
            except FileNotFoundError:
                failures.append(
                    {
                        "user_id": str(row["user_id"]),
                        "memory_id": row["memory_id"],
                        "reasons": ["active_version_missing"],
                    }
                )
                continue
            try:
                upgraded = _upgrade_to_schema_v2(
                    memory_type=row["memory_type"], text=content.decode("utf-8")
                )
            except (MarkdownParseError, UnicodeDecodeError) as exc:
                # 坏文档不阻塞同批其他用户（§5.6 迁移验收）
                failures.append(
                    {
                        "user_id": str(row["user_id"]),
                        "memory_id": row["memory_id"],
                        "reasons": [f"parse_failed:{type(exc).__name__}"],
                    }
                )
                continue
            if upgraded is None:
                # 已是 v2：不再产生新版本，但**投影仍要回填**（评审新发现 9）——
                # 本任务因此成为"无论何时跑都能补齐投影"的全量入口。
                skipped_already_v2 += 1
                if payload.dry_run:
                    continue
                try:
                    refreshed = await ctx.memory_service.refresh_index_projection(
                        session, user_id=row["user_id"], memory_id=row["memory_id"]
                    )
                except (MarkdownParseError, UnicodeDecodeError):
                    # 投影刷新失败不拖垮同批其他文档：只记账（正文本身没动）
                    failures.append(
                        {
                            "user_id": str(row["user_id"]),
                            "memory_id": row["memory_id"],
                            "reasons": ["projection_refresh_failed"],
                        }
                    )
                    continue
                if refreshed:
                    refreshed_already_v2 += 1
                # 本分支不改版本，但可能正是"存量 stale 链接"的现场（早期迁移 / 手工修
                # 版本把文档推到了 v2 而链接停在 v1）：按当前活动版本对齐一次即修复。
                aligned = await graph_states_repo.align_active_graph_links(
                    session,
                    user_id=row["user_id"],
                    memory_id=row["memory_id"],
                    memory_version=int(row["active_version"]),
                )
                graph_links_aligned += len(aligned)
                continue
            migrated += 1
            if payload.dry_run:
                continue
            new_version = int(row["active_version"]) + 1
            encoded = upgraded.encode("utf-8")
            stored = await store.write_immutable_version(
                user_id=row["user_id"],
                memory_id=row["memory_id"],
                version=new_version,
                content=encoded,
            )
            await docs_repo.set_active_version(
                session,
                user_id=row["user_id"],
                memory_id=row["memory_id"],
                active_version=new_version,
                active_storage_key=stored.storage_key,
                active_checksum=stored.checksum,
            )
            # review I-11①：迁移只写 version + current 的话，index 投影的 aliases/keywords/related
            # 会一直是空的（0008 的 docstring 声称回填由本任务完成）。这里在同一事务里刷新投影。
            await ctx.memory_service.refresh_index_projection(
                session, user_id=row["user_id"], memory_id=row["memory_id"]
            )
            # review-3 残留：升版的同一事务里把 KG 链接对齐到新活动版本，否则在该主题下一次
            # 提交之前，KG overlay 读侧会因为"版本不等"看不到这些映射。
            aligned = await graph_states_repo.align_active_graph_links(
                session,
                user_id=row["user_id"],
                memory_id=row["memory_id"],
                memory_version=new_version,
            )
            graph_links_aligned += len(aligned)
            await store.materialize_current(
                user_id=row["user_id"], memory_id=row["memory_id"], content=encoded
            )
        finished = len(rows) < payload.batch_size
        return {
            "kind": kind,
            "status": "done" if finished else "continue",
            "migrated": migrated,
            "skipped_already_v2": skipped_already_v2,
            # 已是 v2 但投影被本次回填补齐的文档数（评审新发现 9 的可观测信号）
            "refreshed_already_v2": refreshed_already_v2,
            # 被对齐到新活动版本的 KG 链接行数（review-3 残留的可观测信号）：既覆盖本次
            # 升版，也覆盖"重跑修好了多少条存量 stale 链接"
            "graph_links_aligned": graph_links_aligned,
            "failures": failures,
            "dry_run": payload.dry_run,
            "next_cursor": None if finished else next_cursor,
        }

    raise InvalidPayloadError(f"未知维护类型: {kind}")  # pragma: no cover
