"""知识总结运维 CLI（方案 §21.4）。

所有可能修改数据库的命令默认 dry-run；只有同时提供 ``--apply``、``--operator`` 和
``--ticket-id`` 才执行写入，并在同一事务记录管理员审计。输出只包含稳定 ID、计数和错误码。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from backend.conversation.persistence.database import ConversationDatabase
from backend.conversation.services.knowledge_summary_retention import run_retention_once
from backend.memory.persistence.database import exec_rowcount
from backend.settings import get_settings

_CANCEL_RETRY_BLOCKED = {
    "THREAD_DELETED",
    "ACCOUNT_DELETED",
    "KNOWLEDGE_SUMMARY_SOURCE_CHANGED",
}
CommandHandler = Callable[[argparse.Namespace], Awaitable[int]]


def _redacted_args(args: argparse.Namespace, *, exclude: set[str] | None = None) -> dict[str, Any]:
    """生成不含正文、Prompt、quote、token 和密钥的审计参数。"""
    excluded = {"func", "apply", "operator", "ticket_id", *(exclude or set())}
    return {
        key: (str(value) if isinstance(value, UUID) else value)
        for key, value in vars(args).items()
        if key not in excluded and value is not None
    }


def _require_apply(args: argparse.Namespace) -> None:
    """校验生产修改命令的三项安全门槛。"""
    if args.apply and (not args.operator or not args.ticket_id):
        raise SystemExit("--apply 必须同时提供 --operator 和 --ticket-id")


async def _write_audit(
    session: Any,
    *,
    args: argparse.Namespace,
    command: str,
    affected_row_count: int,
    result: str,
) -> None:
    """在修改事务中记录脱敏管理员审计。"""
    await session.execute(
        text(
            """
            INSERT INTO conversation.knowledge_summary_admin_audit (
                audit_id, operator, ticket_id, command, arguments_redacted,
                affected_row_count, result
            ) VALUES (
                :audit_id, :operator, :ticket_id, :command, CAST(:arguments AS jsonb),
                :affected_row_count, :result
            )
            """
        ),
        {
            "audit_id": uuid4(),
            "operator": args.operator,
            "ticket_id": args.ticket_id,
            "command": command,
            "arguments": json.dumps(_redacted_args(args), ensure_ascii=False),
            "affected_row_count": affected_row_count,
            "result": result,
        },
    )


async def _open_db() -> ConversationDatabase:
    """按统一 Settings 打开 Conversation 数据库，不读取 CLI 私有连接配置。"""
    settings = get_settings()
    if not settings.conversation_database_url:
        raise RuntimeError("未配置 CONVERSATION_DATABASE_URL")
    return ConversationDatabase(settings)


async def _list_dead_letter(args: argparse.Namespace) -> int:
    """列出死信 Job；这是只读命令，不需要 apply 门槛。"""
    db = await _open_db()
    try:
        async with db.session() as session:
            params: dict[str, Any] = {"limit": args.limit}
            user_filter = ""
            if args.user_id is not None:
                params["user_id"] = args.user_id
                user_filter = "AND user_id = :user_id"
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT generation_id, user_id, thread_id, turn_id, trigger,
                               status, attempt_count, last_error_code, created_at, updated_at
                        FROM conversation.knowledge_summary_generation_jobs
                        WHERE status = 'dead_letter' {user_filter}
                        ORDER BY updated_at DESC, generation_id DESC
                        LIMIT :limit
                        """
                    ),
                    params,
                )
            ).mappings()
            print(json.dumps([_json_row(dict(row)) for row in rows], ensure_ascii=False, indent=2))
            return 0
    finally:
        await db.close()


