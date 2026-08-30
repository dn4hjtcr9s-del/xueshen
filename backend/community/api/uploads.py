"""Community 图片上传接口（community-rebuild-plan.md §八 #16）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, UploadFile

from backend.auth.context import AuthContext
from backend.community.contracts.api import CommunityAttachmentSummary
from backend.community.services.attachment_service import AttachmentUploadService
from backend.shared.auth_context import get_auth_context

from .dependencies import (
    IDEMPOTENCY_KEY_RE,
    get_attachment_upload_service,
    rate_limit,
    require_idempotency_key,
)

router = APIRouter(prefix="/api/v1/community", tags=["community"])


@router.post(
    "/uploads",
    response_model=CommunityAttachmentSummary,
    status_code=201,
    dependencies=[Depends(rate_limit("community.upload"))],
)
async def upload_attachment(
    file: UploadFile,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", pattern=IDEMPOTENCY_KEY_RE
    ),
    auth: AuthContext = Depends(get_auth_context),
    service: AttachmentUploadService = Depends(get_attachment_upload_service),
) -> CommunityAttachmentSummary:
    # §7.11：格式非法由 Header pattern 在路由前拦截为 422 INVALID_PAYLOAD；
    # 缺失进 require_idempotency_key → 422 COMMUNITY_CONTENT_INVALID。
    key = require_idempotency_key(idempotency_key)
    return await service.upload(
        uploader_id=auth.user_id,
        source=file.file,
        content_type=file.content_type,
        original_filename=file.filename,
        idempotency_key=key,
    )
