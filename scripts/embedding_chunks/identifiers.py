"""为 embedding chunk 生成规范化哈希和稳定 UUIDv5 标识。"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Sequence
from uuid import UUID, uuid5

from scripts.embedding_chunks.schemas import ContentRole, SourceRef

_CHUNK_NAMESPACE = UUID("ab09f8a1-f8f4-51b1-a92e-f0a60d43a51f")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    """统一 Unicode、换行和行首尾空白，避免格式噪声改变内容哈希。"""
    normalized = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.strip() for line in normalized.split("\n")).strip()


def content_hash(text: str) -> str:
    """计算规范化正文的 SHA-256。"""
    return _sha256(_normalize_text(text))


def source_hash(refs: Sequence[SourceRef]) -> str:
    """按引用顺序计算精确溯源字段的规范 SHA-256。"""
    payload = [ref.to_dict() for ref in refs]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(canonical)


def stable_chunk_id(
    *,
    book_id: str,
    chapter_path: Sequence[str],
    content_role: ContentRole,
    content_hash_value: str,
    source_hash_value: str,
) -> str:
    """根据 chunk 的语义身份生成不依赖输出顺序的 UUIDv5。"""
    identity = json.dumps(
        {
            "book_id": book_id,
            "chapter_path": list(chapter_path),
            "content_role": content_role,
            "content_hash": content_hash_value,
            "source_hash": source_hash_value,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(uuid5(_CHUNK_NAMESPACE, identity))