async def _retry_generation(args: argparse.Namespace) -> int:
    """为允许的 dead_letter/cancelled Job 创建新的 ops_retry Job。"""
    _require_apply(args)
    db = await _open_db()
    try:
        async with db.session() as session:
            async with session.begin():
                row = (
                    (
                        await session.execute(
                            text(
                                """
                            SELECT *
                            FROM conversation.knowledge_summary_generation_jobs
                            WHERE generation_id = :generation_id
                            FOR UPDATE
                            """
                            ),
                            {"generation_id": args.generation_id},
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    print("Generation 不存在", file=sys.stderr)
                    return 2
                if row["status"] == "needs_review":
                    print("needs_review 必须先解决 review，禁止 ops_retry", file=sys.stderr)
                    return 3
                if row["status"] == "cancelled" and row["last_error_code"] in _CANCEL_RETRY_BLOCKED:
                    print(f"取消原因禁止重试：{row['last_error_code']}", file=sys.stderr)
                    return 3
                if row["status"] not in {"dead_letter", "cancelled"}:
                    print(f"当前状态不允许 ops_retry：{row['status']}", file=sys.stderr)
                    return 3
                if row["status"] == "cancelled":
                    current_checkpoint = (
                        await session.execute(
                            text(
                                """
                                SELECT source_checkpoint_id
                                FROM conversation.conversation_turns
                                WHERE turn_id = :turn_id AND user_id = :user_id
                                FOR SHARE
                                """
                            ),
                            {"turn_id": row["turn_id"], "user_id": row["user_id"]},
                        )
                    ).scalar_one_or_none()
                    if current_checkpoint != row["source_checkpoint_id"]:
                        print("当前消息 checkpoint 已变化，禁止 ops_retry", file=sys.stderr)
                        return 3
                if _payload_was_scrubbed(row):
                    print("原 Job payload 已 scrub，无法安全复用，禁止 ops_retry", file=sys.stderr)
                    return 3

                summary = {
                    "source_generation_id": str(row["generation_id"]),
                    "status": row["status"],
                    "trigger": "ops_retry",
                }
                if not args.apply:
                    print(json.dumps({"dry_run": True, **summary}, ensure_ascii=False))
                    return 0

                new_id = uuid4()
                await session.execute(
                    text(
                        """
                        INSERT INTO conversation.knowledge_summary_generation_jobs (
                            generation_id, idempotency_key, client_request_id, user_id,
                            thread_id, turn_id, source_checkpoint_id, trigger, status,
                            input_manifest, extraction_result, merge_plan_result,
                            primary_turn_occurred_at, next_attempt_at, created_at, updated_at
                        ) VALUES (
                            :generation_id, :idempotency_key, NULL, :user_id,
                            :thread_id, :turn_id, :source_checkpoint_id, 'ops_retry', 'pending',
                            CAST(:input_manifest AS jsonb), CAST(:extraction_result AS jsonb),
                            CAST(:merge_plan_result AS jsonb), :primary_turn_occurred_at,
                            now(), now(), now()
                        )
                        """
                    ),
                    {
                        "generation_id": new_id,
                        "idempotency_key": f"ops-retry:{row['generation_id']}:{new_id}",
                        "user_id": row["user_id"],
                        "thread_id": row["thread_id"],
                        "turn_id": row["turn_id"],
                        "source_checkpoint_id": row["source_checkpoint_id"],
                        "input_manifest": _json_text(row["input_manifest"]),
                        "extraction_result": _json_text(row["extraction_result"]),
                        "merge_plan_result": _json_text(row["merge_plan_result"]),
                        "primary_turn_occurred_at": row["primary_turn_occurred_at"],
                    },
                )
                await _write_audit(
                    session,
                    args=args,
                    command="retry-generation",
                    affected_row_count=1,
                    result="applied",
                )
                print(
                    json.dumps(
                        {"created_generation_id": str(new_id), **summary}, ensure_ascii=False
                    )
                )
                return 0
    finally:
        await db.close()


async def _rebuild_summary_counts(args: argparse.Namespace) -> int:
    """从消息级来源重算 summary 的 Turn/message 计数。"""
    _require_apply(args)
    db = await _open_db()
    try:
        async with db.session() as session:
            async with session.begin():
                conditions = [] if args.include_deleted else ["s.status = 'active'"]
                params: dict[str, Any] = {}
                if args.user_id is not None:
                    conditions.append("s.user_id = :user_id")
                    params["user_id"] = args.user_id
                where = " AND ".join(conditions) if conditions else "TRUE"
                count_sql = f"SELECT COUNT(*) FROM conversation.knowledge_summaries s WHERE {where}"
                count = int((await session.execute(text(count_sql), params)).scalar_one())
                if not args.apply:
                    print(json.dumps({"dry_run": True, "summary_count": count}, ensure_ascii=False))
                    return 0
                await session.execute(
                    text(
                        f"""
                        UPDATE conversation.knowledge_summaries AS s
                        SET source_count = COALESCE(
                                (SELECT COUNT(DISTINCT x.turn_id)
                                 FROM conversation.knowledge_summary_sources AS x
                                 WHERE x.summary_id = s.summary_id), 0
                            ),
                            available_source_count = COALESCE(
                                (SELECT COUNT(DISTINCT x.turn_id)
                                 FROM conversation.knowledge_summary_sources AS x
                                 WHERE x.summary_id = s.summary_id
                                   AND x.status = 'available'), 0
                            ),
                            source_message_count = COALESCE(
                                (SELECT COUNT(DISTINCT x.message_id)
                                 FROM conversation.knowledge_summary_sources AS x
                                 WHERE x.summary_id = s.summary_id), 0
                            ),
                            updated_at = now()
                        WHERE {where}
                        """
                    ),
                    params,
                )
                await _write_audit(
                    session,
                    args=args,
                    command="rebuild-summary-counts",
                    affected_row_count=count,
                    result="applied",
                )
                print(json.dumps({"updated_summary_count": count}, ensure_ascii=False))
                return 0
    finally:
        await db.close()


async def _validate_consistency(args: argparse.Namespace) -> int:
    """检查 active summary 的来源计数是否与消息级来源一致。"""
    db = await _open_db()
    try:
        async with db.session() as session:
            params: dict[str, Any] = {"limit": args.limit}
            user_filter = ""
            if args.user_id is not None:
                params["user_id"] = args.user_id
                user_filter = "AND s.user_id = :user_id"
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT s.summary_id,
                               s.source_count,
                               actual.actual_source_count,
                               s.available_source_count,
                               actual.actual_available_source_count,
                               s.source_message_count,
                               actual.actual_source_message_count
                        FROM conversation.knowledge_summaries AS s
                        CROSS JOIN LATERAL (
                            SELECT
                                COUNT(DISTINCT x.turn_id) AS actual_source_count,
                                COUNT(DISTINCT x.turn_id) FILTER (
                                    WHERE x.status = 'available'
                                ) AS actual_available_source_count,
                                COUNT(DISTINCT x.message_id) AS actual_source_message_count
                            FROM conversation.knowledge_summary_sources AS x
                            WHERE x.summary_id = s.summary_id
                        ) AS actual
                        WHERE s.status = 'active' {user_filter}
                          AND (
                              s.source_count <> actual.actual_source_count
                              OR s.available_source_count <> actual.actual_available_source_count
                              OR s.source_message_count <> actual.actual_source_message_count
                          )
                        ORDER BY s.updated_at ASC, s.summary_id ASC
                        LIMIT :limit
                        """
                    ),
                    params,
                )
            ).mappings()
            payload = [_json_row(dict(row)) for row in rows]
            print(
                json.dumps(
                    {"consistent": not payload, "mismatches": payload}, ensure_ascii=False, indent=2
                )
            )
            return 0 if not payload else 4
    finally:
        await db.close()


async def _show_runtime_control(args: argparse.Namespace) -> int:
    """读取全局自动生成熔断状态。"""
    db = await _open_db()
    try:
        async with db.session() as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT *
                        FROM conversation.knowledge_summary_runtime_control
                        WHERE control_key = 'global'
                        """
                        )
                    )
                )
                .mappings()
                .first()
            )
            print(json.dumps(_json_row(dict(row)) if row else None, ensure_ascii=False, indent=2))
            return 0
    finally:
        await db.close()


