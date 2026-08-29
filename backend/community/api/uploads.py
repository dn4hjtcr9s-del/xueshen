"""Community 图片上传接口（community-rebuild-plan.md §八 #16）。"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Header, UploadFile

from backend.auth.context import AuthContext
from backend.community.contracts.api import CommunityAttachmentSummary
from backend.community.contracts.errors import CommunityContentInvalidError
from backend.community.services.attachment_service import AttachmentUploadService
from backend.shared.auth_context import get_auth_context

from .dependencies import get_attachment_upload_service, rate_limit

router = APIRouter(prefix="/api/v1/community", tags=["community"])

_IDEMPOTENCY_KEY_RE = re.compile(r"^[\x21-\x7e]{1,200}$")


@router.post(
    "/uploads",
    response_model=CommunityAttachmentSummary,
    status_code=201,
    dependencies=[Depends(rate_limit("community.upload"))],
)
async def upload_attachment(
    file: UploadFile,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    auth: AuthContext = Depends(get_auth_context),
    service: AttachmentUploadService = Depends(get_attachment_upload_service),
) -> CommunityAttachmentSummary:
    if not _IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise CommunityContentInvalidError("Idempotency-Key 格式非法", field="Idempotency-Key")
    return await service.upload(
        uploader_id=auth.user_id,
        source=file.file,
        content_type=file.content_type,
        original_filename=file.filename,
        idempotency_key=idempotency_key,
    )
