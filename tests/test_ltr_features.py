from __future__ import annotations

import math

import pytest

from app.rag.core.pipeline.ltr.features import (
    FEATURE_NAMES,
    _ngrams,
    _same_doc_max_bigram_similarities,
    build_online_features,
)
from app.rag.core.pipeline.recall.models import RetrieverHit


def _naive_max_similarity(
    chunk_id: str,
    peers: list[str],
    bigrams: dict[str, set[str]],
) -> float:
    values = []
    for peer_id in peers:
        if peer_id == chunk_id:
            continue
        union = bigrams[chunk_id].union(bigrams[peer_id])
        values.append(
            len(bigrams[chunk_id].intersection(bigrams[peer_id])) / len(union)
            if union
            else 0.0
        )
    return max(values, default=0.0)


def test_same_doc_similarity_optimization_matches_naive_feature() -> None:
    contents = {
        "chunk-1": "产品碳足迹核算边界和活动数据",
        "chunk-2": "产品碳足迹核算边界与排放因子",
        "chunk-3": "审核报告与质量声明",
        "chunk-4": "",
    }
    bigrams = {chunk_id: _ngrams(content, 2) for chunk_id, content in contents.items()}
    chunks_by_doc = {1: ["chunk-1", "chunk-2", "chunk-3"], 2: ["chunk-4"]}

    optimized = _same_doc_max_bigram_similarities(bigrams, chunks_by_doc)

    for peers in chunks_by_doc.values():
        for chunk_id in peers:
            assert optimized[chunk_id] == pytest.approx(
                _naive_max_similarity(chunk_id, peers, bigrams)
            )


def test_online_features_keep_same_doc_values_with_optimized_pair_scan() -> None:
    routes = {
        "dense": [
            RetrieverHit("chunk-1", 11, 7, 0.9, "dense"),
            RetrieverHit("chunk-2", 11, 7, 0.8, "dense"),
            RetrieverHit("chunk-3", 12, 7, 0.7, "dense"),
        ],
        "sparse": [],
        "bm25": [],
    }
    contents = {
        "chunk-1": "产品碳足迹核算边界",
        "chunk-2": "产品碳足迹核算数据",
        "chunk-3": "审核报告",
    }

    chunk_ids, features = build_online_features(
        query="产品碳足迹怎么做",
        routes=routes,
        candidate_contents=contents,
    )

    count_index = FEATURE_NAMES.index("same_doc_candidate_count")
    similarity_index = FEATURE_NAMES.index("same_doc_max_bigram_similarity")
    rows = {chunk_id: features[index] for index, chunk_id in enumerate(chunk_ids)}
    expected = len(_ngrams(contents["chunk-1"], 2).intersection(_ngrams(contents["chunk-2"], 2)))
    expected /= len(_ngrams(contents["chunk-1"], 2).union(_ngrams(contents["chunk-2"], 2)))

    assert rows["chunk-1"][count_index] == 1.0
    assert rows["chunk-2"][count_index] == 1.0
    assert rows["chunk-3"][count_index] == 0.0
    assert rows["chunk-1"][similarity_index] == pytest.approx(expected)
    assert rows["chunk-2"][similarity_index] == pytest.approx(expected)
    assert math.isclose(float(rows["chunk-3"][similarity_index]), 0.0)
