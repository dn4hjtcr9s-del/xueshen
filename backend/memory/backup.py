"""备份与恢复核心逻辑（规格 §21.4 / §13.14）。

- 产物：PostgreSQL 逻辑备份（pg_dump -Fc）、Markdown 存储 tar.gz、manifest.json，
  统一写入 `{backup_root}/{batch_id}/`；数据库与 Markdown 产物用 age 加密，
  manifest 不含用户内容、保持明文以便恢复前校验。
- 临时文件写入 `{batch_id}.tmp/`，成功后原子改名为 `{batch_id}/`。
- 任一环节失败：backup_runs 记 failed + error_summary，并清理临时目录。
- pg_dump/pg_restore 经 `docker compose exec -T postgres` 执行（容器自带客户端，
  版本与服务端一致，本机零安装）。
- 恢复：产物定位以磁盘 `{backup_root}/{batch_id}/` 为准（全新灾难恢复目标没有
  backup_runs 行）；目标库已有该批次行时交叉校验状态与 manifest checksum。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from backend.memory.maintenance_gate import (
    MaintenanceGate,
    RestoreAbortedError,
)
from backend.memory.persistence import account_deletion as deletion_repo
from backend.memory.persistence import backup_runs as backup_repo
from backend.settings import Settings

logger = logging.getLogger("memory.backup")

POSTGRES_DUMP_NAME = "postgres.dump"
MARKDOWN_TAR_NAME = "markdown.tar.gz"
MANIFEST_NAME = "manifest.json"

# 备份 manifest 的格式版本（§21.4 manifest.schema_version）。
BACKUP_MANIFEST_SCHEMA_VERSION = 1

# manifest artifacts 条目只允许这两个固定相对文件名（评审 #4：路径不得逃逸 BACKUP_ROOT，
# 从源头杜绝 manifest 携带绝对路径或 ../ 注入）。
EXPECTED_ARTIFACT_FILES = {
    "postgres": f"{POSTGRES_DUMP_NAME}.age",
    "markdown": f"{MARKDOWN_TAR_NAME}.age",
}

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class BackupError(Exception):
    """备份/恢复失败；message 写入 backup_runs.error_summary（受控摘要）。"""

    @property
    def message(self) -> str:
        return str(self)


@dataclass(frozen=True)
class _DbTarget:
    user: str
    name: str


def _db_target(settings: Settings) -> _DbTarget:
    parsed = urlparse(settings.database_url)
    return _DbTarget(user=parsed.username or "memory", name=parsed.path.lstrip("/") or "memory")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _run_command(*args: str, stdout_path: Path | None = None) -> None:
    """执行外部命令；非零退出抛 BackupError（只保留错误码与尾部摘要）。"""
    stdout_handle = stdout_path.open("wb") if stdout_path else None
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=stdout_handle if stdout_handle else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
    finally:
        if stdout_handle:
            stdout_handle.close()
    if process.returncode != 0:
        tail = stderr.decode("utf-8", errors="replace")[-300:].strip()
        raise BackupError(f"命令失败({args[0]}): exit={process.returncode} {tail}")


def validate_manifest(
    manifest: dict[str, object], *, batch_id: UUID, encryption_method: str
) -> None:
    """恢复/验证前的 manifest 校验（§21.4）：批次、加密方法、产物条目、规格字段。

    规格 §21.4 要求 manifest 至少包含 schema_version / migration revision /
    图谱 manifest checksum / 账号删除 watermark / manifest 自身 checksum 等字段；
    缺任一字段即拒绝（评审 #13）。artifacts 条目文件名必须等于固定常量，
    不得携带绝对路径或目录穿越（评审 #4）。
    """
    if manifest.get("batch_id") != str(batch_id):
        raise BackupError("manifest batch_id 与 backup_runs 不一致")
    if manifest.get("encryption_method") != encryption_method:
        raise BackupError(f"加密方法不匹配: manifest={manifest.get('encryption_method')}")
    for key in (
        "schema_version",
        "migration_revision",
        "graph_manifest_checksum",
        "account_deletion_ledger",
        "manifest_checksum",
    ):
        if key not in manifest:
            raise BackupError(f"manifest 缺少规格字段: {key}")
    if manifest.get("schema_version") != BACKUP_MANIFEST_SCHEMA_VERSION:
        raise BackupError(f"不支持的 manifest schema_version: {manifest.get('schema_version')}")
    # manifest 自身 checksum：对其余字段的 canonical JSON 重算比对（防篡改）
    declared = manifest.get("manifest_checksum")
    from backend.memory.contracts.common import canonical_json

    payload = {k: v for k, v in manifest.items() if k != "manifest_checksum"}
    actual = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    if declared != actual:
        raise BackupError("manifest 自身 checksum 不匹配")
    _validate_migration_revision(manifest.get("migration_revision"))
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise BackupError("manifest 缺少 artifacts")
    for key in ("postgres", "markdown"):
        entry = artifacts.get(key)
        if not isinstance(entry, dict) or not entry.get("sha256") or not entry.get("file"):
            raise BackupError(f"manifest artifacts.{key} 不完整")
        if entry["file"] != EXPECTED_ARTIFACT_FILES[key]:
            raise BackupError(
                f"manifest artifacts.{key}.file 非法: {entry['file']}（仅允许固定相对文件名）"
            )


def _alembic_config(*, database_url: str | None = None) -> Config:
    """构造不依赖当前工作目录的 Alembic 配置。"""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    if database_url is not None:
        # env.py 优先读取 attributes，避免恢复临时库时误用进程默认 DATABASE_URL。
        config.attributes["database_url"] = database_url
    return config


def _migration_script() -> ScriptDirectory:
    return ScriptDirectory.from_config(_alembic_config())


def _current_migration_head() -> str:
    heads = _migration_script().get_heads()
    if len(heads) != 1:
        raise BackupError(f"当前 Alembic migration 必须只有一个 head，实际为: {heads}")
    return heads[0]


def _validate_migration_revision(value: object) -> str:
    """确保备份 revision 是当前唯一 Alembic head 的祖先或 head 本身。"""
    if not isinstance(value, str) or not value.strip():
        raise BackupError("manifest migration_revision 必须是非空字符串")
    revision = value.strip()
    script = _migration_script()
    heads = script.get_heads()
    if len(heads) != 1:
        raise BackupError(f"当前 Alembic migration 必须只有一个 head，实际为: {heads}")
    try:
        resolved = script.get_revision(revision)
    except Exception as exc:
        raise BackupError(f"manifest migration_revision 未知: {revision}") from exc
    if resolved is None or resolved.revision != revision:
        raise BackupError(f"manifest migration_revision 必须使用完整 revision: {revision}")
    ancestors = {item.revision for item in script.walk_revisions(base="base", head=heads[0])}
    if revision not in ancestors:
        raise BackupError(
            f"manifest migration_revision 不属于当前 head {heads[0]} 的升级链: {revision}"
        )
    return revision


def _upgrade_database_to_head(database_url: str) -> None:
    """在工作线程中把恢复目标升级到当前唯一 Alembic head。"""
    command.upgrade(_alembic_config(database_url=database_url), "head")


def _build_manifest(
    *,
    batch_id: UUID,
    settings: Settings,
    postgres_plain: Path,
    markdown_plain: Path,
    migration_revision: str | None,
    graph_manifest_checksum: str | None,
    deletion_ledger: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    from backend.memory.contracts.common import canonical_json

    manifest: dict[str, object] = {
        "schema_version": BACKUP_MANIFEST_SCHEMA_VERSION,
        "batch_id": str(batch_id),
        "created_at": datetime.now(UTC).isoformat(),
        "encryption_method": settings.backup_encryption_method,
        # 应用 migration revision（§21.4）：备份时 alembic_version.version_num
        "migration_revision": migration_revision,
        # 图谱 manifest checksum（§21.4）：最近已应用的知识图谱同步 manifest checksum
        "graph_manifest_checksum": graph_manifest_checksum,
        # 评审 P0-2/#13：内嵌账号删除 ledger 快照（只含 user_hash 与完成证明），
        # 全新环境灾难恢复时据此重建 ops ledger 并重放 purge。
        "account_deletion_ledger": deletion_ledger or [],
        "artifacts": {
            "postgres": {
                "file": EXPECTED_ARTIFACT_FILES["postgres"],
                "sha256": _sha256(postgres_plain),
                "size_bytes": postgres_plain.stat().st_size,
            },
            "markdown": {
                "file": EXPECTED_ARTIFACT_FILES["markdown"],
                "sha256": _sha256(markdown_plain),
                "size_bytes": markdown_plain.stat().st_size,
            },
        },
    }
    # manifest 自身 checksum（§21.4）：对其余字段 canonical JSON 的 sha256
    manifest["manifest_checksum"] = hashlib.sha256(
        canonical_json(manifest).encode("utf-8")
    ).hexdigest()
    return manifest


def _check_encryption_config(settings: Settings) -> None:
    if settings.backup_encryption_method != "age-x25519-v1":
        raise BackupError(f"不支持的加密方法: {settings.backup_encryption_method}")
    if not settings.backup_age_recipient:
        raise BackupError("未配置 BACKUP_AGE_RECIPIENT")
    if not settings.backup_age_identity_file:
        raise BackupError("未配置 BACKUP_AGE_IDENTITY_FILE")


# ---------------------------------------------------------------------------
# 阻塞文件操作（sync helper，经 asyncio.to_thread 调用）
# ---------------------------------------------------------------------------


def _tar_storage(storage_root: Path, markdown_plain: Path) -> None:
    storage_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(markdown_plain, "w:gz") as tar:
        tar.add(storage_root, arcname="markdown")


def _finalize_dir(tmp_dir: Path, final_dir: Path) -> None:
    """原子改名（§21.4）：临时目录 → 正式目录。"""
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir.rename(final_dir)


def _write_manifest_file(manifest_path: Path, manifest: dict[str, object]) -> None:
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _storage_non_empty(storage_root: Path) -> bool:
    return storage_root.exists() and any(storage_root.iterdir())


async def _current_migration_revision(session: AsyncSession) -> str | None:
    """备份时的 alembic migration revision（§21.4 manifest 字段）。"""
    exists = await session.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'alembic_version'"
        )
    )
    if int(exists.scalar_one()) == 0:
        return None
    row = await session.execute(text("SELECT version_num FROM alembic_version"))
    return row.scalar_one_or_none()


async def _latest_graph_manifest_checksum(session: AsyncSession) -> str | None:
    """最近已应用的知识图谱同步 manifest checksum（§21.4 manifest 字段）。"""
    exists = await session.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'knowledge_graph_sync_runs'"
        )
    )
    if int(exists.scalar_one()) == 0:
        return None
    row = await session.execute(
        text(
            "SELECT manifest_checksum FROM knowledge_graph_sync_runs "
            "WHERE applied = true ORDER BY applied_at DESC LIMIT 1"
        )
    )
    return row.scalar_one_or_none()


async def _non_empty_public_tables(session: AsyncSession) -> list[str]:
    """public schema 中已有数据的表（评审 #3：恢复前非空检查覆盖全部表）。

    alembic_version 只记录 migration 版本，不算用户数据；ops schema 是环境
    本地状态（账号删除 ledger），不属于 public，不在检查范围内。
    """
    rows = await session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name <> 'alembic_version' "
            "ORDER BY table_name"
        )
    )
    non_empty: list[str] = []
    for (table_name,) in rows.all():
        # 表名来自 information_schema，非外部输入；双引号包裹防注入
        count = await session.execute(text(f'SELECT COUNT(*) FROM "{table_name}"'))
        if int(count.scalar_one()) > 0:
            non_empty.append(str(table_name))
    return non_empty


def _extract_markdown(markdown_plain: Path, storage_root: Path, force: bool, work: Path) -> None:
    if force and storage_root.exists():
        shutil.rmtree(storage_root)
    storage_root.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(markdown_plain, "r:gz") as tar:
        tar.extractall(work / "markdown-out", filter="data")
    extracted = work / "markdown-out" / "markdown"
    if extracted.exists():
        extracted.rename(storage_root)


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------


async def create_backup(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> UUID:
    """执行一次完整备份批次，返回 batch_id（§21.4）。失败批次也落库。"""
    _check_encryption_config(settings)
    batch_id = uuid4()
    backup_root = Path(settings.backup_root)
    final_dir = backup_root / str(batch_id)
    tmp_dir = backup_root / f"{batch_id}.tmp"
    artifacts = {
        "postgres": final_dir / f"{POSTGRES_DUMP_NAME}.age",
        "markdown": final_dir / f"{MARKDOWN_TAR_NAME}.age",
        "manifest": final_dir / MANIFEST_NAME,
    }
    async with session_factory() as session:
        async with session.begin():
            await backup_repo.insert_run(
                session,
                batch_id=batch_id,
                backup_root=str(backup_root),
                postgres_artifact=str(artifacts["postgres"]),
                markdown_artifact=str(artifacts["markdown"]),
                manifest_artifact=str(artifacts["manifest"]),
            )
    try:
        await asyncio.to_thread(tmp_dir.mkdir, parents=True, exist_ok=False)
        postgres_plain = tmp_dir / POSTGRES_DUMP_NAME
        markdown_plain = tmp_dir / MARKDOWN_TAR_NAME

        target = _db_target(settings)
        await _run_command(
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "-U",
            target.user,
            "-Fc",
            # ops schema（账号删除 ledger）是环境本地状态，不进备份；
            # 其快照内嵌于 manifest，灾难恢复时据此重建。
            "--exclude-schema=ops",
            target.name,
            stdout_path=postgres_plain,
        )
        await asyncio.to_thread(_tar_storage, Path(settings.memory_storage_root), markdown_plain)

        async with session_factory() as session:
            ledger_snapshot = await deletion_repo.list_ledger_entries(session)
            migration_revision = await _current_migration_revision(session)
            graph_manifest_checksum = await _latest_graph_manifest_checksum(session)
        _validate_migration_revision(migration_revision)
        manifest = await asyncio.to_thread(
            _build_manifest,
            batch_id=batch_id,
            settings=settings,
            postgres_plain=postgres_plain,
            markdown_plain=markdown_plain,
            migration_revision=migration_revision,
            graph_manifest_checksum=graph_manifest_checksum,
            deletion_ledger=[
                {
                    "account_deletion_id": str(entry["account_deletion_id"]),
                    "user_hash": entry["user_hash"],
                    "user_hash_key_version": entry["user_hash_key_version"],
                    "status": entry["status"],
                    "requested_at": entry["requested_at"].isoformat(),
                    "purge_completed_at": (
                        entry["purge_completed_at"].isoformat()
                        if entry["purge_completed_at"]
                        else None
                    ),
                    "completion_proof_checksum": entry["completion_proof_checksum"],
                }
                for entry in ledger_snapshot
            ],
        )
        manifest_path = tmp_dir / MANIFEST_NAME
        await asyncio.to_thread(_write_manifest_file, manifest_path, manifest)

        recipient = settings.backup_age_recipient or ""
        for plain, name in (
            (postgres_plain, POSTGRES_DUMP_NAME),
            (markdown_plain, MARKDOWN_TAR_NAME),
        ):
            await _run_command(
                "age", "-r", recipient, "-o", str(tmp_dir / f"{name}.age"), str(plain)
            )
            await asyncio.to_thread(plain.unlink)

        await asyncio.to_thread(_finalize_dir, tmp_dir, final_dir)
        manifest_checksum = await asyncio.to_thread(_sha256, final_dir / MANIFEST_NAME)
        async with session_factory() as session:
            async with session.begin():
                await backup_repo.mark_succeeded(
                    session,
                    batch_id=batch_id,
                    postgres_checksum=manifest["artifacts"]["postgres"]["sha256"],  # type: ignore[index]
                    markdown_checksum=manifest["artifacts"]["markdown"]["sha256"],  # type: ignore[index]
                    manifest_checksum=manifest_checksum,
                    completed_at=datetime.now(UTC),
                )
        logger.info("备份批次成功: batch=%s", batch_id)
        return batch_id
    except Exception as exc:
        summary = exc.message if isinstance(exc, BackupError) else f"{type(exc).__name__}: {exc}"
        summary = summary[:1000]
        async with session_factory() as session:
            async with session.begin():
                await backup_repo.mark_failed(
                    session,
                    batch_id=batch_id,
                    error_summary=summary,
                    completed_at=datetime.now(UTC),
                )
        await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)
        logger.error("备份批次失败: batch=%s error=%s", batch_id, summary)
        raise BackupError(summary) from exc


# ---------------------------------------------------------------------------
# 恢复验证（每周）
# ---------------------------------------------------------------------------


async def _decrypt_and_check(
    *,
    identity: str,
    artifact_path: Path,
    decrypted: Path,
    expected_sha256: str,
    label: str,
) -> None:
    await _run_command("age", "-d", "-i", identity, "-o", str(decrypted), str(artifact_path))
    if await asyncio.to_thread(_sha256, decrypted) != expected_sha256:
        raise BackupError(f"{label} 产物 checksum 不匹配")


async def verify_backup_restore(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_id: UUID,
) -> None:
    """每周恢复验证（§21.4）：隔离目录解密 + manifest/checksum 校验 + 状态更新。"""
    _check_encryption_config(settings)
    async with session_factory() as session:
        run = await backup_repo.get_run(session, batch_id)
    if run is None:
        raise BackupError(f"backup_runs 不存在: {batch_id}")
    if run["status"] != "succeeded":
        raise BackupError(f"批次状态非 succeeded: {run['status']}")

    error: str | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="memory-restore-verify-") as isolated:
            work = Path(isolated)
            manifest_path = Path(run["manifest_artifact"])
            if await asyncio.to_thread(_sha256, manifest_path) != run["manifest_checksum"]:
                raise BackupError("manifest checksum 不匹配")
            manifest = json.loads(
                await asyncio.to_thread(manifest_path.read_text, encoding="utf-8")
            )
            validate_manifest(
                manifest,
                batch_id=batch_id,
                encryption_method=settings.backup_encryption_method,
            )
            identity = settings.backup_age_identity_file or ""
            artifacts = manifest["artifacts"]
            assert isinstance(artifacts, dict)
            for key, checksum_column in (
                ("postgres", "postgres_checksum"),
                ("markdown", "markdown_checksum"),
            ):
                entry = artifacts[key]
                assert isinstance(entry, dict)
                if entry["sha256"] != run[checksum_column]:
                    raise BackupError(f"{key} 产物 checksum 与 backup_runs 不一致")
                await _decrypt_and_check(
                    identity=identity,
                    artifact_path=Path(run["backup_root"]) / str(batch_id) / str(entry["file"]),
                    decrypted=work / str(entry["file"]).removesuffix(".age"),
                    expected_sha256=str(entry["sha256"]),
                    label=str(key),
                )
    except Exception as exc:
        error = exc.message if isinstance(exc, BackupError) else f"{type(exc).__name__}: {exc}"
        error = error[:1000]

    async with session_factory() as session:
        async with session.begin():
            await backup_repo.mark_restore_verified(
                session,
                batch_id=batch_id,
                status="failed" if error else "succeeded",
                error=error,
                verified_at=datetime.now(UTC),
            )
    if error:
        raise BackupError(error)


# ---------------------------------------------------------------------------
# 覆盖性恢复
# ---------------------------------------------------------------------------


async def restore_backup(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_id: UUID,
    force: bool = False,
) -> list[str]:
    """恢复一个备份批次（§21.4）。返回实际重放 purge 的 account_deletion_id 列表。

    默认只允许恢复到空目标；--force 显式覆盖。manifest/checksum/加密元数据
    全部校验及解密通过后才激活全局 maintenance gate。门禁等待已有在线工作
    排空并独占流量锁，随后重置 public、pg_restore、升级 migration 到当前 head、
    恢复 Markdown 并同步重放账号删除。全部成功才解锁；任一失败保持门禁激活，
    数据继续视为不可信（评审二轮 P0-1 / P1-5）。
    """
    _check_encryption_config(settings)
    backup_dir = Path(settings.backup_root) / str(batch_id)
    manifest_path = backup_dir / MANIFEST_NAME
    if not await asyncio.to_thread(manifest_path.exists):
        raise BackupError(f"manifest 不存在: {manifest_path}")

    with tempfile.TemporaryDirectory(prefix="memory-restore-") as isolated:
        work = Path(isolated)
        try:
            manifest = json.loads(
                await asyncio.to_thread(manifest_path.read_text, encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BackupError(f"manifest 读取或解析失败: {exc}") from exc
        if not isinstance(manifest, dict):
            raise BackupError("manifest 顶层必须是 JSON object")
        validate_manifest(
            manifest, batch_id=batch_id, encryption_method=settings.backup_encryption_method
        )
        identity = settings.backup_age_identity_file or ""
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, dict)
        plain_paths: dict[str, Path] = {}
        backup_root_resolved = backup_dir.resolve()
        for key in ("postgres", "markdown"):
            entry = artifacts[key]
            assert isinstance(entry, dict)
            # 防御纵深（评审 #4）：validate_manifest 已强制固定文件名，
            # 这里再确认解析后的绝对路径不逃逸批次目录
            artifact_path = (backup_dir / str(entry["file"])).resolve()
            if not artifact_path.is_relative_to(backup_root_resolved):
                raise BackupError(f"artifact 路径逃逸 BACKUP_ROOT: {entry['file']}")
            decrypted = work / str(entry["file"]).removesuffix(".age")
            await _decrypt_and_check(
                identity=identity,
                artifact_path=artifact_path,
                decrypted=decrypted,
                expected_sha256=str(entry["sha256"]),
                label=str(key),
            )
            plain_paths[key] = decrypted

        engine = session_factory.kw.get("bind")
        if engine is None:
            raise BackupError("恢复流程无法取得数据库引擎")
        gate = MaintenanceGate(engine)
        try:
            async with gate.restore(force=force, reason=f"restore-backup:{batch_id}"):
                # DROP public 前快照环境本地 ledger；ops schema 不参与 pg_restore。
                async with session_factory() as session:
                    async with session.begin():
                        await deletion_repo.ensure_ops_schema(session)
                        pre_restore_ledger = await deletion_repo.list_ledger_entries(session)

                # 获得排他流量锁后再检查目标，消除检查与覆盖之间的在线写竞态。
                async with session_factory() as session:
                    non_empty_tables = await _non_empty_public_tables(session)
                    run: dict[str, Any] | None = None
                    runs_table = await session.execute(
                        text(
                            "SELECT COUNT(*) FROM information_schema.tables "
                            "WHERE table_schema = 'public' AND table_name = 'backup_runs'"
                        )
                    )
                    if int(runs_table.scalar_one()) == 1:
                        run = await backup_repo.get_run(session, batch_id)
                storage_root = Path(settings.memory_storage_root)
                if not force and (
                    non_empty_tables or await asyncio.to_thread(_storage_non_empty, storage_root)
                ):
                    detail = (
                        f"非空表: {', '.join(non_empty_tables)}"
                        if non_empty_tables
                        else "存储目录非空"
                    )
                    raise RestoreAbortedError(f"目标非空（{detail}）；覆盖现有环境必须显式 --force")
                if run is not None:
                    if run["status"] != "succeeded":
                        raise BackupError(f"批次状态非 succeeded: {run['status']}")
                    if await asyncio.to_thread(_sha256, manifest_path) != run["manifest_checksum"]:
                        raise BackupError("manifest checksum 与 backup_runs 不匹配")

                target = _db_target(settings)
                # 重置 public schema：pg_restore 面对干净库，避免 "already exists" 报错。
                await _run_command(
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    target.user,
                    "-d",
                    target.name,
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
                )
                # pg_restore 从 stdin 读取 dump。
                with plain_paths["postgres"].open("rb") as dump_handle:
                    process = await asyncio.create_subprocess_exec(
                        "docker",
                        "compose",
                        "exec",
                        "-T",
                        "postgres",
                        "pg_restore",
                        "-U",
                        target.user,
                        "-d",
                        target.name,
                        "--no-owner",
                        stdin=dump_handle,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await process.communicate()
                if process.returncode != 0:
                    tail = stderr.decode("utf-8", errors="replace")[-300:].strip()
                    raise BackupError(f"pg_restore 失败: exit={process.returncode} {tail}")

                declared_revision = _validate_migration_revision(manifest["migration_revision"])
                async with session_factory() as session:
                    restored_revision = await _current_migration_revision(session)
                if restored_revision != declared_revision:
                    raise BackupError(
                        "恢复出的 alembic_version 与 manifest 不一致: "
                        f"database={restored_revision}, manifest={declared_revision}"
                    )

                await asyncio.to_thread(_upgrade_database_to_head, settings.database_url)
                head = _current_migration_head()
                async with session_factory() as session:
                    upgraded_revision = await _current_migration_revision(session)
                if upgraded_revision != head:
                    raise BackupError(
                        f"恢复后 migration 未到当前 head: database={upgraded_revision}, head={head}"
                    )

                await asyncio.to_thread(
                    _extract_markdown, plain_paths["markdown"], storage_root, force, work
                )

                # 合并 ops ledger、备份内嵌快照与恢复出的 manifest 三方删除记录；
                # 同步重放全部成功后 maintenance gate 才会解除。
                return await _replay_account_deletions(
                    settings=settings,
                    session_factory=session_factory,
                    pre_restore_ledger=pre_restore_ledger,
                    manifest=manifest,
                )
        except RestoreAbortedError as exc:
            raise BackupError(str(exc)) from exc
        except BackupError as exc:
            # 只有离开 gate.restore 后才会到这里：若它仍是本次 owner，说明失败发生
            # 在恢复正文内，门禁已经保持 active；若进入正文前就被拒绝（已有失败
            # 恢复或并发 restore），不得把对方的维护状态误归因于本次调用。
            raise BackupError(str(exc)) from exc
        except Exception as exc:
            # gate.restore 在正文异常时保持 active；调用方统一得到 BackupError。
            raise BackupError(f"恢复失败，维护门禁保持激活: {exc}") from exc


def _status_rank(status: object) -> int:
    return {"failed": 0, "requested": 1, "running": 2, "completed": 3}.get(str(status), 0)


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


async def _replay_account_deletions(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    pre_restore_ledger: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> list[str]:
    """恢复后重放账号删除（§21.4 / 评审 P0-2）。

    返回实际执行 purge 的 account_deletion_id 列表；任一失败抛 BackupError，
    保证清理全部完成前恢复不算成功、不应对外提供用户级服务。
    """
    from backend.memory.contracts.common import user_privacy_hash
    from backend.memory.services.account_purge import purge_user_account
    from backend.memory.storage.local_markdown import LocalMarkdownStore

    entries: dict[str, dict[str, Any]] = {}

    def _merge(entry: dict[str, Any]) -> None:
        key = str(entry["account_deletion_id"])
        existing = entries.get(key)
        if existing is None or _status_rank(entry.get("status")) >= _status_rank(
            existing.get("status")
        ):
            entries[key] = entry

    for raw in manifest.get("account_deletion_ledger") or []:  # 备份内嵌快照（DR 场景）
        if isinstance(raw, dict):
            _merge(raw)
    for entry in pre_restore_ledger:  # 同环境存活的 ops ledger
        _merge(entry)
    async with session_factory() as session:
        for row in await deletion_repo.list_manifests_for_replay(session):
            _merge(row)  # 恢复出的 public manifest（旧备份内记录的删除意图）

    if not entries:
        return []

    # user_hash → internal_user_id 反查（对恢复数据中的身份映射逐一计算摘要）
    async with session_factory() as session:
        rows = await session.execute(
            text("SELECT DISTINCT internal_user_id FROM account_identity_mappings")
        )
        hash_to_user = {
            user_privacy_hash(settings.privacy_hmac_key, str(row[0])): row[0] for row in rows.all()
        }

    # 全量回写 ledger：水位在任何后续恢复中持续存活
    async with session_factory() as session:
        async with session.begin():
            for entry in entries.values():
                await deletion_repo.upsert_ledger_entry(
                    session,
                    account_deletion_id=UUID(str(entry["account_deletion_id"])),
                    user_hash=str(entry["user_hash"]),
                    user_hash_key_version=str(
                        entry.get("user_hash_key_version") or settings.privacy_hmac_key_version
                    ),
                    status=str(entry["status"]),
                    requested_at=_as_datetime(entry["requested_at"]),
                    purge_completed_at=(
                        _as_datetime(entry["purge_completed_at"])
                        if entry.get("purge_completed_at")
                        else None
                    ),
                    completion_proof_checksum=entry.get("completion_proof_checksum"),
                )

    to_purge = [
        (entry, hash_to_user[str(entry["user_hash"])])
        for entry in entries.values()
        if str(entry["user_hash"]) in hash_to_user
    ]
    if not to_purge:
        return []

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from backend.memory.worker.checkpoint import CheckpointCleanupAdapter
    from backend.memory.worker.main import _psycopg_conninfo

    store = LocalMarkdownStore(settings.memory_storage_root)
    replayed: list[str] = []
    async with AsyncPostgresSaver.from_conn_string(_psycopg_conninfo(settings)) as saver:
        await saver.setup()  # 幂等：目标库可能尚未创建 langgraph checkpoint 表
        adapter = CheckpointCleanupAdapter(saver=saver)
        for entry, user_id in to_purge:
            deletion_id = UUID(str(entry["account_deletion_id"]))
            try:
                await purge_user_account(
                    session_factory,
                    settings=settings,
                    store=store,
                    checkpoint_cleanup=adapter,
                    user_id=user_id,
                    account_deletion_id=deletion_id,
                    now=datetime.now(UTC),
                    requested_at=_as_datetime(entry["requested_at"]),
                )
            except Exception as exc:
                raise BackupError(f"账号删除重放失败({deletion_id}): {exc}") from exc
            replayed.append(str(deletion_id))
    return replayed
