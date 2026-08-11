"""按最终 embedding token 上限切分语义单元，并安全生成正文 overlap。"""

from __future__ import annotations

from dataclasses import dataclass

from scripts.embedding_chunks.schemas import (
    ChunkDraft,
    ExcludedRecord,
    SemanticUnit,
    SourceRef,
)
from scripts.embedding_chunks.tokenizer import Token, Tokenizer


@dataclass(frozen=True, slots=True)
class _Piece:
    text: str
    source_refs: tuple[SourceRef, ...]
    splittable: bool
    element_type: str
    overlap: bool = False


def embedding_prefix(unit: SemanticUnit) -> str:
    """构造计入 token 上限的稳定 embedding 元数据前缀。"""
    chapter = " > ".join(unit.chapter_path) if unit.chapter_path else "未分章"
    return (
        f"书名：{unit.book_name}\n"
        f"章节：{chapter}\n"
        f"内容类型：{unit.content_role}\n\n"
    )


def _join_text(pieces: list[_Piece]) -> str:
    parts: list[str] = []
    previous: _Piece | None = None
    for piece in pieces:
        text = piece.text.strip()
        if not text:
            continue
        if parts:
            parts.append(" " if previous is not None and previous.overlap else "\n\n")
        parts.append(text)
        previous = piece
    return "".join(parts)


def _unique_refs(pieces: list[_Piece]) -> tuple[SourceRef, ...]:
    seen: set[tuple[int, int]] = set()
    refs: list[SourceRef] = []
    for piece in pieces:
        for ref in piece.source_refs:
            if ref.key not in seen:
                refs.append(ref)
                seen.add(ref.key)
    return tuple(refs)


def _largest_fitting_prefix(
    *,
    piece: _Piece,
    current: list[_Piece],
    prefix: str,
    tokenizer: Tokenizer,
    chunk_size: int,
) -> tuple[_Piece | None, _Piece | None]:
    tokens = tokenizer.encode(piece.text)
    low, high, best = 1, len(tokens), 0
    while low <= high:
        middle = (low + high) // 2
        candidate_text = tokenizer.decode(tokens[:middle]).strip()
        candidate_piece = _Piece(
            candidate_text,
            piece.source_refs,
            piece.splittable,
            piece.element_type,
            piece.overlap,
        )
        if tokenizer.count(prefix + _join_text([*current, candidate_piece])) <= chunk_size:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    if best == 0:
        return None, piece
    head_text = tokenizer.decode(tokens[:best]).strip()
    tail_text = tokenizer.decode(tokens[best:]).strip()
    head = _Piece(
        head_text,
        piece.source_refs,
        piece.splittable,
        piece.element_type,
        piece.overlap,
    )
    tail = (
        _Piece(tail_text, piece.source_refs, piece.splittable, piece.element_type, False)
        if tail_text
        else None
    )
    return head, tail


def _overlap_piece(
    pieces: list[_Piece], tokenizer: Tokenizer, overlap: int
) -> _Piece | None:
    if overlap <= 0:
        return None
    eligible = [piece for piece in pieces if piece.splittable and piece.text.strip()]
    if not eligible:
        return None
    text = _join_text(eligible)
    tokens = tokenizer.encode(text)
    tail_tokens: list[Token] = tokens[-overlap:]
    tail_text = tokenizer.decode(tail_tokens).strip()
    if not tail_text:
        return None
    return _Piece(
        text=tail_text,
        source_refs=_unique_refs(eligible),
        splittable=True,
        element_type="overlap",
        overlap=True,
    )


def chunk_semantic_unit(
    unit: SemanticUnit,
    tokenizer: Tokenizer,
    *,
    chunk_size: int = 800,
    overlap: int = 100,
) -> tuple[list[ChunkDraft], list[ExcludedRecord]]:
    """切分一个语义单元；不可拆原子超限时排除而不截断。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap 必须满足 0 <= overlap < chunk_size")

    prefix = embedding_prefix(unit)
    if tokenizer.count(prefix) >= chunk_size:
        return [], [
            ExcludedRecord(
                book_id=unit.book_id,
                reason="embedding_prefix_exceeds_chunk_size",
                text=prefix.strip(),
                source_refs=unit.source_refs,
                chapter_path=unit.chapter_path,
            )
        ]

    queue = [
        _Piece(
            segment.text,
            segment.source_refs,
            segment.splittable,
            segment.element_type,
        )
        for segment in unit.segments
        if segment.text.strip()
    ]
    drafts: list[ChunkDraft] = []
    excluded: list[ExcludedRecord] = []
    current: list[_Piece] = []
    part_index = 0

    def flush() -> None:
        nonlocal current, part_index
        if not current or all(piece.overlap for piece in current):
            current = []
            return
        content_text = _join_text(current)
        embedding_text = prefix + content_text
        drafts.append(
            ChunkDraft(
                book_id=unit.book_id,
                book_name=unit.book_name,
                grade_level=unit.grade_level,
                section=unit.section,
                chapter_path=unit.chapter_path,
                content_role=unit.content_role,
                retrieval_weight=unit.retrieval_weight,
                content_text=content_text,
                embedding_text=embedding_text,
                source_refs=_unique_refs(current),
                token_count=tokenizer.count(embedding_text),
                tokenizer_id=tokenizer.tokenizer_id,
                unit_sequence=unit.sequence,
                part_index=part_index,
            )
        )
        part_index += 1
        overlap_value = _overlap_piece(current, tokenizer, overlap)
        current = [overlap_value] if overlap_value is not None else []

    while queue:
        piece = queue.pop(0)
        candidate = prefix + _join_text([*current, piece])
        if tokenizer.count(candidate) <= chunk_size:
            current.append(piece)
            continue

        if not piece.splittable:
            if current:
                flush()
                # overlap 不能阻止原子段进入新 chunk，必要时直接丢弃 overlap。
                if current and tokenizer.count(prefix + _join_text([*current, piece])) > chunk_size:
                    current = []
                queue.insert(0, piece)
                continue
            excluded.append(
                ExcludedRecord(
                    book_id=unit.book_id,
                    reason="atomic_segment_exceeds_chunk_size",
                    text=piece.text,
                    source_refs=piece.source_refs,
                    element_type=piece.element_type,
                    chapter_path=unit.chapter_path,
                    details={"token_count": tokenizer.count(prefix + piece.text)},
                )
            )
            continue

        head, tail = _largest_fitting_prefix(
            piece=piece,
            current=current,
            prefix=prefix,
            tokenizer=tokenizer,
            chunk_size=chunk_size,
        )
        if head is None:
            if current:
                # 当前若只包含 overlap，缩减为 0；否则先发布已有正文。
                if all(item.overlap for item in current):
                    current = []
                else:
                    flush()
                queue.insert(0, piece)
                continue
            excluded.append(
                ExcludedRecord(
                    book_id=unit.book_id,
                    reason="text_token_cannot_fit",
                    text=piece.text,
                    source_refs=piece.source_refs,
                    element_type=piece.element_type,
                    chapter_path=unit.chapter_path,
                )
            )
            continue
        current.append(head)
        if tail is not None:
            queue.insert(0, tail)
        flush()

    flush()
    return drafts, excluded
