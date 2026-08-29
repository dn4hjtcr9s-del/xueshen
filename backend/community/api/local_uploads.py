"""本地存储文件读取（development 使用，生产由 CDN 替代）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from backend.community.storage.base import StorageBackend
from backend.community.storage.local import LocalStorage

from .dependencies import get_storage

router = APIRouter(prefix="/api/v1/community", tags=["community"])


@router.get("/local-uploads/{key:path}")
async def serve_local_upload(
    key: str,
    storage: StorageBackend = Depends(get_storage),
) -> FileResponse:
    if not isinstance(storage, LocalStorage):
        raise HTTPException(status_code=404, detail="仅本地存储模式支持此端点")
    file_path = storage.resolve_file(key)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path)
