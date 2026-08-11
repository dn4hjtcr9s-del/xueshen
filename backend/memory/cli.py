"""Memory 维护 CLI（规格 §13.1 / §16.1 / §9.1 / §21.4）。

用法：
  uv run python -m backend.memory.cli sync-knowledge-graph --check|--apply [--allow-remove]
  uv run python -m backend.memory.cli create-identity-mapping ...
  uv run python -m backend.memory.cli validate-openai
  uv run python -m backend.memory.cli create-backup
  uv run python -m backend.memory.cli restore-backup --batch-id <uuid> [--force]
  uv run python -m backend.memory.cli verify-backup-restore --batch-id <uuid>
  uv run python -m backend.memory.cli create-break-glass-grant ...
  uv run python -m backend.memory.cli revoke-break-glass-grant --grant-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from backend.memory.knowledge_graph.parser import (
    CATALOG_FILE_NAME,
    GRAPH_FILE_NAME,
    KnowledgeGraphParseError,
    parse_graph_file,
)
from backend.memory.knowledge_graph.registry import KnowledgeGraphRegistry
from backend.memory.persistence.database import Database
from backend.settings import get_settings


def _cmd_sync_knowledge_graph(args: argparse.Namespace) -> int:
    settings = get_settings()
    root = Path(settings.knowledge_graph_root)
    graph_path = root / GRAPH_FILE_NAME
    catalog_path = root / CATALOG_FILE_NAME

    try:
        parsed = parse_graph_file(graph_path, catalog_path)
    except KnowledgeGraphParseError as exc:
        print(f"[sync-knowledge-graph] 解析失败：{exc}", file=sys.stderr)
        return 2

    if parsed.skipped_dashed_edges:
        print(
            f"[sync-knowledge-graph] 警告：按裁决 A 跳过 {len(parsed.skipped_dashed_edges)} "
            "条虚线边（教材顺序提示边，不入库）"
        )
    print(
        f"[sync-knowledge-graph] 解析成功：{len(parsed.nodes)} 节点, "
        f"{len(parsed.edges)} 边, manifest={parsed.manifest_checksum[:12]}…"
    )

    async def _run() -> int:
        db = Database(settings)
        try:
            async with db.session() as session:
                registry = KnowledgeGraphRegistry(session)
                removals = await registry.plan_removals(parsed)
                if removals:
                    refs = await registry.removal_references(removals)
                    print(f"[sync-knowledge-graph] 将删除节点 {removals}，引用计数 {refs}")
                    if not args.allow_remove:
                        print(
                            "[sync-knowledge-graph] 默认失败：使用 --allow-remove 才允许删除",
                            file=sys.stderr,
                        )
                        return 3
                if not args.apply:
                    print("[sync-knowledge-graph] --check 完成（dry-run），未写入数据库")
                    return 0
                sync_run_id = await registry.create_sync_run(parsed)
                if removals and args.allow_remove:
                    await registry.archive_removal_audit(
                        sync_run_id=sync_run_id,
                        node_ids=removals,
                        privacy_hmac_key=settings.privacy_hmac_key,
                    )
                await registry.apply_sync(
                    parsed=parsed, sync_run_id=sync_run_id, allow_remove=args.allow_remove
                )
                await session.commit()
                print(f"[sync-knowledge-graph] --apply 完成，sync_run_id={sync_run_id}")
                return 0
        finally:
            await db.close()

    return asyncio.run(_run())


def _cmd_create_identity_mapping(args: argparse.Namespace) -> int:
    from backend.memory.persistence.identity import IdentityMappingRepository

    settings = get_settings()
    summary = {
        "issuer": args.issuer,
        "external_subject": args.external_subject,
        "internal_user_id": str(args.internal_user_id),
        "replace_existing": args.replace_existing,
    }

    async def _run() -> int:
        db = Database(settings)
        try:
            async with db.session() as session:
                repo = IdentityMappingRepository(session)
                existing = await repo.resolve(
                    issuer=args.issuer, external_subject=args.external_subject
                )
                if existing and not args.replace_existing:
                    print(
                        f"[identity] 冲突：已存在映射 -> {existing}；使用 --replace-existing 覆盖",
                        file=sys.stderr,
                    )
                    return 3
                if args.dry_run:
                    print(f"[identity] --dry-run 摘要：{summary}")
                    return 0
                created = await repo.create(
                    internal_user_id=args.internal_user_id,
                    issuer=args.issuer,
                    external_subject=args.external_subject,
                    replace_existing=args.replace_existing,
                )
                await session.commit()
                print(f"[identity] 已创建映射：{created}")
                return 0
        finally:
            await db.close()

    return asyncio.run(_run())


def _cmd_validate_openai(_args: argparse.Namespace) -> int:
    """手动 smoke test（§9.1）：真实模型验收为手动步骤，不阻塞 CI。"""
    settings = get_settings()
    if not settings.openai_api_key:
        print("[validate-openai] 未配置 OPENAI_API_KEY", file=sys.stderr)
        return 2

    async def _run() -> int:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.openai_memory_timeout_seconds,
        )
        try:
            request_kwargs: dict[str, Any] = {
                "model": settings.openai_memory_model,
                "input": '只回复 JSON：{"ok": true}',
                "reasoning": {"effort": settings.openai_reasoning_effort},
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "validate_openai",
                        "schema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                        "strict": True,
                    }
                },
            }
            response = await client.responses.create(**request_kwargs)
            print(f"[validate-openai] 成功：model={settings.openai_memory_model}")
            print(f"[validate-openai] 输出：{response.output_text[:200]}")
            return 0
        except Exception as exc:
            print(
                f"[validate-openai] 失败：{type(exc).__name__}: {exc}\n"
                "请确认模型存在、账号有权限、参数受支持、Schema 兼容。",
                file=sys.stderr,
            )
            return 1

    return asyncio.run(_run())


def _cmd_create_backup(_args: argparse.Namespace) -> int:
    """执行一次备份批次（§21.4）：pg_dump + Markdown tar + age 加密 + backup_runs。"""
    from backend.memory.backup import BackupError, create_backup

    settings = get_settings()

    async def _run() -> int:
        db = Database(settings)
        try:
            batch_id = await create_backup(settings, db.session_factory)
            print(f"[create-backup] 成功：batch_id={batch_id}")
            return 0
        except BackupError as exc:
            print(f"[create-backup] 失败：{exc}", file=sys.stderr)
            return 1
        finally:
            await db.close()

    return asyncio.run(_run())


def _cmd_verify_backup_restore(args: argparse.Namespace) -> int:
    """每周恢复验证（§21.4）：隔离目录解密校验并更新 backup_runs 验证状态。"""
    from backend.memory.backup import BackupError, verify_backup_restore

    settings = get_settings()

    async def _run() -> int:
        db = Database(settings)
        try:
            await verify_backup_restore(settings, db.session_factory, batch_id=args.batch_id)
            print(f"[verify-backup-restore] 成功：batch_id={args.batch_id}")
            return 0
        except BackupError as exc:
            print(f"[verify-backup-restore] 失败：{exc}", file=sys.stderr)
            return 1
        finally:
            await db.close()

    return asyncio.run(_run())


def _cmd_restore_backup(args: argparse.Namespace) -> int:
    """恢复一个备份批次（§21.4）：校验 manifest/checksum 后写入目标。"""
    from backend.memory.backup import BackupError, restore_backup

    settings = get_settings()

    async def _run() -> int:
        db = Database(settings)
        try:
            replay_ids = await restore_backup(
                settings, db.session_factory, batch_id=args.batch_id, force=args.force
            )
            print(f"[restore-backup] 成功：batch_id={args.batch_id}")
            if replay_ids:
                print(
                    "[restore-backup] 警告：以下账号删除 manifest 必须重新应用（§21.4）：\n"
                    + "\n".join(f"  - {rid}" for rid in replay_ids)
                    + "\n服务启动后对每个 account_deletion_id 调用 "
                    "POST /internal/account-memory/purge 重放。"
                )
            return 0
        except BackupError as exc:
            print(f"[restore-backup] 失败：{exc}", file=sys.stderr)
            return 1
        finally:
            await db.close()

    return asyncio.run(_run())


def _cmd_create_break_glass_grant(args: argparse.Namespace) -> int:
    """创建 break-glass grant（§13.15）：限用户、限时、必填 reason/scopes。"""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from backend.memory.break_glass import validate_grant_creation
    from backend.memory.contracts.common import new_trace_id
    from backend.memory.persistence import break_glass as bg_repo

    settings = get_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=args.minutes)
    scopes = [s for s in args.scopes.split() if s]
    grant_id = uuid4()

    try:
        validate_grant_creation(
            settings=settings,
            reason=args.reason,
            scopes=scopes,
            expires_at=expires_at,
            now=now,
            admin_user_id=args.admin_user_id,
            approved_by=args.approved_by,
        )
    except ValueError as exc:
        print(f"[break-glass] 校验失败：{exc}", file=sys.stderr)
        return 2

    summary = {
        "grant_id": str(grant_id),
        "admin_user_id": str(args.admin_user_id),
        "target_user_id": str(args.target_user_id),
        "scopes": scopes,
        "expires_at": expires_at.isoformat(),
        "approved_by": str(args.approved_by) if args.approved_by else None,
    }
    if args.dry_run:
        print(f"[break-glass] --dry-run 摘要：{summary}")
        return 0

    async def _run() -> int:
        db = Database(settings)
        try:
            async with db.session() as session:
                trace_id = new_trace_id()
                await bg_repo.create_grant(
                    session,
                    grant_id=grant_id,
                    admin_user_id=args.admin_user_id,
                    target_user_id=args.target_user_id,
                    reason=args.reason,
                    scopes=scopes,
                    approved_by=args.approved_by,
                    expires_at=expires_at,
                )
                for action in ("request", "approve"):
                    await bg_repo.insert_audit(
                        session,
                        audit_id=uuid4(),
                        grant_id=grant_id,
                        admin_user_id=args.admin_user_id,
                        target_user_id=args.target_user_id,
                        action=action,
                        resource_type="grant",
                        resource_id=str(grant_id),
                        trace_id=trace_id,
                    )
                await session.commit()
                print(f"[break-glass] 已创建 grant：{summary}")
                return 0
        finally:
            await db.close()

    return asyncio.run(_run())


def _cmd_revoke_break_glass_grant(args: argparse.Namespace) -> int:
    """撤销 break-glass grant 并写审计（§13.15）。"""
    from datetime import UTC, datetime
    from uuid import uuid4

    from backend.memory.contracts.common import new_trace_id
    from backend.memory.persistence import break_glass as bg_repo

    settings = get_settings()

    async def _run() -> int:
        db = Database(settings)
        try:
            async with db.session() as session:
                grant = await bg_repo.get_grant(session, args.grant_id)
                if grant is None:
                    print(f"[break-glass] grant 不存在：{args.grant_id}", file=sys.stderr)
                    return 2
                revoked = await bg_repo.revoke_grant(
                    session, grant_id=args.grant_id, revoked_at=datetime.now(UTC)
                )
                if not revoked:
                    print(f"[break-glass] grant 已撤销过：{args.grant_id}", file=sys.stderr)
                    return 3
                await bg_repo.insert_audit(
                    session,
                    audit_id=uuid4(),
                    grant_id=args.grant_id,
                    admin_user_id=grant["admin_user_id"],
                    target_user_id=grant["target_user_id"],
                    action="revoke",
                    resource_type="grant",
                    resource_id=str(args.grant_id),
                    trace_id=new_trace_id(),
                )
                await session.commit()
                print(f"[break-glass] 已撤销 grant：{args.grant_id}")
                return 0
        finally:
            await db.close()

    return asyncio.run(_run())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backend.memory.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync-knowledge-graph")
    group = sync.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="dry-run 校验")
    group.add_argument("--apply", action="store_true", help="实际写入注册表")
    sync.add_argument("--allow-remove", action="store_true", help="允许删除节点（先审计）")
    sync.set_defaults(func=_cmd_sync_knowledge_graph)

    identity = sub.add_parser("create-identity-mapping")
    identity.add_argument("--issuer", required=True)
    identity.add_argument("--external-subject", required=True)
    identity.add_argument("--internal-user-id", required=True, type=UUID)
    identity.add_argument("--dry-run", action="store_true")
    identity.add_argument("--replace-existing", action="store_true")
    identity.set_defaults(func=_cmd_create_identity_mapping)

    validate = sub.add_parser("validate-openai")
    validate.set_defaults(func=_cmd_validate_openai)

    verify = sub.add_parser("verify-backup-restore")
    verify.add_argument("--batch-id", required=True, type=UUID)
    verify.set_defaults(func=_cmd_verify_backup_restore)

    backup = sub.add_parser("create-backup")
    backup.set_defaults(func=_cmd_create_backup)

    restore = sub.add_parser("restore-backup")
    restore.add_argument("--batch-id", required=True, type=UUID)
    restore.add_argument("--force", action="store_true", help="覆盖非空目标（危险）")
    restore.set_defaults(func=_cmd_restore_backup)

    bg_create = sub.add_parser("create-break-glass-grant")
    bg_create.add_argument("--admin-user-id", required=True, type=UUID)
    bg_create.add_argument("--target-user-id", required=True, type=UUID)
    bg_create.add_argument("--reason", required=True)
    bg_create.add_argument("--scopes", required=True, help="空格分隔，如 'memory:read'")
    bg_create.add_argument("--minutes", type=int, default=30, help="有效期分钟数（≤60）")
    bg_create.add_argument("--approved-by", type=UUID, default=None)
    bg_create.add_argument("--dry-run", action="store_true")
    bg_create.set_defaults(func=_cmd_create_break_glass_grant)

    bg_revoke = sub.add_parser("revoke-break-glass-grant")
    bg_revoke.add_argument("--grant-id", required=True, type=UUID)
    bg_revoke.set_defaults(func=_cmd_revoke_break_glass_grant)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
