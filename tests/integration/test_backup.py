"""备份/恢复集成测试（§21.4 / §23.3）：真实 pg_dump（docker compose）+ age 加解密。

覆盖：成功批次产物与 backup_runs 记账、失败批次记录、恢复验证状态更新、
篡改产物被 checksum 校验拦截；覆盖恢复均在临时数据库内执行，以验证门禁、
migration 升级与失败 fail-closed 语义，同时避免误伤开发或运行环境。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from backend.memory import backup as backup_module
from backend.memory.backup import (
    BackupError,
    create_backup,
    restore_backup,
    validate_manifest,
    verify_backup_restore,
)
from backend.memory.maintenance_gate import (
    MaintenanceActiveError,
    MaintenanceGate,
)
from backend.memory.persistence import backup_runs as backup_repo
from backend.memory.persistence.database import create_engine, create_session_factory
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


@asynccontextmanager
async def _temporary_restore_environment(
    settings: Settings,
    tmp_path: Path,
    age_keypair: tuple[str, str],
    *,
    revision: str = "head",
) -> AsyncIterator[tuple[Settings, async_sessionmaker[AsyncSession]]]:
    """创建仅供恢复测试使用的独立数据库，避免触碰开发/运行环境。"""
    parsed = urlparse(settings.database_url)
    db_user = parsed.username or "memory"
    temp_db = f"memory_restore_{uuid4().hex[:8]}"
    temp_url = settings.database_url.rsplit("/", 1)[0] + f"/{temp_db}"
    await asyncio.to_thread(
        subprocess.run,
        ["docker", "compose", "exec", "-T", "postgres", "createdb", "-U", db_user, temp_db],
        check=True,
        capture_output=True,
    )
    engine = None
    try:
        config = Config("alembic.ini")
        config.attributes["database_url"] = temp_url
        await asyncio.to_thread(command.upgrade, config, revision)
        backup_settings = _backup_settings(settings, tmp_path, age_keypair, database_url=temp_url)
        engine = create_engine(backup_settings)
        yield backup_settings, create_session_factory(engine)
    finally:
        if engine is not None:
            await engine.dispose()
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "compose", "exec", "-T", "postgres", "dropdb", "-U", db_user, temp_db],
            check=False,
            capture_output=True,
        )


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


async def test_restore_rejects_non_empty_target_without_force(
    settings: Settings,
    tmp_path: Path,
    age_keypair: tuple[str, str],
) -> None:
    """非空目标的安全中止不得污染共享开发库，也必须撤销本次维护门禁。"""
    async with _temporary_restore_environment(settings, tmp_path, age_keypair) as (
        backup_settings,
        session_factory,
    ):
        batch_id = await create_backup(backup_settings, session_factory)
        with pytest.raises(BackupError, match="目标非空"):
            await restore_backup(backup_settings, session_factory, batch_id=batch_id)

        engine = session_factory.kw.get("bind")
        assert engine is not None
        assert await MaintenanceGate(engine).is_active() is False


async def test_restore_upgrade_failure_keeps_gate_active_and_blocks_traffic(
    settings: Settings,
    tmp_path: Path,
    age_keypair: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复已覆盖目标后若迁移失败，隔离库必须继续 fail-closed。"""
    async with _temporary_restore_environment(settings, tmp_path, age_keypair) as (
        backup_settings,
        session_factory,
    ):
        batch_id = await create_backup(backup_settings, session_factory)

        def _failing_upgrade(_database_url: str) -> None:
            raise RuntimeError("injected migration failure")

        monkeypatch.setattr(backup_module, "_upgrade_database_to_head", _failing_upgrade)
        with pytest.raises(BackupError, match="injected migration failure"):
            await restore_backup(backup_settings, session_factory, batch_id=batch_id, force=True)

        engine = session_factory.kw.get("bind")
        assert engine is not None
        gate = MaintenanceGate(engine)
        assert await gate.is_active() is True
        with pytest.raises(MaintenanceActiveError):
            async with gate.traffic():
                pass


async def test_restore_upgrades_ancestor_revision_before_replay(
    settings: Settings,
    tmp_path: Path,
    age_keypair: tuple[str, str],
) -> None:
    """恢复 0004 备份时，必须先升级至当前 head 才能报告成功。"""
    async with _temporary_restore_environment(
        settings,
        tmp_path,
        age_keypair,
    ) as (backup_settings, session_factory):
        legacy_config = Config("alembic.ini")
        legacy_config.attributes["database_url"] = backup_settings.database_url
        await asyncio.to_thread(command.downgrade, legacy_config, "0004_account_deletion_ledger")

        batch_id = await create_backup(backup_settings, session_factory)

        current_config = Config("alembic.ini")
        current_config.attributes["database_url"] = backup_settings.database_url
        await asyncio.to_thread(command.upgrade, current_config, "head")

        replayed = await restore_backup(
            backup_settings, session_factory, batch_id=batch_id, force=True
        )
        assert replayed == []
        async with session_factory() as session:
            version = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
        assert version == "0006_global_maintenance_gate"

        engine = session_factory.kw.get("bind")
        assert engine is not None
        assert await MaintenanceGate(engine).is_active() is False


