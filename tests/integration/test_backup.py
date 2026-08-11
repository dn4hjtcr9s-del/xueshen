"""备份/恢复集成测试（§21.4 / §23.3）：真实 pg_dump（docker compose）+ age 加解密。

覆盖：成功批次产物与 backup_runs 记账、失败批次记录、恢复验证状态更新、
篡改产物被 checksum 校验拦截。restore_backup 的覆盖性恢复不进自动化测试，
按运维手册手动演练（避免误伤开发库）。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory import backup as backup_module
from backend.memory.backup import (
    BackupError,
    create_backup,
    verify_backup_restore,
)
from backend.memory.persistence import backup_runs as backup_repo
from backend.settings import Settings

pytestmark = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("docker") is None,
    reason="需要 age 与 docker（备份加密与 pg_dump 依赖）",
)


@pytest.fixture(scope="module")
def age_keypair(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """模块级临时 age 密钥对：返回 (recipient, identity_file)。"""
    key_file = tmp_path_factory.mktemp("age") / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key_file)], check=True, capture_output=True)
    recipient = ""
    for line in key_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("# public key:"):
            recipient = line.split(":", 1)[1].strip()
    assert recipient.startswith("age1")
    return recipient, str(key_file)


def _backup_settings(
    settings: Settings, tmp_path: Path, age_keypair: tuple[str, str], **overrides: Any
) -> Settings:
    recipient, identity_file = age_keypair
    base: dict[str, Any] = {
        "app_env": "test",
        "memory_storage_root": str(tmp_path / "storage"),
        "backup_root": str(tmp_path / "backups"),
        "backup_age_recipient": recipient,
        "backup_age_identity_file": identity_file,
    }
    base.update(overrides)
    merged = {**settings.model_dump(), **base}
    return Settings(**merged)


async def test_create_backup_success_and_verify(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    age_keypair: tuple[str, str],
) -> None:
    backup_settings = _backup_settings(settings, tmp_path, age_keypair)
    # 准备一点 Markdown 数据，确保 tar 非空
    (Path(backup_settings.memory_storage_root) / "users").mkdir(parents=True)
    (Path(backup_settings.memory_storage_root) / "users" / "probe.md").write_text(
        "probe", encoding="utf-8"
    )

    batch_id = await create_backup(backup_settings, session_factory)

    async with session_factory() as session:
        run = await backup_repo.get_run(session, batch_id)
    assert run is not None
    assert run["status"] == "succeeded"
    assert len(run["postgres_checksum"]) == 64
    assert len(run["markdown_checksum"]) == 64
    assert len(run["manifest_checksum"]) == 64
    final_dir = Path(backup_settings.backup_root) / str(batch_id)
    assert (final_dir / "postgres.dump.age").exists()
    assert (final_dir / "markdown.tar.gz.age").exists()
    assert (final_dir / "manifest.json").exists()
    assert not (Path(backup_settings.backup_root) / f"{batch_id}.tmp").exists()
    assert run["restore_verification_status"] == "pending"

    # 每周恢复验证：状态 pending → succeeded
    await verify_backup_restore(backup_settings, session_factory, batch_id=batch_id)
    async with session_factory() as session:
        run = await backup_repo.get_run(session, batch_id)
    assert run is not None
    assert run["restore_verification_status"] == "succeeded"
    assert run["restore_verified_at"] is not None
    assert run["restore_verification_error"] is None


async def test_failed_batch_recorded(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    age_keypair: tuple[str, str],
    monkeypatch: Any,
) -> None:
    """pg_dump 失败：批次记 failed + error_summary，临时目录被清理。"""
    backup_settings = _backup_settings(settings, tmp_path, age_keypair)

    async def _failing_command(*args: str, stdout_path: Path | None = None) -> None:
        raise BackupError("模拟 pg_dump 失败")

    monkeypatch.setattr(backup_module, "_run_command", _failing_command)

    with pytest.raises(BackupError, match="模拟 pg_dump 失败"):
        await create_backup(backup_settings, session_factory)

    async with session_factory() as session:
        result = await session.execute(
            text("SELECT batch_id, status, error_summary FROM backup_runs")
        )
        rows = result.mappings().all()
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "模拟 pg_dump 失败" in rows[0]["error_summary"]

    def _leftover_tmp_dirs() -> list[Path]:
        backup_root = Path(backup_settings.backup_root)
        return list(backup_root.glob("*.tmp")) if backup_root.exists() else []

    assert await asyncio.to_thread(_leftover_tmp_dirs) == []


async def test_verify_detects_tampered_artifact(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    age_keypair: tuple[str, str],
) -> None:
    """篡改加密产物：恢复验证失败并记录 restore_verification_error。"""
    backup_settings = _backup_settings(settings, tmp_path, age_keypair)
    batch_id = await create_backup(backup_settings, session_factory)

    artifact = Path(backup_settings.backup_root) / str(batch_id) / "postgres.dump.age"
    data = bytearray(artifact.read_bytes())
    data[-10] ^= 0xFF
    artifact.write_bytes(bytes(data))

    with pytest.raises(BackupError):
        await verify_backup_restore(backup_settings, session_factory, batch_id=batch_id)
    async with session_factory() as session:
        run = await backup_repo.get_run(session, batch_id)
    assert run is not None
    assert run["restore_verification_status"] == "failed"
    assert run["restore_verification_error"]


async def test_verify_rejects_unknown_batch(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    age_keypair: tuple[str, str],
) -> None:
    backup_settings = _backup_settings(settings, tmp_path, age_keypair)
    with pytest.raises(BackupError, match="backup_runs 不存在"):
        await verify_backup_restore(backup_settings, session_factory, batch_id=uuid4())
