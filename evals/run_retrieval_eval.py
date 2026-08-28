"""教材原始资料驱动的真实检索评测运行器。

用法：
    env RAG_DATABASE_URL='postgresql+psycopg://rag:rag@127.0.0.1:55433/rag' \
        .venv/bin/python evals/run_retrieval_eval.py

运行器会读取 70 条主证据评测集，调用真实 Embedding API 和 RAG hybrid_search，
并在 ``evals/`` 下写入脱敏报告。支持 hybrid 与 vector-only 两种模式；
报告不保存 API key、数据库 URL 或查询向量。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.rag.evaluation import (  # noqa: E402
    acceptable_metrics,
    acceptable_rank,
    primary_metrics,
    primary_rank,
)
from backend.rag.retrieval import RetrievalService  # noqa: E402
from backend.rag.schemas import SearchHit  # noqa: E402
from backend.rag.settings import RAGSettings  # noqa: E402
from evals.rerank import (  # noqa: E402
    RERANK_DOCUMENT_STRATEGY,
    RERANK_QUERY_STRATEGY,
    RerankClient,
    RerankSettings,
)
from scripts.embedding_generation.client import OpenAIEmbeddingClient  # noqa: E402
from scripts.embedding_generation.settings import EmbeddingSettings  # noqa: E402

DEFAULT_CASES_PATH = ROOT / "evals" / "retrieval_cases_v1.jsonl"
DEFAULT_ARTIFACT_PATH = ROOT / "embedding_artifacts" / "v1" / "chunks.jsonl"
EXPECTED_CASE_COUNT = 70
DEFAULT_CUTOFF = 20
VECTOR_RERANK_CANDIDATES = 50
RETRIEVAL_MODES = ("hybrid", "vector", "vector-rerank")


class EvaluationDataError(ValueError):
    """评测集或原始教材 artifact 的可审计性校验失败。"""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取非空 JSONL 行，并在错误中保留精确文件与行号。"""
    if not path.is_file():
        raise EvaluationDataError(f"文件不存在：{path}")
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise EvaluationDataError(f"{path}:{line_number} 不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise EvaluationDataError(f"{path}:{line_number} 必须是 JSON 对象")
        records.append(payload)
    return records


def _artifact_anchor(record: dict[str, Any]) -> dict[str, Any]:
    """提取会影响教材来源可追溯性的稳定锚点字段。"""
    source_refs = record.get("source_refs")
    if not isinstance(source_refs, list) or not source_refs:
        raise EvaluationDataError(f"artifact chunk {record.get('chunk_id')} 缺少 source_refs")
    first_ref = source_refs[0]
    if not isinstance(first_ref, dict) or not first_ref.get("source_pdf"):
        raise EvaluationDataError(f"artifact chunk {record.get('chunk_id')} 缺少 source_pdf")
    return {
        "book_id": record["book_id"],
        "book_name": record["book_name"],
        "source_pdf": first_ref["source_pdf"],
        "page_start": record["source_page_start"],
        "page_end": record["source_page_end"],
        "chapter_path": record["chapter_path"],
        "content_role": record["content_role"],
        "chunk_index": record["chunk_index"],
        "source_hash": record["source_hash"],
        "content_hash": record["content_hash"],
    }


def _validate_cases(
    cases: Sequence[dict[str, Any]],
    artifacts_by_id: dict[str, dict[str, Any]],
) -> None:
    """校验 case 数量、主证据、多正例及其与教材 artifact 的一致性。"""
    if len(cases) != EXPECTED_CASE_COUNT:
        raise EvaluationDataError(
            f"评测集必须恰好包含 {EXPECTED_CASE_COUNT} 条 case，实际为 {len(cases)} 条"
        )

    case_ids: set[str] = set()
    primary_ids: set[str] = set()
    for index, case in enumerate(cases, start=1):
        case_id = case.get("case_id")
        query = case.get("query")
        primary_id = case.get("primary_chunk_id")
        acceptable_ids = case.get("acceptable_chunk_ids")
        source_anchor = case.get("source_anchor")
        if not isinstance(case_id, str) or not case_id:
            raise EvaluationDataError(f"第 {index} 条 case 缺少 case_id")
        if case_id in case_ids:
            raise EvaluationDataError(f"case_id 重复：{case_id}")
        case_ids.add(case_id)
        if not isinstance(query, str) or not query.strip():
            raise EvaluationDataError(f"{case_id} 缺少 query")
        if not isinstance(primary_id, str) or not primary_id:
            raise EvaluationDataError(f"{case_id} 缺少 primary_chunk_id")
        if primary_id in primary_ids:
            raise EvaluationDataError(f"primary_chunk_id 重复：{primary_id}")
        primary_ids.add(primary_id)
        if (
            not isinstance(acceptable_ids, list)
            or not acceptable_ids
            or not all(isinstance(item, str) and item for item in acceptable_ids)
        ):
            raise EvaluationDataError(f"{case_id} 的 acceptable_chunk_ids 必须为非空字符串列表")
        if len(set(acceptable_ids)) != len(acceptable_ids):
            raise EvaluationDataError(f"{case_id} 的 acceptable_chunk_ids 不能重复")
        if primary_id not in acceptable_ids:
            raise EvaluationDataError(
                f"{case_id} 的 acceptable_chunk_ids 必须包含 primary_chunk_id"
            )
        missing_acceptable_ids = [
            chunk_id for chunk_id in acceptable_ids if chunk_id not in artifacts_by_id
        ]
        if missing_acceptable_ids:
            raise EvaluationDataError(
                f"{case_id} 的 acceptable chunk 不存在于 artifact：{missing_acceptable_ids}"
            )
        acceptable_reason = case.get("acceptable_reason")
        if len(acceptable_ids) > 1 and (
            not isinstance(acceptable_reason, str) or not acceptable_reason.strip()
        ):
            raise EvaluationDataError(f"{case_id} 有多个正例时必须填写 acceptable_reason")
        if acceptable_reason is not None and not isinstance(acceptable_reason, str):
            raise EvaluationDataError(f"{case_id} 的 acceptable_reason 必须是字符串")

        artifact = artifacts_by_id.get(primary_id)
        if artifact is None:
            raise EvaluationDataError(
                f"{case_id} 的 primary_chunk_id 不存在于 artifact：{primary_id}"
            )
        if not isinstance(source_anchor, dict):
            raise EvaluationDataError(f"{case_id} 缺少 source_anchor")
        expected_anchor = _artifact_anchor(artifact)
        if source_anchor != expected_anchor:
            raise EvaluationDataError(
                f"{case_id} 的 source_anchor 与 artifact 不一致："
                f"期望 {expected_anchor!r}，实际 {source_anchor!r}"
            )
        slices = case.get("slices")
        if (
            not isinstance(slices, list)
            or not slices
            or not all(isinstance(item, str) and item for item in slices)
        ):
            raise EvaluationDataError(f"{case_id} 的 slices 必须为非空字符串列表")
        if not isinstance(case.get("source_excerpt"), str) or not case["source_excerpt"].strip():
            raise EvaluationDataError(f"{case_id} 缺少 source_excerpt")


def _embed_queries(
    queries: Sequence[str], settings: EmbeddingSettings
) -> tuple[tuple[float, ...], ...]:
    """按 embedding 配置分批生成查询向量，不把向量写入报告或标准输出。"""
    client = OpenAIEmbeddingClient(settings)
    vectors: list[tuple[float, ...]] = []
    for start in range(0, len(queries), settings.batch_size):
        batch = queries[start : start + settings.batch_size]
        batch_number = start // settings.batch_size + 1
        print(f"[eval] 生成查询向量：第 {batch_number} 批，共 {len(batch)} 条")
        response = client.embed(batch)
        vectors.extend(response.vectors)
    if len(vectors) != len(queries):
        raise RuntimeError(f"Embedding 返回数量异常：期望 {len(queries)}，实际 {len(vectors)}")
    return tuple(vectors)


def _metric_summary(records: Sequence[dict[str, Any]]) -> dict[str, float]:
    """从完成检索的 case 记录中计算 Strict Primary 与多正例指标。"""
    rankings = [list(record["ranked_chunk_ids"]) for record in records]
    strict = primary_metrics(
        [str(record["primary_chunk_id"]) for record in records],
        rankings,
        cutoff=DEFAULT_CUTOFF,
    )
    acceptable = acceptable_metrics(
        [list(record["acceptable_chunk_ids"]) for record in records],
        rankings,
        cutoff=DEFAULT_CUTOFF,
    )
    return {
        **strict,
        "acceptable_at_1": acceptable["acceptable_at_1"],
        "acceptable_at_5": acceptable["acceptable_at_5"],
        "acceptable_mrr": acceptable["mrr"],
    }


def _grouped_metrics(records: Iterable[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """按教材或切片聚合 Strict Primary 与多正例指标。"""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        values = record[key]
        if isinstance(values, str):
            grouped[values].append(record)
        else:
            for value in values:
                grouped[str(value)].append(record)
    return {
        group: {"count": len(group_records), **_metric_summary(group_records)}
        for group, group_records in sorted(grouped.items())
    }


def _serialize_hit(
    rank: int,
    hit: SearchHit,
    *,
    vector_candidate_rank: int | None = None,
    vector_candidate_score: float | None = None,
) -> dict[str, Any]:
    """保存足够审计排名的命中元数据，不保存教材正文或向量。"""
    result = {
        "rank": rank,
        "chunk_id": hit.chunk_id,
        "score": hit.score,
        "book_id": hit.book_id,
        "book_name": hit.book_name,
        "chapter_path": list(hit.chapter_path),
        "content_role": hit.content_role,
        "page_start": hit.source_page_start,
        "page_end": hit.source_page_end,
        "matched_channels": list(hit.matched_channels),
    }
    if vector_candidate_rank is not None:
        result["vector_candidate_rank"] = vector_candidate_rank
    if vector_candidate_score is not None:
        result["vector_candidate_score"] = vector_candidate_score
    return result


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    """保存最终命中与其候选池，供评测报告审计 Rerank 前后顺序。"""

    hits: tuple[SearchHit, ...]
    candidates: tuple[SearchHit, ...]


def _retrieve_hits(
    service: RetrievalService,
    *,
    query: str,
    vector: Sequence[float],
    mode: str,
    rerank_client: RerankClient | None = None,
) -> RetrievalOutcome:
    """按指定模式调用真实检索通道，并在需要时执行 vector candidate Rerank。"""
    if mode == "hybrid":
        hits = service.hybrid_search(query_text=query, query_vector=vector, limit=DEFAULT_CUTOFF)
        return RetrievalOutcome(hits=hits, candidates=hits)
    if mode == "vector":
        hits = service.hnsw_vector_search(vector, limit=DEFAULT_CUTOFF)
        return RetrievalOutcome(hits=hits, candidates=hits)
    if mode == "vector-rerank":
        if rerank_client is None:
            raise ValueError("vector-rerank 模式需要 RerankClient")
        candidates = service.hnsw_vector_search(vector, limit=VECTOR_RERANK_CANDIDATES)
        rerank_results = rerank_client.rerank(
            query=query,
            documents=[hit.content_text for hit in candidates],
            top_n=DEFAULT_CUTOFF,
        )
        hits = tuple(
            replace(
                candidates[item.index],
                score=item.relevance_score,
                matched_channels=("vector", "rerank"),
            )
            for item in rerank_results
        )
        return RetrievalOutcome(hits=hits, candidates=candidates)
    raise ValueError(f"不支持的检索模式：{mode}，可选值为 {', '.join(RETRIEVAL_MODES)}")


def run_evaluation(
    *,
    cases_path: Path = DEFAULT_CASES_PATH,
    artifact_path: Path = DEFAULT_ARTIFACT_PATH,
    output_path: Path | None = None,
    mode: str = "hybrid",
) -> Path:
    """执行一次真实 embedding + 指定模式检索评测，并返回脱敏报告路径。"""
    if mode not in RETRIEVAL_MODES:
        raise ValueError(f"不支持的检索模式：{mode}，可选值为 {', '.join(RETRIEVAL_MODES)}")
    cases = _read_jsonl(cases_path)
    artifacts = _read_jsonl(artifact_path)
    artifacts_by_id = {str(record.get("chunk_id")): record for record in artifacts}
    if len(artifacts_by_id) != len(artifacts):
        raise EvaluationDataError("artifact 中存在重复 chunk_id")
    _validate_cases(cases, artifacts_by_id)

    embedding_settings = EmbeddingSettings.from_sources(env_file=ROOT / ".env")
    if embedding_settings.dimensions != 1024:
        raise EvaluationDataError("评测查询向量维度必须为 1024")
    query_vectors = _embed_queries([str(case["query"]) for case in cases], embedding_settings)

    retrieval_settings = RAGSettings()
    if retrieval_settings.embedding_dimensions != embedding_settings.dimensions:
        raise EvaluationDataError(
            "Embedding 与 RAG 数据库的向量维度不一致："
            f"{embedding_settings.dimensions} != {retrieval_settings.embedding_dimensions}"
        )

    rerank_settings = (
        RerankSettings.from_sources(env_file=ROOT / ".env") if mode == "vector-rerank" else None
    )
    rerank_client = RerankClient(rerank_settings) if rerank_settings else None
    service = RetrievalService(settings=retrieval_settings)
    records: list[dict[str, Any]] = []
    try:
        for index, (case, vector) in enumerate(zip(cases, query_vectors, strict=True), start=1):
            outcome = _retrieve_hits(
                service,
                query=str(case["query"]),
                vector=vector,
                mode=mode,
                rerank_client=rerank_client,
            )
            hits = outcome.hits
            ranked_chunk_ids = [hit.chunk_id for hit in hits]
            rank = primary_rank(str(case["primary_chunk_id"]), ranked_chunk_ids)
            acceptable_ids = list(case["acceptable_chunk_ids"])
            acceptable_hit_rank = acceptable_rank(acceptable_ids, ranked_chunk_ids)
            vector_candidate_positions = {
                hit.chunk_id: (candidate_rank, hit.score)
                for candidate_rank, hit in enumerate(outcome.candidates, start=1)
            }
            records.append(
                {
                    "case_id": case["case_id"],
                    "topic": case.get("topic", ""),
                    "query": case["query"],
                    "primary_chunk_id": case["primary_chunk_id"],
                    "primary_rank": rank,
                    "acceptable_chunk_ids": acceptable_ids,
                    "acceptable_rank": acceptable_hit_rank,
                    "book_id": case["source_anchor"]["book_id"],
                    "slices": case["slices"],
                    "candidate_chunk_ids": [hit.chunk_id for hit in outcome.candidates],
                    "ranked_chunk_ids": ranked_chunk_ids,
                    "top_hits": [
                        _serialize_hit(
                            hit_rank,
                            hit,
                            vector_candidate_rank=(
                                vector_candidate_positions[hit.chunk_id][0]
                                if mode == "vector-rerank"
                                else None
                            ),
                            vector_candidate_score=(
                                vector_candidate_positions[hit.chunk_id][1]
                                if mode == "vector-rerank"
                                else None
                            ),
                        )
                        for hit_rank, hit in enumerate(hits, start=1)
                    ],
                }
            )
            rank_text = str(rank) if rank is not None else f">{DEFAULT_CUTOFF}"
            acceptable_rank_text = (
                str(acceptable_hit_rank)
                if acceptable_hit_rank is not None
                else f">{DEFAULT_CUTOFF}"
            )
            print(
                f"[eval] 检索 {index:02}/{len(cases)} {case['case_id']}: "
                f"primary rank={rank_text}, acceptable rank={acceptable_rank_text}"
            )
    finally:
        service.close()
        if rerank_client is not None:
            rerank_client.close()

    metrics = _metric_summary(records)
    report = {
        "run_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "path": str(cases_path.relative_to(ROOT)),
            "case_count": len(cases),
            "artifact_path": str(artifact_path.relative_to(ROOT)),
        },
        "embedding": {
            "model": embedding_settings.model,
            "dimensions": embedding_settings.dimensions,
        },
        "retrieval": {
            "mode": mode,
            "method": (
                "hybrid_search"
                if mode == "hybrid"
                else "hnsw_vector_search"
                if mode == "vector"
                else "hnsw_vector_search_then_rerank"
            ),
            "cutoff": DEFAULT_CUTOFF,
            **(
                {
                    "vector_candidates": 50,
                    "fts_candidates": 50,
                    "formula_candidates": 20,
                    "rrf_k": retrieval_settings.rrf_k,
                }
                if mode == "hybrid"
                else {
                    "vector_candidates": (
                        VECTOR_RERANK_CANDIDATES if mode == "vector-rerank" else DEFAULT_CUTOFF
                    ),
                    **(
                        {
                            "rerank_model": rerank_settings.model,
                            "rerank_top_n": DEFAULT_CUTOFF,
                            "rerank_instruct": rerank_settings.instruct,
                            "rerank_query_strategy": RERANK_QUERY_STRATEGY,
                            "rerank_document_strategy": RERANK_DOCUMENT_STRATEGY,
                        }
                        if rerank_settings is not None
                        else {}
                    ),
                }
            ),
        },
        "summary": {
            "Primary@1": metrics["primary_at_1"],
            "Primary@5": metrics["primary_at_5"],
            "MRR": metrics["mrr"],
            "Acceptable@1": metrics["acceptable_at_1"],
            "Acceptable@5": metrics["acceptable_at_5"],
            "Acceptable MRR": metrics["acceptable_mrr"],
        },
        "by_book": _grouped_metrics(records, "book_id"),
        "by_slice": _grouped_metrics(records, "slices"),
        "cases": records,
    }
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    default_report_prefix = {
        "hybrid": "retrieval_eval_report",
        "vector": "retrieval_eval_vector_report",
        "vector-rerank": "retrieval_eval_vector_rerank_report",
    }[mode]
    final_output_path = output_path or ROOT / "evals" / f"{default_report_prefix}_{timestamp}.json"
    final_output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[eval] 真实检索完成")
    print(f"[eval] Primary@1: {metrics['primary_at_1']:.4f}")
    print(f"[eval] Primary@5: {metrics['primary_at_5']:.4f}")
    print(f"[eval] MRR: {metrics['mrr']:.4f}")
    print(f"[eval] Acceptable@1: {metrics['acceptable_at_1']:.4f}")
    print(f"[eval] Acceptable@5: {metrics['acceptable_at_5']:.4f}")
    print(f"[eval] Acceptable MRR: {metrics['acceptable_mrr']:.4f}")
    print(f"[eval] 报告：{final_output_path}")
    return final_output_path


def _parse_args() -> argparse.Namespace:
    """解析运行器参数，默认始终指向版本化 70-case 数据集。"""
    parser = argparse.ArgumentParser(description="执行教材原始资料驱动的真实检索评测")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH, help="评测集 JSONL 路径")
    parser.add_argument(
        "--artifact", type=Path, default=DEFAULT_ARTIFACT_PATH, help="教材 chunk artifact"
    )
    parser.add_argument("--output", type=Path, help="脱敏报告输出路径")
    parser.add_argument(
        "--mode",
        choices=RETRIEVAL_MODES,
        default="hybrid",
        help="检索模式：hybrid、vector，或 vector-rerank（向量候选接 Rerank）",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        arguments = _parse_args()
        run_evaluation(
            cases_path=arguments.cases.resolve(),
            artifact_path=arguments.artifact.resolve(),
            output_path=arguments.output.resolve() if arguments.output else None,
            mode=arguments.mode,
        )
    except Exception as exc:
        print(f"[eval] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
