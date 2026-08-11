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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.persistence import backup_runs as backup_repo
from backend.settings import Settings

logger = logging.getLogger("memory.backup")

POSTGRES_DUMP_NAME = "postgres.dump"
MARKDOWN_TAR_NAME = "markdown.tar.gz"
MANIFEST_NAME = "manifest.json"


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
    """恢复/验证前的 manifest 校验（§21.4）：批次、加密方法、产物条目。"""
    if manifest.get("batch_id") != str(batch_id):
        raise BackupError("manifest batch_id 与 backup_runs 不一致")
    if manifest.get("encryption_method") != encryption_method:
        raise BackupError(f"加密方法不匹配: manifest={manifest.get('encryption_method')}")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise BackupError("manifest 缺少 artifacts")
    for key in ("postgres", "markdown"):
        entry = artifacts.get(key)
        if not isinstance(entry, dict) or not entry.get("sha256") or not entry.get("file"):
            raise BackupError(f"manifest artifacts.{key} 不完整")


def _build_manifest(
    *,
    batch_id: UUID,
    settings: Settings,
    postgres_plain: Path,
    markdown_plain: Path,
) -> dict[str, object]:
    return {
        "batch_id": str(batch_id),
        "created_at": datetime.now(UTC).isoformat(),
        "encryption_method": settings.backup_encryption_method,
        "artifacts": {
            "postgres": {
                "file": f"{POSTGRES_DUMP_NAME}.age",
                "sha256": _sha256(postgres_plain),
                "size_bytes": postgres_plain.stat().st_size,
            },
            "markdown": {
                "file": f"{MARKDOWN_TAR_NAME}.age",
                "sha256": _sha256(markdown_plain),
                "size_bytes": markdown_plain.stat().st_size,
            },
        },
    }


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
            target.name,
            stdout_path=postgres_plain,
        )
        await asyncio.to_thread(_tar_storage, Path(settings.memory_storage_root), markdown_plain)

        manifest = await asyncio.to_thread(
            _build_manifest,
            batch_id=batch_id,
            settings=settings,
            postgres_plain=postgres_plain,
            markdown_plain=markdown_plain,
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
    """恢复一个备份批次（§21.4）。返回需要重放账号删除的 account_deletion_id 列表。

    默认只允许恢复到空目标；--force 显式覆盖。manifest/checksum/加密元数据
    全部校验通过后才写入目标，写入前先重置 public schema 保证 pg_restore
    面对的是干净库。
    """
    _check_encryption_config(settings)
    backup_dir = Path(settings.backup_root) / str(batch_id)
    manifest_path = backup_dir / MANIFEST_NAME
    if not await asyncio.to_thread(manifest_path.exists):
        raise BackupError(f"manifest 不存在: {manifest_path}")

    # 目标环境检查：数据库与存储目录必须为空，除非 --force
    async with session_factory() as session:
        tables = await session.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name IN "
                "('memory_documents', 'memory_operations')"
            )
        )
        counts = {"docs": 0, "ops": 0}
        if int(tables.scalar_one()) == 2:
            row = await session.execute(
                text(
                    "SELECT (SELECT COUNT(*) FROM memory_documents) AS docs, "
                    "(SELECT COUNT(*) FROM memory_operations) AS ops"
                )
            )
            counts = dict(row.mappings().one())
        # backup_runs 行存在时交叉校验（灾难恢复新库可能还没有该行）
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
        counts["docs"] > 0
        or counts["ops"] > 0
        or await asyncio.to_thread(_storage_non_empty, storage_root)
    ):
        raise BackupError("目标非空（数据库或存储目录已有数据）；覆盖现有环境必须显式 --force")
    if run is not None:
        if run["status"] != "succeeded":
            raise BackupError(f"批次状态非 succeeded: {run['status']}")
        if await asyncio.to_thread(_sha256, manifest_path) != run["manifest_checksum"]:
            raise BackupError("manifest checksum 与 backup_runs 不匹配")

    with tempfile.TemporaryDirectory(prefix="memory-restore-") as isolated:
        work = Path(isolated)
        manifest = json.loads(await asyncio.to_thread(manifest_path.read_text, encoding="utf-8"))
        validate_manifest(
            manifest, batch_id=batch_id, encryption_method=settings.backup_encryption_method
        )
        identity = settings.backup_age_identity_file or ""
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, dict)
        plain_paths: dict[str, Path] = {}
        for key in ("postgres", "markdown"):
            entry = artifacts[key]
            assert isinstance(entry, dict)
            decrypted = work / str(entry["file"]).removesuffix(".age")
            await _decrypt_and_check(
                identity=identity,
                artifact_path=backup_dir / str(entry["file"]),
                decrypted=decrypted,
                expected_sha256=str(entry["sha256"]),
                label=str(key),
            )
            plain_paths[key] = decrypted

        target = _db_target(settings)
        # 重置 public schema：pg_restore 面对干净库，避免 "already exists" 报错
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
        # pg_restore 从 stdin 读取 dump
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

        await asyncio.to_thread(
            _extract_markdown, plain_paths["markdown"], storage_root, force, work
        )

    # §21.4：恢复旧备份后必须重新应用 account_deletion_manifest
    async with session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT account_deletion_id FROM account_deletion_manifest "
                "WHERE status = 'completed'"
            )
        )
        return [str(r[0]) for r in rows.fetchall()]