async def _set_auto_suspension(args: argparse.Namespace, *, resume: bool) -> int:
    """人工暂停或恢复自动生成，并记录审计。"""
    _require_apply(args)
    db = await _open_db()
    try:
        async with db.session() as session:
            async with session.begin():
                action = "resume" if resume else "suspend"
                if not args.apply:
                    print(json.dumps({"dry_run": True, "action": action}, ensure_ascii=False))
                    return 0
                if resume:
                    affected = await exec_rowcount(
                        session,
                        text(
                            """
                            UPDATE conversation.knowledge_summary_runtime_control
                            SET auto_generation_suspended = false,
                                suspend_reason_code = NULL,
                                suspend_snapshot = NULL,
                                suspended_at = NULL,
                                updated_by = :operator,
                                updated_at = now()
                            WHERE control_key = 'global'
                            """
                        ),
                        {"operator": args.operator},
                    )
                else:
                    affected = await exec_rowcount(
                        session,
                        text(
                            """
                            UPDATE conversation.knowledge_summary_runtime_control
                            SET auto_generation_suspended = true,
                                suspend_reason_code = :reason,
                                updated_by = :operator,
                                updated_at = now(),
                                suspended_at = COALESCE(suspended_at, now())
                            WHERE control_key = 'global'
                            """
                        ),
                        {"reason": args.reason_code, "operator": args.operator},
                    )
                await _write_audit(
                    session,
                    args=args,
                    command="resume-auto" if resume else "suspend-auto",
                    affected_row_count=affected,
                    result="applied",
                )
                print(json.dumps({"applied": True, "action": action}, ensure_ascii=False))
                return 0
    finally:
        await db.close()


