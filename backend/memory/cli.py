"""Memory 维护 CLI（规格 §13.1 / §16.1 / §9.1 / §21.4）。

用法：
  uv run python -m backend.memory.cli sync-knowledge-graph --check|--apply [--allow-remove]
  uv run python -m backend.memory.cli create-identity-mapping ...
  uv run python -m backend.memory.cli validate-openai
  uv run python -m backend.memory.cli verify-backup-restore --batch-id <uuid>
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


def _cmd_verify_backup_restore(_args: argparse.Namespace) -> int:
    """步骤 15（备份/恢复）接入完整实现。"""
    print("[verify-backup-restore] 尚未实现：将在备份/恢复步骤接入", file=sys.stderr)
    return 2


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

    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
