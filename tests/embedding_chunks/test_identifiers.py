"""验证内容哈希、溯源哈希和 UUIDv5 chunk ID 的稳定性。"""

from __future__ import annotations

from uuid import UUID

from scripts.embedding_chunks.identifiers import content_hash, source_hash, stable_chunk_id
from scripts.embedding_chunks.schemas import SourceRef


def _ref(*, raw_hash: str = "a" * 64) -> SourceRef:
    return SourceRef(
        source_page=7,
        mineru_page_index=6,
        block_index=3,
        source_chunk_id="0001_pages",
        source_pdf="book.pdf",
        element_type="text",
        bbox=(1, 2, 3, 4),
        raw_hash=raw_hash,
    )


def test_content_hash_normalizes_line_endings_and_trailing_space() -> None:
    assert content_hash("  定理  \r\n证明\t\r\n") == content_hash("定理\n证明")
    assert content_hash("定理\n证明") != content_hash("定理\n结论")


def test_source_hash_is_stable_and_tracks_exact_source_fields() -> None:
    refs = (_ref(),)

    assert source_hash(refs) == source_hash(tuple(refs))
    assert source_hash(refs) != source_hash((_ref(raw_hash="b" * 64),))


def test_stable_chunk_id_is_uuid5_and_changes_with_semantic_identity() -> None:
    values = {
        "book_id": "01_book",
        "chapter_path": ("第一章", "第一节"),
        "content_role": "theorem",
        "content_hash_value": content_hash("定理内容"),
        "source_hash_value": source_hash((_ref(),)),
    }

    first = stable_chunk_id(**values)
    second = stable_chunk_id(**values)
    changed = stable_chunk_id(**{**values, "content_role": "proof"})

    assert first == second
    assert UUID(first).version == 5
    assert changed != first
