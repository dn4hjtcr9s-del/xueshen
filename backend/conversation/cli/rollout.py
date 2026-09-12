"""Rollout 运维 CLI（memory-rebuild §5.4 Phase 2 / §5.5 Phase 3）。

五个子命令，**每个都支持 ``--dry-run``**：

- ``verify-manifest``：只读扫描五类不一致；发现即退出码 1，空则 0（CI 断言用）；
- ``reconcile-orphans``：扫描 + :meth:`RolloutReconciler.repair`；默认演练，``--apply`` 才写；
- ``export-thread``：按 ordinal 顺序把该 thread 的全部段拼成一个 JSONL 导出；
- ``delete-thread-rollouts``：先删对象与热段、再落 tombstone；默认演练；
- ``retention-scan``：列出并按需清理超过保留期的 ``sealed`` 段；默认演练。

依赖由 CLI 自己装配，不读任何私有连接配置：``Settings`` 的 ``conversation_database_url``
与 ``conversation_rollout_root``，对象存储按 ``conversation_rollout_object_store``
经 :func:`~backend.conversation.rollout.factory.build_rollout_object_store` 构造
（worker / app / CLI 三个装配点共用，I-7）；配置成 ``kodo`` 时只有凭据缺失才失败，
不静默切回本地——静默降级会让运维以为对象已经上了云。

删除类子命令（``delete-thread-rollouts`` / ``retention-scan``）除对象镜像外还会删除
**本地热缓存段文件**（``{root}/threads/...``，C-3）：只删 ``objects/`` 会让用户原文
留在磁盘上，而 tombstone 之后再没有工具能发现它。两条路径都经由
:func:`~backend.conversation.rollout.deletion.delete_rollout_payloads`（对象 + 热段 +
按 thread 清扫）——review-2 新发现 6 要求未封存段（``object_key IS NULL``）也必须被删掉，
而它们没有 key 可推导，只能按 ``<thread_id>`` 目录清扫。

退出码：0 成功；1 发现不一致或存在失败的操作；2 前置条件不满足（配置/对象存储不可用）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.conversation.contracts.object_store import (
    ObjectStoreError,
    ObjectStoreNonRetryableError,
)
from backend.conversation.persistence import rollout_manifests as manifests_repo
from backend.conversation.persistence.database import (
    create_conversation_engine,
    create_conversation_session_factory,
)
from backend.conversation.rollout.codec import sha256_hex
from backend.conversation.rollout.deletion import (
    delete_rollout_payloads,
    delete_thread_rollout_payloads,
)
from backend.conversation.rollout.reconcile import (
    FAILURE_PREFIX,
    ReconcileReport,
    RolloutReconciler,
)
from backend.settings import Settings, get_settings

CommandHandler = Callable[[argparse.Namespace], Awaitable[int]]

#: retention 候选取 ``created_at`` 早于阈值且仍为 ``sealed`` 的段（§5.5 生命周期）。
_RETENTION_SQL = """
    SELECT segment_id, thread_id, object_key, created_at
    FROM conversation.conversation_rollout_segments
    WHERE status = 'sealed' AND created_at < :cutoff
    ORDER BY created_at, segment_id
    LIMIT :limit
