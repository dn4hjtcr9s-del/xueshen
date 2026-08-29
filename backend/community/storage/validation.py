"""社区图片上传校验（community-rebuild-plan.md §7.10）。"""

from __future__ import annotations

from tempfile import SpooledTemporaryFile
from typing import BinaryIO

from PIL import Image

from backend.community.contracts.errors import (
    UploadBombRejectedError,
    UploadInvalidTypeError,
    UploadTooLargeError,
)
from backend.settings import Settings

# MIME 映射表冻结（§7.10）
FORMAT_TO_MIME: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
FORMAT_TO_EXT: dict[str, str] = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}
ALLOWED_MIMES: frozenset[str] = frozenset(FORMAT_TO_MIME.values())

# 默认内存阈值 1MiB，超过则落盘
_SPOOL_MAX_SIZE = 1024 * 1024
_READ_CHUNK = 64 * 1024


def normalize_content_type(content_type: str | None) -> str:
    """小写归一、去 ; 参数；缺失视为空字符串。"""
    if content_type is None:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def _mime_allowed(content_type: str) -> bool:
    return content_type in ALLOWED_MIMES


def _stream_to_spooled(
    source: BinaryIO,
    max_size: int,
) -> SpooledTemporaryFile[bytes]:
    """流式读入 SpooledTemporaryFile；超 max_size 即断。"""
    spool = SpooledTemporaryFile(max_size=_SPOOL_MAX_SIZE, mode="w+b")
    total = 0
    while True:
        chunk = source.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            spool.close()
            raise UploadTooLargeError(f"图片超过 {max_size} bytes")
        spool.write(chunk)
    spool.seek(0)
    return spool


async def validate_and_measure_image(
    source: BinaryIO,
    content_type: str | None,
    settings: Settings,
) -> tuple[SpooledTemporaryFile[bytes], str, str, int, int, int]:
    """校验图片并返回 (spooled_file, mime, ext, width, height, size_bytes)。

    异常：
      - UploadTooLargeError (>5MiB)
      - UploadInvalidTypeError（白名单/解码失败/GIF）
      - UploadBombRejectedError（像素超阈值）
    """
    max_bytes = settings.community_image_max_bytes
    max_pixels = settings.community_image_max_pixels

    spool = _stream_to_spooled(source, max_bytes)
    size_bytes = spool.tell()
    spool.seek(0)

    # 先用 Pillow verify 做完整性校验
    try:
        with Image.open(spool) as img:
            img.verify()
    except Exception as exc:
        spool.close()
        raise UploadInvalidTypeError(f"无法解码图片: {exc}") from exc

    # verify 后需重新 open 才能读 format/size
    spool.seek(0)
    try:
        with Image.open(spool) as img:
            fmt = img.format
            width, height = img.size
    except Exception as exc:
        spool.close()
        raise UploadInvalidTypeError(f"无法读取图片信息: {exc}") from exc

    if fmt not in FORMAT_TO_MIME:
        spool.close()
        if fmt == "GIF":
            raise UploadInvalidTypeError("不支持 GIF 格式")
        raise UploadInvalidTypeError(f"不支持的图片格式: {fmt}")

    mime = FORMAT_TO_MIME[fmt]
    ext = FORMAT_TO_EXT[fmt]

    # Content-Type 校验：缺失/不合法走 Pillow 检测；不一致按 Pillow 校正
    normalized_ct = normalize_content_type(content_type)
    if normalized_ct:
        if normalized_ct not in ALLOWED_MIMES:
            spool.close()
            raise UploadInvalidTypeError(f"Content-Type 不在白名单: {normalized_ct}")
        # 不一致但各自合法：以 Pillow 为准（mime 已是 Pillow 结果）

    # 显式像素计算（§7.10 D36）
    if width * height > max_pixels:
        spool.close()
        raise UploadBombRejectedError(f"图片像素 {width * height} 超过阈值 {max_pixels}")

    spool.seek(0)
    return spool, mime, ext, width, height, size_bytes


def configure_image_security(settings: Settings) -> None:
    """启动时同步 Pillow MAX_IMAGE_PIXELS 作为双保险。"""
    Image.MAX_IMAGE_PIXELS = settings.community_image_max_pixels
