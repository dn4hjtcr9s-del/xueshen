"""本地存储文件读取（development 使用，生产由 CDN 替代）。

`GET /api/v1/community/local-uploads/{storage_key:path}`（§7.12，§八 #19）：
- 仅 backend=local 挂载，flag 无关（§八 #19 仅受 backend 控制）；
- 无认证依赖（图片公开语义，D46 中唯一完全绕过认证的路由）；
- 限流组 community.read（§7.13）。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from backend.community.contracts.errors import CommunityNotFoundError
from backend.community.persistence import attachments as attachments_repo
from backend.community.storage.base import StorageBackend
from backend.community.storage.local import LocalStorage

from .dependencies import get_community_runtime, get_storage, rate_limit

router = APIRouter(prefix="/api/v1/community", tags=["community"])

#: key 格式校验（§7.12 冻结）
_STORAGE_KEY_RE = re.compile(r"^community/\d{4}-\d{2}/[0-9a-f-]{36}\.(jpg|png|webp)$")


@router.get("/local-uploads/{key:path}")
async def serve_local_upload(
    request: Request,
    key: str,
    storage: StorageBackend = Depends(get_storage),
    _rate: None = Depends(rate_limit("community.read")),
) -> FileResponse:
    """按 §7.12 查找规则读取图片字节；Content-Type 以数据库 `mime` 为准。"""
    if not isinstance(storage, LocalStorage):
        raise CommunityNotFoundError("仅本地存储模式支持此端点")
    # ① key 格式校验
    if not _STORAGE_KEY_RE.match(key):
        raise CommunityNotFoundError("文件不存在")
    # ② 查 community_attachments，无记录 → 404
    session_factory = get_community_runtime(request).database.session_factory
    async with session_factory() as session:
        row = await attachments_repo.get_attachment_by_storage_key(session, key)
    if row is None:
        raise CommunityNotFoundError("文件不存在")
    # ③ 状态门槛：uploaded/attached/deleted 放行；orphaned → 404
    if str(row["status"]) == "orphaned":
        raise CommunityNotFoundError("文件不存在")
    # ④ realpath 位于 uploads 根内（防路径穿越）
    try:
        file_path = storage.resolve_file(key)
    except ValueError:
        raise CommunityNotFoundError("文件不存在") from None
    # ⑤ 文件不存在 → 404
    if not file_path.exists():
        raise CommunityNotFoundError("文件不存在")
    # Content-Type 一律以数据库 mime 为准（§7.12）
    return FileResponse(file_path, media_type=str(row["mime"]))
