"""附件上传服务（community-rebuild-plan.md §7.10/§7.11/§7.12）。"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.community.contracts.api import CommunityAttachmentSummary
from backend.community.contracts.errors import CommunityUploadFailedError
from backend.community.persistence import attachments as attachments_repo
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

        # 幂等键抢占在调用方（post_command_service 同模式），
        # 上传服务内部只负责执行存储与落库。
        storage_key = _generate_storage_key(ext)

        # 3) 事务保持打开，执行 Kodo/local 上传
        result = await self.storage.upload(storage_key, spool, mime, size_bytes)
        spool.close()
        if not result.success:
            raise CommunityUploadFailedError(
                result.error_message or "上传失败",
                retryable=_is_retryable_upload_error(result.error_message or ""),
            )

        # 4) INSERT attachments
        attachment_id = uuid4()
        async with self._session_factory() as session:
            async with session.begin():
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
    ) -> None:
        """管理/补偿路径：直接删除存储并物理删除行。"""
        async with self._session_factory() as session:
            async with session.begin():
                row = await attachments_repo.get_attachment_by_id(session, attachment_id)
                if row is None:
                    return
                result = await self.storage.delete(row["storage_key"])
                if not result.success:
                    raise CommunityUploadFailedError(
                        result.error_message or "删除失败",
                        retryable=True,
                    )
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