async def test_manifest_contains_spec_fields(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    age_keypair: tuple[str, str],
) -> None:
    """评审 #13 / §21.4：manifest 包含 schema_version、migration revision、
    图谱 manifest checksum、账号删除 watermark 与自身 checksum，且通过强校验。"""
    import hashlib
    import json

    from backend.memory.contracts.common import canonical_json

    backup_settings = _backup_settings(settings, tmp_path, age_keypair)
    batch_id = await create_backup(backup_settings, session_factory)
    manifest = json.loads(
        (Path(backup_settings.backup_root) / str(batch_id) / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["schema_version"] == 1
    async with session_factory() as session:
        revision = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    assert manifest["migration_revision"] == revision
    assert "graph_manifest_checksum" in manifest
    assert isinstance(manifest["account_deletion_ledger"], list)
    payload = {k: v for k, v in manifest.items() if k != "manifest_checksum"}
    expected = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    assert manifest["manifest_checksum"] == expected
    # 强校验接受合法 manifest（恢复/验证共用同一入口）
    validate_manifest(
        manifest,
        batch_id=batch_id,
        encryption_method=backup_settings.backup_encryption_method,
    )


async def test_restore_replays_completed_account_deletion(
    settings: Settings,
    tmp_path: Path,
    age_keypair: tuple[str, str],
) -> None:
    """评审 P0-2 灾难恢复演练：T0 备份 → T1 完成账号删除 → T2 恢复 T0。

    全程在独立临时数据库中进行（不动开发库）；ops.account_deletion_ledger
    不被恢复重置，恢复后必须自动重放 purge 并再次物理删除该用户数据。
    """
    from backend.memory.contracts.common import user_privacy_hash
    from backend.memory.persistence import account_deletion as deletion_repo
    from backend.memory.persistence.identity import IdentityMappingRepository
    from backend.memory.services.account_purge import purge_user_account
    from backend.memory.storage.local_markdown import LocalMarkdownStore

    parsed = urlparse(settings.database_url)
    db_user = parsed.username or "memory"
    temp_db = f"memory_restore_{uuid4().hex[:8]}"
    temp_url = settings.database_url.rsplit("/", 1)[0] + f"/{temp_db}"

    await asyncio.to_thread(
        subprocess.run,
        ["docker", "compose", "exec", "-T", "postgres", "createdb", "-U", db_user, temp_db],
        check=True,
        capture_output=True,
    )
    try:
        env = {**__import__("os").environ, "DATABASE_URL": temp_url}
        await asyncio.to_thread(
            subprocess.run,
            ["uv", "run", "alembic", "upgrade", "head"],
            check=True,
            capture_output=True,
            env=env,
        )
        backup_settings = _backup_settings(settings, tmp_path, age_keypair, database_url=temp_url)
        engine = create_engine(backup_settings)
        sf = create_session_factory(engine)
        store = LocalMarkdownStore(backup_settings.memory_storage_root)
        user = uuid4()
        try:
            # T0 前：播种用户数据与 Markdown
            async with sf() as session:
                async with session.begin():
                    await IdentityMappingRepository(session).create(
                        internal_user_id=user,
                        issuer="https://accounts.example",
                        external_subject="ext-restore-1",
                    )
                    await session.execute(
                        text(
                            "INSERT INTO memory_documents (user_id, memory_id, memory_type, "
                            "logical_path, active_version, active_storage_key, active_checksum) "
                            "VALUES (:u, 'learner', 'learner', 'learner.md', 1, 'k1', :ck)"
                        ),
                        {"u": user, "ck": "ab" * 32},
                    )
            user_dir = (
                Path(backup_settings.memory_storage_root) / "users" / str(user)[:2] / str(user)
            )
            user_dir.mkdir(parents=True)
            (user_dir / "learner.md").write_text("v1", encoding="utf-8")

            batch_id = await create_backup(backup_settings, sf)

            # T1：完成账号删除（ledger 写入 temp 库的 ops schema）
            user_hash = user_privacy_hash(backup_settings.privacy_hmac_key, str(user))
            async with sf() as session:
                async with session.begin():
                    await deletion_repo.insert_manifest(
                        session,
                        account_deletion_id=uuid4(),
                        user_hash=user_hash,
                        user_hash_key_version=backup_settings.privacy_hmac_key_version,
                        requested_at=datetime.now(UTC),
                        backup_retention_until=datetime.now(UTC),
                    )
            summary = await purge_user_account(
                sf,
                settings=backup_settings,
                store=store,
                checkpoint_cleanup=None,
                user_id=user,
                account_deletion_id=None,
                now=datetime.now(UTC),
            )
            assert summary.markdown_tree_deleted
            assert not user_dir.exists()

            # T2：恢复 T0 备份（--force：temp 库已有 ops ledger 等非空状态）
            replayed = await restore_backup(backup_settings, sf, batch_id=batch_id, force=True)

            assert len(replayed) == 1
            async with sf() as session:
                docs = await session.execute(
                    text("SELECT COUNT(*) FROM memory_documents WHERE user_id = :u"),
                    {"u": user},
                )
                assert int(docs.scalar_one()) == 0, "恢复后用户数据必须被重放 purge 清除"
                mappings = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM account_identity_mappings WHERE internal_user_id = :u"
                    ),
                    {"u": user},
                )
                assert int(mappings.scalar_one()) == 0
                manifest = await deletion_repo.get_manifest_by_user_hash(
                    session, user_hash=user_hash
                )
                assert manifest is not None
                assert manifest["status"] == "completed"
                ledger = await deletion_repo.list_ledger_entries(session)
                assert len(ledger) == 1
                assert ledger[0]["status"] == "completed"
            assert not user_dir.exists(), "恢复重建的 Markdown 必须被重放 purge 删除"
        finally:
            await engine.dispose()
    finally:
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "compose", "exec", "-T", "postgres", "dropdb", "-U", db_user, temp_db],
            check=False,
            capture_output=True,
        )
