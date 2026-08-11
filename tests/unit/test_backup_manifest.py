"""备份 manifest 校验单元测试（§21.4 / §23.3）。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.memory.backup import BackupError, validate_manifest

_BATCH_ID = uuid4()


def _manifest() -> dict[str, object]:
    return {
        "batch_id": str(_BATCH_ID),
        "created_at": "2026-08-11T12:00:00+00:00",
        "encryption_method": "age-x25519-v1",
        "artifacts": {
            "postgres": {"file": "postgres.dump.age", "sha256": "a" * 64, "size_bytes": 1},
            "markdown": {"file": "markdown.tar.gz.age", "sha256": "b" * 64, "size_bytes": 1},
        },
    }


def test_validate_manifest_ok() -> None:
    validate_manifest(_manifest(), batch_id=_BATCH_ID, encryption_method="age-x25519-v1")


def test_validate_manifest_rejects_batch_mismatch() -> None:
    with pytest.raises(BackupError, match="batch_id"):
        validate_manifest(_manifest(), batch_id=uuid4(), encryption_method="age-x25519-v1")


def test_validate_manifest_rejects_method_mismatch() -> None:
    with pytest.raises(BackupError, match="加密方法不匹配"):
        validate_manifest(_manifest(), batch_id=_BATCH_ID, encryption_method="age-x25519-v2")


def test_validate_manifest_rejects_missing_artifacts() -> None:
    manifest = _manifest()
    manifest["artifacts"] = {"postgres": {"file": "x", "sha256": "a" * 64}}
    with pytest.raises(BackupError, match="markdown"):
        validate_manifest(manifest, batch_id=_BATCH_ID, encryption_method="age-x25519-v1")


def test_validate_manifest_rejects_incomplete_entry() -> None:
    manifest = _manifest()
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, dict)
    artifacts["postgres"] = {"file": "postgres.dump.age"}
    with pytest.raises(BackupError, match="postgres"):
        validate_manifest(manifest, batch_id=_BATCH_ID, encryption_method="age-x25519-v1")
