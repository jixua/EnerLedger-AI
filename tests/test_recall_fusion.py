from __future__ import annotations

import pytest

from app.rag.core.pipeline.recall.fusion import fuse_with_weighted_score
from app.rag.core.pipeline.recall.models import RetrieverHit
from app.rag.core.pipeline.recall.protocols import SOURCE_BM25, SOURCE_DENSE, SOURCE_SPARSE

DEFAULT_WEIGHTS = {
    SOURCE_BM25: 0.15,
    SOURCE_SPARSE: 0.15,
    SOURCE_DENSE: 0.70,
}
ALL_SOURCES = [SOURCE_BM25, SOURCE_SPARSE, SOURCE_DENSE]


def _hit(chunk_id: str, source: str, score: float) -> RetrieverHit:
    return RetrieverHit(
        chunk_id=chunk_id,
        doc_id=100,
        dataset_id=10,
        score=score,
        source=source,
    )


@pytest.mark.parametrize("source", [SOURCE_BM25, SOURCE_SPARSE])
def test_keyword_routes_apply_log1p_before_source_minmax(source: str) -> None:
    # log1p([0, 3, 15]) = [0, ln(4), ln(16)], so the middle value is exactly 0.5.
    # Raw-score min-max would produce 0.2 and therefore cannot satisfy this assertion.
    hits = fuse_with_weighted_score(
        per_source_hits={
            source: [
                _hit("high", source, 15.0),
                _hit("middle", source, 3.0),
                _hit("low", source, 0.0),
            ]
        },
        all_sources=ALL_SOURCES,
        weights=DEFAULT_WEIGHTS,
    )

    by_id = {hit.chunk_id: hit for hit in hits}
    assert by_id["high"].fused_score == pytest.approx(1.0)
    assert by_id["middle"].fused_score == pytest.approx(0.5)
    assert by_id["low"].fused_score == pytest.approx(0.0)


def test_three_routes_use_frozen_weights_and_missing_chunk_route_contributes_zero() -> None:
    hits = fuse_with_weighted_score(
        per_source_hits={
            SOURCE_BM25: [_hit("bm25-only", SOURCE_BM25, 12.0)],
            SOURCE_SPARSE: [_hit("sparse-only", SOURCE_SPARSE, 5.0)],
            SOURCE_DENSE: [_hit("dense-only", SOURCE_DENSE, 0.91)],
        },
        all_sources=ALL_SOURCES,
        weights=DEFAULT_WEIGHTS,
    )

    by_id = {hit.chunk_id: hit for hit in hits}
    assert by_id["bm25-only"].fused_score == pytest.approx(0.15)
    assert by_id["sparse-only"].fused_score == pytest.approx(0.15)
    assert by_id["dense-only"].fused_score == pytest.approx(0.70)
    assert by_id["bm25-only"].scores == {
        SOURCE_BM25: 12.0,
        SOURCE_SPARSE: None,
        SOURCE_DENSE: None,
    }
    assert by_id["bm25-only"].normalized_scores[SOURCE_BM25] == pytest.approx(1.0)
    assert by_id["bm25-only"].weighted_contributions == {
        SOURCE_BM25: pytest.approx(0.15),
        SOURCE_SPARSE: pytest.approx(0.0),
        SOURCE_DENSE: pytest.approx(0.0),
    }


def test_missing_route_renormalizes_weights_over_active_routes_only() -> None:
    hits = fuse_with_weighted_score(
        per_source_hits={
            SOURCE_BM25: [_hit("bm25-only", SOURCE_BM25, 8.0)],
            SOURCE_SPARSE: [],
            SOURCE_DENSE: [_hit("dense-only", SOURCE_DENSE, 0.88)],
        },
        all_sources=ALL_SOURCES,
        weights=DEFAULT_WEIGHTS,
    )

    by_id = {hit.chunk_id: hit for hit in hits}
    assert by_id["bm25-only"].fused_score == pytest.approx(0.15 / 0.85)
    assert by_id["dense-only"].fused_score == pytest.approx(0.70 / 0.85)
    assert all(hit.scores[SOURCE_SPARSE] is None for hit in hits)
    assert by_id["dense-only"].weighted_contributions[SOURCE_DENSE] == pytest.approx(
        0.70 / 0.85
    )


def test_same_chunk_is_merged_and_keeps_each_route_raw_score() -> None:
    hits = fuse_with_weighted_score(
        per_source_hits={
            SOURCE_BM25: [_hit("shared", SOURCE_BM25, 7.0)],
            SOURCE_SPARSE: [_hit("shared", SOURCE_SPARSE, 3.0)],
            SOURCE_DENSE: [_hit("shared", SOURCE_DENSE, 0.75)],
        },
        all_sources=ALL_SOURCES,
        weights=DEFAULT_WEIGHTS,
    )

    assert len(hits) == 1
    assert hits[0].fused_score == pytest.approx(1.0)
    assert hits[0].scores == {
        SOURCE_BM25: 7.0,
        SOURCE_SPARSE: 3.0,
        SOURCE_DENSE: 0.75,
    }


def test_equal_fused_scores_are_deterministically_sorted_by_chunk_id() -> None:
    hits = fuse_with_weighted_score(
        per_source_hits={
            SOURCE_DENSE: [
                _hit("chunk-b", SOURCE_DENSE, 0.8),
                _hit("chunk-a", SOURCE_DENSE, 0.8),
            ]
        },
        all_sources=ALL_SOURCES,
        weights=DEFAULT_WEIGHTS,
    )

    assert [hit.chunk_id for hit in hits] == ["chunk-a", "chunk-b"]
    assert [hit.fused_score for hit in hits] == [pytest.approx(1.0), pytest.approx(1.0)]
