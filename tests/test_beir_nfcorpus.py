from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.rag.evaluation.beir_nfcorpus import load_nfcorpus, score_nfcorpus
from app.rag.evaluation.retrieval_benchmark import Benchmark
from scripts.nfcorpus_retrieval_benchmark import _document_map, filename


def _benchmark() -> Benchmark:
    return Benchmark(
        queries={"q": "question"},
        corpus={"a": "A", "b": "B", "c": "C"},
        qrels={"q": frozenset({"a", "c"})},
        checksums={},
    )


def test_beir_metrics_use_fixed_k_and_all_relevant_documents() -> None:
    report = score_nfcorpus(_benchmark(), {"q": ["b", "a"]}, k=3)
    metrics = report["metrics"]
    assert metrics["precision"] == pytest.approx(1 / 3)
    assert metrics["recall"] == 0.5
    assert metrics["ndcg"] == pytest.approx((1 / math.log2(3)) / (1 + 1 / math.log2(3)))
    assert metrics["mrr"] == 0.5
    assert metrics["map"] == 0.25
    empty_metrics = score_nfcorpus(_benchmark(), {"q": []}, k=3)["metrics"]
    assert all(value == 0 for value in empty_metrics.values())


def test_nfcorpus_loader_uses_only_test_qrels(tmp_path: Path) -> None:
    (tmp_path / "corpus.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"_id": "a", "title": "Title", "text": "body"},
                {"_id": "b", "title": "", "text": "other"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "queries.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"_id": "train", "text": "training"},
                {"_id": "test", "text": "testing"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "qrels").mkdir()
    (tmp_path / "qrels/test.tsv").write_text(
        "query-id\tcorpus-id\tscore\ntest\ta\t1\n", encoding="utf-8"
    )
    benchmark = load_nfcorpus(tmp_path)
    assert benchmark.queries == {"test": "testing"}
    assert benchmark.corpus == {"a": "Title\n\nbody", "b": "other"}
    assert benchmark.qrels == {"test": frozenset({"a"})}


def test_document_mapping_rejects_incomplete_or_mixed_corpus() -> None:
    benchmark = _benchmark()
    docs = [
        {"id": i, "filename": filename(doc_id), "status": "READY"}
        for i, doc_id in enumerate(benchmark.corpus, start=1)
    ]
    assert _document_map(benchmark, docs, complete=True) == {1: "a", 2: "b", 3: "c"}
    with pytest.raises(ValueError, match="尚未全部"):
        _document_map(benchmark, docs[:-1], complete=True)
    with pytest.raises(ValueError, match="额外文档"):
        _document_map(
            benchmark,
            docs + [{"id": 4, "filename": "foreign.md", "status": "READY"}],
            complete=False,
        )


def test_official_nfcorpus_test_split_if_downloaded() -> None:
    directory = Path(__file__).resolve().parents[1] / "benchmarks/beir/nfcorpus/data"
    if not directory.is_dir():
        pytest.skip("本地未下载 NFCorpus")
    benchmark = load_nfcorpus(directory)
    assert len(benchmark.queries) == 323
    assert len(benchmark.corpus) == 3633
    assert sum(map(len, benchmark.qrels.values())) == 12334
