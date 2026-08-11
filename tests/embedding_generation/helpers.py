"""Embedding 测试共享 fixture：生成最小且哈希正确的 Chunk Artifact。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def file_hash(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes())
    return digest.hexdigest()


def write_chunk_root(root: Path, texts: list[str], *, duplicate_first: bool = False) -> Path:
    root.mkdir(parents=True)
    chunks = []
    for index, text in enumerate(texts):
        content_index = 0 if duplicate_first and index == 1 else index
        chunks.append(
            {
                "schema_version": "embedding-chunks/v1",
                "chunk_id": f"chunk-{index}",
                "chunk_index": index,
                "content_hash": f"content-{content_index}",
                "embedding_text": texts[0] if duplicate_first and index == 1 else text,
            }
        )
    chunks_path = root / "chunks.jsonl"
    chunks_path.write_text(
        "".join(json.dumps(chunk, ensure_ascii=False) + "\n" for chunk in chunks),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "embedding-chunks/v1",
        "build_id": "build-runner",
        "chunk_count": len(chunks),
        "files": {
            "chunks.jsonl": {
                "records": len(chunks),
                "sha256": file_hash(chunks_path),
            }
        },
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return root