"""


@dataclass(slots=True)
class _Runtime:
    """CLI 自装配的依赖集合（引擎生命周期由 :func:`_open_runtime` 管理）。"""

    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    object_store: Any


@asynccontextmanager
async def _open_runtime() -> AsyncIterator[_Runtime]:
    """构造 Settings → 引擎 → 会话工厂 → 本地对象存储，退出时释放连接池。"""
    settings = get_settings()
    if not settings.conversation_database_url:
        raise SystemExit("未配置 CONVERSATION_DATABASE_URL，无法连接 conversation 库")
    from backend.conversation.rollout.factory import build_rollout_object_store

    try:
        object_store = build_rollout_object_store(settings)
    except ObjectStoreNonRetryableError as exc:
        # 配置不全时显式失败，不静默降级（§5.12）
        raise SystemExit(f"rollout 对象存储配置不可用: {exc}") from exc
    engine = create_conversation_engine(settings)
    try:
        yield _Runtime(
            settings=settings,
            engine=engine,
            session_factory=create_conversation_session_factory(engine),
            object_store=object_store,
        )
    finally:
        await engine.dispose()


def _build_reconciler(runtime: _Runtime) -> RolloutReconciler:
    """按 CLI 运行时装配对账器（日志走默认 logger，结论同时打印到 stdout）。"""
    return RolloutReconciler(
        session_factory=runtime.session_factory,
        object_store=runtime.object_store,
        rollout_root=runtime.settings.conversation_rollout_root,
    )


def _resolve_dry_run(args: argparse.Namespace) -> bool:
    """统一 dry-run 语义：显式 ``--dry-run`` 或未给 ``--apply`` 都按演练处理。"""
    dry_run = bool(getattr(args, "dry_run", False))
    apply = bool(getattr(args, "apply", False))
    if dry_run and apply:
        print("同时给出 --dry-run 与 --apply：按 --dry-run 处理，不做任何修改", file=sys.stderr)
    return dry_run or not apply


def _print_report(report: ReconcileReport) -> None:
    """打印对账结果：一行中文摘要 + 每条 finding 的定位信息。"""
    print(f"rollout manifest 对账：{report.summary()}")
    for finding in report.findings:
        print(
            f"[{finding.kind}] segment={finding.segment_id} thread={finding.thread_id} "
            f"object={finding.object_key} message={finding.message_id} {finding.detail}"
        )


def _write_bytes(path: Path, data: bytes) -> None:
    """同步写导出文件（ASYNC240：Path 操作不得出现在 async 函数里）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


async def _verify_manifest(args: argparse.Namespace) -> int:
    """只读校验：发现任何不一致即退出码 1（CI 门禁）。"""
    async with _open_runtime() as runtime:
        reconciler = _build_reconciler(runtime)
        try:
            report = await reconciler.scan(limit=args.limit)
        except ObjectStoreError as exc:
            print(f"对象存储不可用，校验未完成：{exc}", file=sys.stderr)
            return 2
        _print_report(report)
        if report.ok:
            return 0
        print(
            "rollout manifest 校验失败：请先跑 reconcile-orphans 查看可修复项，其余人工处理",
            file=sys.stderr,
        )
        return 1


async def _reconcile_orphans(args: argparse.Namespace) -> int:
    """扫描 + 修复；默认 dry-run，``--apply`` 才真正标记段 deleted。"""
    dry_run = _resolve_dry_run(args)
    async with _open_runtime() as runtime:
        reconciler = _build_reconciler(runtime)
        try:
            report = await reconciler.scan(limit=args.limit)
        except ObjectStoreError as exc:
            print(f"对象存储不可用，对账未完成：{exc}", file=sys.stderr)
            return 2
        _print_report(report)
        actions = await reconciler.repair(report, dry_run=dry_run)
        for action in actions:
            print(action)
        failures = [action for action in actions if action.startswith(FAILURE_PREFIX)]
        if failures:
            print(f"有 {len(failures)} 项修复失败，详见日志", file=sys.stderr)
            return 1
        return 0


async def _export_thread(args: argparse.Namespace) -> int:
    """按 ordinal 顺序导出 thread 的段；对象缺失只提示跳过，不中断导出。"""
    async with _open_runtime() as runtime:
        async with runtime.session_factory() as session:
            segments = await manifests_repo.list_by_thread(session, args.thread_id)
        chunks: list[bytes] = []
        skipped = 0
        for segment in segments:
            segment_id = segment.get("segment_id")
            object_key = segment.get("object_key")
            if not object_key:
                print(f"段 {segment_id} 尚未封存（无 object_key），跳过", file=sys.stderr)
                skipped += 1
                continue
            try:
                data = await runtime.object_store.get(key=str(object_key))
            except ObjectStoreError as exc:
                print(f"段 {segment_id} 的对象不可读，跳过：{object_key}（{exc}）", file=sys.stderr)
                skipped += 1
                continue
            expected = str(segment.get("sha256") or "").strip().lower()
            if expected and sha256_hex(data) != expected:
                print(
                    f"段 {segment_id} 的内容 sha256 与 manifest 不一致，仍按原样导出，请人工确认",
                    file=sys.stderr,
                )
            chunks.append(data)
        payload = b"".join(chunks)
        if args.dry_run:
            print(
                f"[dry-run] 将导出 {len(chunks)} 个段、{len(payload)} 字节"
                f"（跳过 {skipped} 个）；未写入任何文件"
            )
            return 0
        if args.output:
            output_path = Path(args.output)
            await asyncio.to_thread(_write_bytes, output_path, payload)
            print(
                f"已导出 {len(chunks)} 个段、{len(payload)} 字节到 {output_path}"
                f"（跳过 {skipped} 个）"
            )
        else:
            buffer = getattr(sys.stdout, "buffer", None)
            if buffer is None:
                print("stdout 不支持二进制写入，请用 --output 指定文件", file=sys.stderr)
                return 2
            buffer.write(payload)
            buffer.flush()
            print(
                f"已导出 {len(chunks)} 个段、{len(payload)} 字节到 stdout（跳过 {skipped} 个）",
                file=sys.stderr,
            )
        return 0


