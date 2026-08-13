"""备份 manifest 强校验单元测试（评审 #4 路径逃逸 / #13 规格字段）。"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.memory.backup import BackupError, validate_manifest
from backend.memory.contracts.common import canonical_json


def _manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "batch_id": str(uuid4()),
        "created_at": "2026-08-12T00:00:00+00:00",
        "encryption_method": "age-x25519-v1",
        "migration_revision": "0004_account_deletion_ledger",
        "graph_manifest_checksum": None,
        "account_deletion_ledger": [],
        "artifacts": {
            "postgres": {"file": "postgres.dump.age", "sha256": "ab" * 32, "size_bytes": 1},
            "markdown": {"file": "markdown.tar.gz.age", "sha256": "cd" * 32, "size_bytes": 1},
        },
    }
    manifest["manifest_checksum"] = _seal(manifest)
    return manifest


def _seal(manifest: dict[str, Any]) -> str:
    payload = {k: v for k, v in manifest.items() if k != "manifest_checksum"}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _reseal(manifest: dict[str, Any]) -> dict[str, Any]:
    """篡改后重算自身 checksum：隔离路径/字段校验与防篡改校验。"""
    manifest = {k: v for k, v in manifest.items() if k != "manifest_checksum"}
    manifest["manifest_checksum"] = _seal(manifest)
    return manifest


def _check(manifest: dict[str, Any]) -> None:
    validate_manifest(
        manifest,
        batch_id=UUID(str(manifest["batch_id"])),
        encryption_method="age-x25519-v1",
    )


def test_validate_manifest_accepts_well_formed() -> None:
    _check(_manifest())


@pytest.mark.parametrize(
    "bad_file",
    ["../evil.dump.age", "/etc/passwd", "nested/postgres.dump.age", "./postgres.dump.age"],
)
def test_validate_manifest_rejects_artifact_path_escape(bad_file: str) -> None:
    """评审 #4：artifact 文件名必须等于固定常量，绝对路径/目录穿越一律拒绝。"""
    manifest = _manifest()
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, dict)
    postgres = artifacts["postgres"]
    assert isinstance(postgres, dict)
    postgres["file"] = bad_file
    with pytest.raises(BackupError, match="非法"):
        _check(_reseal(manifest))


@pytest.mark.parametrize(
    "missing",
    [
        "schema_version",
        "migration_revision",
        "graph_manifest_checksum",
        "account_deletion_ledger",
        "manifest_checksum",
    ],
)
def test_validate_manifest_rejects_missing_spec_fields(missing: str) -> None:
    """评审 #13：规格 §21.4 要求的字段缺任一即拒绝。"""
    manifest = _manifest()
    del manifest[missing]
    if missing != "manifest_checksum":
        manifest = _reseal(manifest)
    with pytest.raises(BackupError, match=f"缺少规格字段: {missing}"):
        _check(manifest)


def test_validate_manifest_rejects_wrong_schema_version() -> None:
    manifest = _manifest()
    manifest["schema_version"] = 2
    with pytest.raises(BackupError, match="schema_version"):
        _check(_reseal(manifest))


def test_validate_manifest_detects_tampering_without_reseal() -> None:
    """篡改任一字段但未重算自身 checksum：防篡改校验必须拦截。"""
    manifest = _manifest()
    manifest["migration_revision"] = "9999"
    with pytest.raises(BackupError, match="自身 checksum 不匹配"):
        _check(manifest)


@pytest.mark.parametrize(
    "revision",
    [None, 123, "", "9999_unknown", "0004"],
)
def test_validate_manifest_rejects_invalid_migration_revision(revision: object) -> None:
    """恢复前必须拒绝空值、非字符串、未知值和缩写 revision。"""
    manifest = _manifest()
    manifest["migration_revision"] = revision
    with pytest.raises(BackupError, match="migration_revision"):
        _check(_reseal(manifest))


@pytest.mark.parametrize(
    "revision",
    ["0004_account_deletion_ledger", "0006_global_maintenance_gate"],
)
def test_validate_manifest_accepts_current_head_and_ancestor(revision: str) -> None:
    """当前 head 与同一升级链上的祖先 revision 都可用于兼容性升级。"""
    manifest = _manifest()
    manifest["migration_revision"] = revision
    _check(_reseal(manifest))