async def _run_retention(args: argparse.Namespace) -> int:
    """运行一轮 retention；默认只输出 dry-run 说明，不执行 SQL 修改。"""
    _require_apply(args)
    db = await _open_db()
    try:
        settings = get_settings()
        if not args.apply:
            print(json.dumps({"dry_run": True, "operation": "retention"}, ensure_ascii=False))
            return 0
        result = await run_retention_once(db.session_factory, settings)
        async with db.session() as session:
            async with session.begin():
                await _write_audit(
                    session,
                    args=args,
                    command="run-retention",
                    affected_row_count=sum(result.values()),
                    result="applied",
                )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        await db.close()


def _payload_was_scrubbed(row: Any) -> bool:
    """scrub 后只剩审计标记，不能把不完整 payload 复制到新 Job。"""
    return any(
        isinstance(row.get(key), dict) and bool(row[key].get("scrubbed"))
        for key in ("input_manifest", "extraction_result", "merge_plan_result")
    )


def _json_text(value: Any) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def _json_row(row: dict[str, Any]) -> dict[str, Any]:
    """将 UUID/时间转换为稳定 JSON 标量。"""
    return {
        key: (
            value.isoformat()
            if hasattr(value, "isoformat")
            else str(value)
            if isinstance(value, UUID)
            else value
        )
        for key, value in row.items()
    }


def _add_mutation_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--apply", action="store_true", help="实际写入；默认 dry-run")
    parser.add_argument("--operator", help="生产操作人，--apply 时必填")
    parser.add_argument("--ticket-id", help="变更单号，--apply 时必填")


def build_parser() -> argparse.ArgumentParser:
    """构建固定的知识总结运维命令入口。"""
    parser = argparse.ArgumentParser(prog="backend.conversation.cli.knowledge_summary")
    sub = parser.add_subparsers(dest="command", required=True)

    dead = sub.add_parser("list-dead-letter-jobs")
    dead.add_argument("--user-id", type=UUID)
    dead.add_argument("--limit", type=int, default=50)
    dead.set_defaults(func=_list_dead_letter)

    retry = sub.add_parser("retry-generation")
    retry.add_argument("--generation-id", required=True, type=UUID)
    _add_mutation_flags(retry)
    retry.set_defaults(func=_retry_generation)

    rebuild = sub.add_parser("rebuild-summary-counts")
    rebuild.add_argument("--user-id", type=UUID)
    rebuild.add_argument("--include-deleted", action="store_true")
    _add_mutation_flags(rebuild)
    rebuild.set_defaults(func=_rebuild_summary_counts)

    validate = sub.add_parser("validate-knowledge-summary-consistency")
    validate.add_argument("--user-id", type=UUID)
    validate.add_argument("--limit", type=int, default=100)
    validate.set_defaults(func=_validate_consistency)

    show = sub.add_parser("show-runtime-control")
    show.set_defaults(func=_show_runtime_control)

    suspend = sub.add_parser("suspend-auto")
    suspend.add_argument("--reason-code", required=True)
    _add_mutation_flags(suspend)
    suspend.set_defaults(func=lambda args: _set_auto_suspension(args, resume=False))

    resume = sub.add_parser("resume-auto")
    _add_mutation_flags(resume)
    resume.set_defaults(func=lambda args: _set_auto_suspension(args, resume=True))

    retention = sub.add_parser("run-retention")
    _add_mutation_flags(retention)
    retention.set_defaults(func=_run_retention)
    return parser


async def _invoke_handler(handler: CommandHandler, args: argparse.Namespace) -> int:
    """将通用 Awaitable 包装为 asyncio.run 所需的 coroutine。"""
    return await handler(args)


def main() -> None:
    """执行 CLI。"""
    args = build_parser().parse_args()
    handler: CommandHandler = args.func
    raise SystemExit(asyncio.run(_invoke_handler(handler, args)))


if __name__ == "__main__":
    main()
