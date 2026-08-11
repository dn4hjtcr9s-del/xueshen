"""把清洗记录恢复为带标题上下文和精确溯源的数学语义单元。"""

from __future__ import annotations

from collections.abc import Iterable

from scripts.embedding_chunks.heading_tracker import HeadingTracker
from scripts.embedding_chunks.role_classifier import classify_record
from scripts.embedding_chunks.schemas import (
    CleanRecord,
    ContentRole,
    ExcludedRecord,
    SemanticSegment,
    SemanticUnit,
)
from scripts.embedding_chunks.table_formatter import linearize_table

ANCHOR_ROLES: set[ContentRole] = {
    "definition",
    "theorem",
    "example",
    "exercise",
    "answer_key",
    "appendix",
}


def _segment_for(record: CleanRecord, role: ContentRole) -> SemanticSegment:
    if role == "table":
        caption = str(record.extra.get("caption", ""))
        text = linearize_table(record.text, caption)
        return SemanticSegment(text, record.source_refs, splittable=False, element_type="table")
    if role == "figure_caption":
        caption = " ".join(str(record.extra.get("caption", "")).split())
        return SemanticSegment(
            f"图注：{caption}", record.source_refs, splittable=True, element_type="figure_caption"
        )
    if role == "formula":
        return SemanticSegment(
            f"$$\n{record.text.strip()}\n$$",
            record.source_refs,
            splittable=False,
            element_type="equation_interline",
        )
    if record.element_type == "page_footnote":
        return SemanticSegment(
            f"脚注：{record.text.strip()}",
            record.source_refs,
            splittable=True,
            element_type=record.element_type,
        )
    return SemanticSegment(
        record.text.strip(),
        record.source_refs,
        splittable=True,
        element_type=record.element_type,
    )


def build_semantic_units(
    records: Iterable[CleanRecord],
) -> tuple[list[SemanticUnit], list[ExcludedRecord]]:
    """按标题和角色边界组合记录，同时返回全部可审计排除项。"""
    tracker = HeadingTracker()
    units: list[SemanticUnit] = []
    excluded: list[ExcludedRecord] = []
    current_segments: list[SemanticSegment] = []
    current_role: ContentRole | None = None
    current_path: tuple[str, ...] = ()
    current_record: CleanRecord | None = None
    unit_sequence = 0

    def flush() -> None:
        nonlocal current_segments, current_role, current_path, current_record, unit_sequence
        if current_segments and current_role is not None and current_record is not None:
            units.append(
                SemanticUnit(
                    book_id=current_record.book_id,
                    book_name=current_record.book_name,
                    grade_level=current_record.grade_level,
                    section=current_record.section,
                    chapter_path=current_path,
                    content_role=current_role,
                    retrieval_weight=0.65 if current_role == "answer_key" else 1.0,
                    segments=tuple(current_segments),
                    sequence=unit_sequence,
                )
            )
            unit_sequence += 1
        current_segments = []
        current_role = None
        current_path = ()
        current_record = None

    def start(record: CleanRecord, path: tuple[str, ...], role: ContentRole) -> None:
        nonlocal current_role, current_path, current_record
        current_role = role
        current_path = path
        current_record = record
        current_segments.append(_segment_for(record, role))

    for record in records:
        if record.element_type == "title":
            flush()
            tracker.update(record.level or 2, record.text)
            continue

        path = tracker.path
        classification = classify_record(record, path)
        if classification.role is None:
            if classification.exclusion_reason != "image_without_caption":
                flush()
            excluded.append(
                ExcludedRecord(
                    book_id=record.book_id,
                    reason=classification.exclusion_reason or "unclassified",
                    text=record.text,
                    source_refs=record.source_refs,
                    element_type=record.element_type,
                    chapter_path=path,
                )
            )
            continue

        role = classification.role
        if current_record is not None and (
            current_record.section != record.section or current_path != path
        ):
            flush()

        if current_role is None:
            start(record, path, role)
            continue

        # 定理后的证明、例题后的解答属于前一锚点，不改变单元主角色。
        if role == "proof" and current_role in {"definition", "theorem", "proof"}:
            current_segments.append(_segment_for(record, role))
            continue
        if role == "solution" and current_role in {"example", "exercise", "solution"}:
            current_segments.append(_segment_for(record, role))
            continue

        # 公式、表格和图注优先绑定当前上下文；正文继续扩展当前锚点。
        if role in {"body", "formula", "table", "figure_caption"}:
            current_segments.append(_segment_for(record, role))
            continue

        # 连续答案或附录记录维持同一单元，其余强锚点开始新单元。
        if role == current_role and role in {"answer_key", "appendix"}:
            current_segments.append(_segment_for(record, role))
            continue
        if role in ANCHOR_ROLES or role in {"proof", "solution"}:
            flush()
            start(record, path, role)
            continue

        current_segments.append(_segment_for(record, role))

    flush()
    return units, excluded