async def _delete_thread_rollouts(args: argparse.Namespace) -> int:
    """逐个删除对象与本地热段，**全部成功后才**落 tombstone。"""
    dry_run = _resolve_dry_run(args)
    async with _open_runtime() as runtime:
        async with runtime.session_factory() as session:
            segments = await manifests_repo.list_by_thread(session, args.thread_id)
        keys = [str(segment["object_key"]) for segment in segments if segment.get("object_key")]
        unsealed = [segment for segment in segments if not segment.get("object_key")]
        if dry_run:
            print(
                f"[dry-run] 将把 thread {args.thread_id} 的 {len(segments)} 个段标记为 deleted，"
                f"并删除 {len(keys)} 个对象（含本地热缓存段）："
            )
            for key in keys:
                print(f"  待删除对象 {key}")
            if unsealed:
                # 未封存段没有 object key（新发现 6）：只能按 <thread_id> 目录清扫
                print(
                    f"  另有 {len(unsealed)} 个未封存段（无 object_key），"
                    "将按 thread 目录清扫本地热缓存段"
                )
            print("[dry-run] 未产生任何写操作；确认后加 --apply 执行")
            return 0
        # 顺序（与 thread_deletion 统一）：**先把对象与热段删干净，再落 tombstone**。
        # 反过来的话，对象删除失败会留下"manifest 已是 deleted、对象还在"的残留；
        # 而 reconcile 的孤儿判定只把**非 deleted** 行的 object_key 当作有效引用，
        # 这种残留对象将永远不被报为孤儿 —— 静默的数据泄漏。
        #
        # 热段走统一助手：逐 key 精确删除 + 按 thread 目录清扫（未封存段、kodo 本地
        # 缓存、无 manifest 行的孤儿热文件都在这条路径上被清掉）。
        report = await delete_thread_rollout_payloads(
            object_store=runtime.object_store,
            object_keys=keys,
            thread_id=args.thread_id,
        )
        if not report.ok:
            for failure in report.failures:
                print(f"对象/热段删除失败：{failure}", file=sys.stderr)
            print(
                f"有 {len(report.failures)} 项未删除成功；段**未**标 tombstone，"
                "重跑本命令即可（对象删除是幂等的）",
                file=sys.stderr,
            )
            return 1
        async with runtime.session_factory() as session:
            async with session.begin():
                await manifests_repo.mark_thread_deleted(session, thread_id=args.thread_id)
        print(f"thread {args.thread_id}：{report.summary()}，{len(segments)} 个段已标记 deleted")
        return 0


