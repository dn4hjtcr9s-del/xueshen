"""RRF 融合测试：验证多路排名与内容权重共同生效。"""

from __future__ import annotations

from backend.rag.rrf import RankedCandidate, fuse_ranked_results


def test_rrf_combines_channels_and_applies_answer_key_weight() -> None:
    body = RankedCandidate(chunk_id="body", retrieval_weight=1.0)
    answer = RankedCandidate(chunk_id="answer", retrieval_weight=0.65)

    fused = fuse_ranked_results(
        {
            "vector": [answer, body],
            "fts": [body, answer],
        },
        rrf_k=60,
    )

    assert [item.chunk_id for item in fused] == ["body", "answer"]
    assert fused[0].score > fused[1].score
    assert fused[1].retrieval_weight == 0.65


def test_rrf_deduplicates_same_chunk_across_channels() -> None:
    shared = RankedCandidate(chunk_id="shared", retrieval_weight=1.0)

    fused = fuse_ranked_results({"vector": [shared], "fts": [shared]}, rrf_k=60)

    assert len(fused) == 1
    assert fused[0].matched_channels == ("fts", "vector")
