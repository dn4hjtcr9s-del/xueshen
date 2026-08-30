"""附件上传服务（community-rebuild-plan.md §7.10/§7.11/§7.12）。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.community.contracts.api import CommunityAttachmentSummary
from backend.community.contracts.errors import (
    CommunityIdempotencyConflictError,
    CommunityUploadFailedError,
)
from backend.community.persistence import attachments as attachments_repo
from backend.community.persistence import idempotency as idem_repo
from backend.community.persistence.idempotency import delete_request_by_resource
from backend.community.storage.base import StorageBackend
from backend.community.storage.validation import validate_and_measure_image
from backend.settings import Settings

_STORAGE_KEY_RE = re.compile(r"^community/\d{4}-\d{2}/[0-9a-f-]{36}\.(jpg|png|webp)$")


def _generate_storage_key(ext: str) -> str:
    now = datetime.now(UTC)
    return f"community/{now.strftime('%Y-%m')}/{uuid4()}{ext}"


def sanitize_original_filename(raw: str | None) -> str:
    """§7.3 original_filename 清洗（冻结伪代码）。"""
    if raw is None:
        return ""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(
        ch
        for ch in name
        if unicodedata.category(ch) != "Cf" and not (ord(ch) < 0x20 or ord(ch) == 0x7F)
    )
    name = name.strip()
    name = unicodedata.normalize("NFC", name)
    return name[:100]


class AttachmentUploadService:
    """处理图片上传：校验 → 幂等 → 存储 → 落库。"""

    def __init__(
        self,
        settings: Settings,
        storage: StorageBackend,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self.storage = storage
        self._session_factory = session_factory

    async def upload(
        self,
        *,
        uploader_id: UUID,
        source: BinaryIO,
        content_type: str | None,
        original_filename: str | None,
        idempotency_key: str,
    ) -> CommunityAttachmentSummary:
        # 1) 事务外 Pillow 校验
        spool, mime, ext, width, height, size_bytes = await validate_and_measure_image(
            source, content_type, self.settings
        )

        # 2) 计算文件内容 hash 作为幂等 payload
        spool.seek(0)
        file_sha256 = hashlib.sha256(spool.read()).hexdigest()
        spool.seek(0)
        payload_hash = file_sha256

        # 3) 幂等抢占与执行：同键同文件 → 重放原附件；同键不同文件 → 冲突
        storage_key = _generate_storage_key(ext)
        attachment_id = uuid4()
        async with self._session_factory() as session:
            async with session.begin():
                existing = await idem_repo.get_request(
                    session,
                    user_id=uploader_id,
                    operation="upload_attachment",
                    idempotency_key=idempotency_key,
                )
                if existing is not None:
                    if existing["payload_hash"] != payload_hash:
                        raise CommunityIdempotencyConflictError("同 Idempotency-Key 已用于不同文件")
                    row = await attachments_repo.get_attachment_by_id(
                        session, existing["resource_id"]
                    )
                    if row is None:
                        raise CommunityIdempotencyConflictError("幂等记录指向的附件不存在")
                    return CommunityAttachmentSummary(
                        attachment_id=row["attachment_id"],
                        url=self.storage.public_url(row["storage_key"]),
                        mime=row["mime"],
                        width=row["width"],
                        height=row["height"],
                        size_bytes=row["size_bytes"],
                    )

                won = await idem_repo.insert_request(
                    session,
                    user_id=uploader_id,
                    operation="upload_attachment",
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    resource_type="attachment",
                    resource_id=attachment_id,
                    retention_days=self.settings.community_idempotency_retention_days,
                )
                if not won:
                    # 并发竞争失败：重读幂等记录
                    existing = await idem_repo.get_request(
                        session,
                        user_id=uploader_id,
                        operation="upload_attachment",
                        idempotency_key=idempotency_key,
                    )
                    if existing is None:
                        raise CommunityIdempotencyConflictError("幂等键竞争失败")
                    if existing["payload_hash"] != payload_hash:
                        raise CommunityIdempotencyConflictError("同 Idempotency-Key 已用于不同文件")
                    row = await attachments_repo.get_attachment_by_id(
                        session, existing["resource_id"]
                    )
                    if row is None:
                        raise CommunityIdempotencyConflictError("幂等记录指向的附件不存在")
                    return CommunityAttachmentSummary(
                        attachment_id=row["attachment_id"],
                        url=self.storage.public_url(row["storage_key"]),
                        mime=row["mime"],
                        width=row["width"],
                        height=row["height"],
                        size_bytes=row["size_bytes"],
                    )

                # 4) 事务保持打开，执行 Kodo/local 上传
                result = await self.storage.upload(storage_key, spool, mime, size_bytes)
                spool.close()
                if not result.success:
                    raise CommunityUploadFailedError(
                        result.error_message or "上传失败",
                        retryable=_is_retryable_upload_error(result.error_message or ""),
                    )

                # 5) INSERT attachments
                await attachments_repo.insert_attachment(
                    session,
                    attachment_id=attachment_id,
                    uploader_id=uploader_id,
                    storage_key=storage_key,
                    original_filename=sanitize_original_filename(original_filename),
                    mime=mime,
                    size_bytes=size_bytes,
                    width=width,
                    height=height,
                )

        return CommunityAttachmentSummary(
            attachment_id=attachment_id,
            url=self.storage.public_url(storage_key),
            mime=mime,
            width=width,
            height=height,
            size_bytes=size_bytes,
        )

    async def delete_attachment(
        self,
        attachment_id: UUID,
        requester_id: UUID,
        is_admin: bool = False,
    ) -> None:
        """管理/补偿路径：校验所有权后删除存储并物理删除行。"""
        async with self._session_factory() as session:
            row = await attachments_repo.get_attachment_by_id(session, attachment_id)
            if row is None:
                return
            if not is_admin and row["uploader_id"] != requester_id:
                from backend.community.contracts.errors import AdminRequiredError

                raise AdminRequiredError("无权删除该附件")

        # 存储删除在事务外执行：避免长事务持有网络 IO
        result = await self.storage.delete(row["storage_key"])
        if not result.success:
            raise CommunityUploadFailedError(
                result.error_message or "删除失败",
                retryable=True,
            )

        async with self._session_factory() as session:
            async with session.begin():
                await delete_request_by_resource(
                    session, resource_type="attachment", resource_id=attachment_id
                )
                await session.execute(
                    text("DELETE FROM community_attachments WHERE attachment_id = :id"),
                    {"id": attachment_id},
                )


def _is_retryable_upload_error(error_message: str) -> bool:
    """粗略判断：5xx/超时/连接异常 → retryable；其余 false。"""
    low = error_message.lower()
    if "timeout" in low or "连接" in low or "conn" in low:
        return True
    if "服务错误" in low or "5" in low:
        return True
    return False