async def _retention_scan(args: argparse.Namespace) -> int:
    """列出 ``created_at`` 早于阈值的 ``sealed`` 段，``--apply`` 时删对象/热段并标 tombstone。"""
    dry_run = _resolve_dry_run(args)
    cutoff = datetime.now(UTC) - timedelta(days=args.older_than_days)
    async with _open_runtime() as runtime:
        async with runtime.session_factory() as session:
            result = await session.execute(
                text(_RETENTION_SQL), {"cutoff": cutoff, "limit": args.limit}
            )
            rows = [dict(row) for row in result.mappings().all()]
        if not rows:
            print(f"没有 created_at 早于 {cutoff.isoformat()} 的已 sealed 段")
            return 0
        if dry_run:
            print(
                f"[dry-run] {len(rows)} 个已 sealed 段早于 {cutoff.isoformat()}，"
                "将删除以下对象并把段标记为 deleted："
            )
            for row in rows:
                print(f"  {row['object_key']}（segment={row['segment_id']}）")
            print("[dry-run] 未产生任何写操作；确认后加 --apply 执行")
            return 0
        # 与 delete-thread-rollouts 同序：先删对象与热段，逐个成功后才落 tombstone。
        # retention 是**段级**删除，只做逐 key 精确清理，**不得**按 thread 清扫目录
        # （那会连带删掉同 thread 内未过期的段）。
        marked = 0
        deleted = 0
        failed: list[str] = []
        for row in rows:
            segment_id = row["segment_id"]
            object_key = row.get("object_key")
            if not object_key:
                # sealed 行按 CHECK 约束必有 object_key；真出现就显式报失败，
                # 绝不静默跳过（静默跳过正是新发现 6 的病根）。
                failed.append(str(segment_id))
                print(
                    f"段 {segment_id} 是 sealed 却没有 object_key，无法删除对象，跳过并计为失败",
                    file=sys.stderr,
                )
                continue
            report = await delete_rollout_payloads(
                object_store=runtime.object_store, object_keys=[str(object_key)]
            )
            if not report.ok:
                failed.append(str(object_key))
                print(
                    f"对象/热段删除失败，段保持 sealed 待下次 retention：{object_key}"
                    f"（{'；'.join(report.failures)}）",
                    file=sys.stderr,
                )
                continue
            deleted += report.deleted_objects
            try:
                async with runtime.session_factory() as session:
                    async with session.begin():
                        updated = await manifests_repo.mark_deleted(
                            session, segment_id=UUID(str(segment_id))
                        )
            except Exception as exc:
                failed.append(str(segment_id))
                print(f"段 {segment_id} 标记 deleted 失败（对象已删）：{exc}", file=sys.stderr)
                continue
            if updated:
                marked += 1
        print(
            f"retention：标记 deleted {marked} 个段，删除 {deleted} 个对象，失败 {len(failed)} 项"
        )
        return 1 if failed else 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def _add_dry_run_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不做任何修改")


def _add_mutation_flags(parser: argparse.ArgumentParser) -> None:
    _add_dry_run_flag(parser)
    parser.add_argument("--apply", action="store_true", help="实际写入；默认 dry-run")


def _non_negative_days(value: str) -> int:
    """``--older-than-days`` 只接受非负整数（负值会把"未来"也算进保留期）。"""
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数天数") from exc
    if days < 0:
        raise argparse.ArgumentTypeError("天数不得为负")
    return days


def build_parser() -> argparse.ArgumentParser:
    """构建 rollout 运维命令入口。"""
    parser = argparse.ArgumentParser(prog="backend.conversation.cli.rollout")
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify-manifest", help="只读校验 manifest 与对象/本地段是否一致")
    verify.add_argument("--limit", type=int, default=1000, help="单次扫描的最大行数")
    _add_dry_run_flag(verify)
    verify.set_defaults(func=_verify_manifest)

    reconcile = sub.add_parser("reconcile-orphans", help="扫描不一致并修复可自动处理的部分")
    reconcile.add_argument("--limit", type=int, default=1000, help="单次扫描的最大行数")
    _add_mutation_flags(reconcile)
    reconcile.set_defaults(func=_reconcile_orphans)

    export = sub.add_parser("export-thread", help="把 thread 的全部段拼成一个 JSONL 导出")
    export.add_argument("--thread-id", required=True, type=UUID)
    export.add_argument("--output", help="导出文件路径；省略则写 stdout")
    _add_dry_run_flag(export)
    export.set_defaults(func=_export_thread)

    delete = sub.add_parser("delete-thread-rollouts", help="标记 thread 全部段 deleted 并删除对象")
    delete.add_argument("--thread-id", required=True, type=UUID)
    _add_mutation_flags(delete)
    delete.set_defaults(func=_delete_thread_rollouts)

    retention = sub.add_parser("retention-scan", help="清理超过保留期的已 sealed 段")
    retention.add_argument(
        "--older-than-days", required=True, type=_non_negative_days, help="保留天数阈值"
    )
    retention.add_argument("--limit", type=int, default=1000, help="单次处理的最大段数")
    _add_mutation_flags(retention)
    retention.set_defaults(func=_retention_scan)
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
