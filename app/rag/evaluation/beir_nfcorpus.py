"""Frozen BEIR NFCorpus test split and document-level retrieval metrics."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path

from app.rag.evaluation.retrieval_benchmark import Benchmark


def load_nfcorpus(directory: Path) -> Benchmark:
    """Read BEIR's corpus, queries and test qrels without changing their IDs."""
    paths = {
        "corpus.jsonl": directory / "corpus.jsonl",
        "queries.jsonl": directory / "queries.jsonl",
        "test.tsv": directory / "qrels" / "test.tsv",
    }
    checksums = {}
    for name, path in paths.items():
        if not path.is_file():
            raise ValueError(f"缺少 NFCorpus 文件: {path}")
        checksums[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    corpus = {}
    for row in _jsonl(paths["corpus.jsonl"]):
        doc_id = str(row["_id"])
        if doc_id in corpus:
            raise ValueError(f"重复语料 ID: {doc_id}")
        corpus[doc_id] = "\n\n".join(
            part for part in (str(row.get("title") or "").strip(), str(row["text"]).strip()) if part
        )
    all_queries = {}
    for row in _jsonl(paths["queries.jsonl"]):
        query_id = str(row["_id"])
        if query_id in all_queries:
            raise ValueError(f"重复问题 ID: {query_id}")
        all_queries[query_id] = str(row["text"])

    qrels: dict[str, set[str]] = {}
    with paths["test.tsv"].open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames != ["query-id", "corpus-id", "score"]:
            raise ValueError("NFCorpus test qrels 列名不符合 BEIR 格式")
        for row in reader:
            query_id, doc_id = row["query-id"], row["corpus-id"]
            if query_id not in all_queries or doc_id not in corpus:
                raise ValueError(f"qrels 引用了不存在的 ID: {query_id}/{doc_id}")
            if int(row["score"]) > 0:
                qrels.setdefault(query_id, set()).add(doc_id)
    if not corpus or not qrels or any(not relevant for relevant in qrels.values()):
        raise ValueError("NFCorpus corpus 或 test qrels 为空")
    return Benchmark(
        queries={query_id: all_queries[query_id] for query_id in qrels},
        corpus=corpus,
        qrels={query_id: frozenset(relevant) for query_id, relevant in qrels.items()},
        checksums=checksums,
    )


def _jsonl(path: Path):
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, start=1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"JSONL 解析失败: {path}:{number}") from exc


def score_nfcorpus(benchmark: Benchmark, predictions: dict[str, list[str]], *, k: int) -> dict:
    """BEIR-compatible binary NDCG/MAP/Recall/P and reciprocal rank at K.

    Missing ranks count as nonrelevant. AP@K divides by the complete relevant
    set, matching trec_eval map_cut; P@K always divides by K.
    """
    if k < 1:
        raise ValueError("k 必须大于 0")
    if set(predictions) != set(benchmark.queries):
        raise ValueError("预测结果的问题 ID 必须与 NFCorpus test qrels 完全一致")
    corpus_ids = set(benchmark.corpus)
    per_query = {}
    for query_id in benchmark.queries:
        ranked = predictions[query_id]
        if len(ranked) != len(set(ranked)):
            raise ValueError(f"问题 {query_id} 的结果含重复文档 ID")
        unknown = set(ranked) - corpus_ids
        if unknown:
            raise ValueError(f"问题 {query_id} 返回测试集外的文档 ID: {sorted(unknown)}")
        relevant = benchmark.qrels[query_id]
        hits = [doc_id in relevant for doc_id in ranked[:k]]
        hit_count = sum(hits)
        dcg = sum(1 / math.log2(rank + 2) for rank, hit in enumerate(hits) if hit)
        ideal = sum(1 / math.log2(rank + 2) for rank in range(min(k, len(relevant))))
        precision_sum = 0.0
        running_hits = 0
        reciprocal_rank = 0.0
        for rank, hit in enumerate(hits, start=1):
            if hit:
                running_hits += 1
                precision_sum += running_hits / rank
                if not reciprocal_rank:
                    reciprocal_rank = 1 / rank
        per_query[query_id] = {
            "precision": hit_count / k,
            "recall": hit_count / len(relevant),
            "ndcg": dcg / ideal,
            "mrr": reciprocal_rank,
            "map": precision_sum / len(relevant),
            "returned": min(k, len(ranked)),
            "relevant": len(relevant),
            "hits": hit_count,
        }
    names = ("precision", "recall", "ndcg", "mrr", "map")
    return {
        "benchmark": "BEIR/nfcorpus",
        "split": "test",
        "k": k,
        "query_count": len(benchmark.queries),
        "corpus_count": len(benchmark.corpus),
        "metrics": {
            name: sum(row[name] for row in per_query.values()) / len(per_query) for name in names
        },
        "per_query": per_query,
        "dataset_sha256": benchmark.checksums,
    }
