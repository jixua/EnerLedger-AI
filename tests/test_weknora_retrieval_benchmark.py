from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.rag.evaluation.retrieval_benchmark import (
    Benchmark,
    load_benchmark,
    score_ranked_passages,
)
from scripts.weknora_retrieval_benchmark import _document_map, passage_filename


def _benchmark() -> Benchmark:
    return Benchmark(
        queries={"q": "question"},
        corpus={"a": "A", "b": "B", "c": "C"},
        qrels={"q": frozenset({"a", "c"})},
        checksums={},
    )


def test_ranked_metrics_use_original_passage_ids_and_fixed_denominators() -> None:
    result = score_ranked_passages(_benchmark(), {"q": ["b", "a", "c"]}, k=3)
    metric = result["metrics"]
    assert metric["precision"] == pytest.approx(2 / 3)
    assert metric["recall"] == 1
    assert metric["ndcg"] == pytest.approx(
        (1 / math.log2(3) + 1 / math.log2(4)) / (1 + 1 / math.log2(3))
    )
    assert metric["mrr"] == 0.5
    assert metric["map"] == pytest.approx((1 / 2 + 2 / 3) / 2)


def test_empty_and_truncated_results_count_misses() -> None:
    benchmark = _benchmark()
    empty = score_ranked_passages(benchmark, {"q": []}, k=2)
    assert all(value == 0 for value in empty["metrics"].values())
    truncated = score_ranked_passages(benchmark, {"q": ["b", "a", "c"]}, k=2)
    assert truncated["metrics"]["recall"] == 0.5
    assert truncated["metrics"]["map"] == 0.25
    assert truncated["weknora_formula_variants"]["map"] == 0.5
    short = score_ranked_passages(benchmark, {"q": ["a"]}, k=2)
    assert short["metrics"]["ndcg"] < 1
    assert short["weknora_formula_variants"]["ndcg"] == 1


def test_unknown_or_duplicate_passage_ids_fail_closed() -> None:
    with pytest.raises(ValueError, match="测试集外"):
        score_ranked_passages(_benchmark(), {"q": ["other"]}, k=2)
    with pytest.raises(ValueError, match="重复"):
        score_ranked_passages(_benchmark(), {"q": ["a", "a"]}, k=2)


def test_document_mapping_requires_exclusive_ready_corpus() -> None:
    benchmark = _benchmark()
    docs = [
        {"id": i, "filename": passage_filename(pid), "status": "READY"}
        for i, pid in enumerate(benchmark.corpus, start=1)
    ]
    assert _document_map(benchmark, docs, require_ready=True) == {1: "a", 2: "b", 3: "c"}
    with pytest.raises(ValueError, match="一一对应"):
        _document_map(benchmark, docs[:-1], require_ready=True)
    docs[0]["status"] = "QUEUED"
    with pytest.raises(ValueError, match="未就绪"):
        _document_map(benchmark, docs, require_ready=True)


def test_official_sample_is_identified_as_smoke_data() -> None:
    samples = Path(__file__).resolve().parents[1] / "benchmarks/weknora/samples"
    if not samples.is_dir():
        pytest.skip("本地未下载可选的 WeKnora 官方样例")
    pytest.importorskip("pyarrow")
    benchmark = load_benchmark(samples)
    assert len(benchmark.queries) == 1
    assert len(benchmark.corpus) == 4
    assert benchmark.has_negative_passages is False
    assert (
        score_ranked_passages(benchmark, {"1": ["1", "2", "3", "4"]}, k=10)[
            "suitable_for_comparison"
        ]
        is False
    )
