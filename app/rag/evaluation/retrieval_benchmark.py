"""Passage-level retrieval evaluation against a frozen Parquet qrels collection.

Both systems must export ranked *source passage IDs* before using this scorer.
Their internal chunk IDs are deliberately excluded from the metric boundary.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

DATA_FILES = ("queries", "corpus", "qrels", "answers", "qas")


@dataclass(frozen=True)
class Benchmark:
    queries: dict[str, str]
    corpus: dict[str, str]
    qrels: dict[str, frozenset[str]]
    checksums: dict[str, str]

    @property
    def has_negative_passages(self) -> bool:
        return any(set(self.corpus) - relevant for relevant in self.qrels.values())


def load_benchmark(directory: Path) -> Benchmark:
    """Load and validate the public WeKnora five-file sample format."""
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError("评测数据读取需要 pyarrow；请在评测环境安装 pyarrow") from exc

    rows = {}
    checksums = {}
    for name in DATA_FILES:
        path = directory / f"{name}.parquet"
        if not path.is_file():
            raise ValueError(f"缺少数据文件: {path}")
        checksums[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        rows[name] = parquet.read_table(path).to_pylist()

    queries = _text_map(rows["queries"], "queries")
    corpus = _text_map(rows["corpus"], "corpus")
    if not queries or not corpus:
        raise ValueError("queries 和 corpus 不能为空")
    qrels: dict[str, set[str]] = {qid: set() for qid in queries}
    for row in rows["qrels"]:
        qid, pid = str(row["qid"]), str(row["pid"])
        if qid not in queries or pid not in corpus:
            raise ValueError(f"qrels 引用了不存在的 qid/pid: {qid}/{pid}")
        if int(row.get("score", 1)) > 0:
            qrels[qid].add(pid)
    if any(not relevant for relevant in qrels.values()):
        raise ValueError("每个问题都必须有至少一个正相关段落")
    return Benchmark(
        queries=queries,
        corpus=corpus,
        qrels={qid: frozenset(pids) for qid, pids in qrels.items()},
        checksums=checksums,
    )


def _text_map(rows: list[dict], name: str) -> dict[str, str]:
    result = {}
    for row in rows:
        key = str(row["id"])
        if key in result:
            raise ValueError(f"{name} 包含重复 ID: {key}")
        result[key] = str(row["text"])
    return result


def score_ranked_passages(
    benchmark: Benchmark, predictions: dict[str, list[str]], *, k: int
) -> dict:
    """Macro-average binary relevance metrics over exactly the benchmark queries.

    Precision divides by the number of unique returned passages up to k, as in
    WeKnora's documented retrieval metric. AP divides by all relevant passages.
    A returned passage must belong to the frozen corpus; unknown IDs fail closed.
    """
    if k < 1:
        raise ValueError("k 必须大于 0")
    if set(predictions) != set(benchmark.queries):
        raise ValueError("预测结果的问题 ID 必须与测试集完全一致")
    per_query = {}
    for qid in benchmark.queries:
        ranked = predictions[qid]
        if len(ranked) != len(set(ranked)):
            raise ValueError(f"问题 {qid} 的结果含重复 passage ID")
        unknown = set(ranked) - set(benchmark.corpus)
        if unknown:
            raise ValueError(f"问题 {qid} 返回测试集外的 passage ID: {sorted(unknown)}")
        retrieved = ranked[:k]
        relevant = benchmark.qrels[qid]
        hits = [int(pid in relevant) for pid in retrieved]
        hit_count = sum(hits)
        dcg = sum(hit / math.log2(rank + 2) for rank, hit in enumerate(hits))
        ideal = sum(1 / math.log2(rank + 2) for rank in range(min(k, len(relevant))))
        upstream_ideal = sum(
            1 / math.log2(rank + 2) for rank in range(min(len(retrieved), len(relevant)))
        )
        running_hits = 0
        ap_sum = 0.0
        first_rank = None
        for rank, hit in enumerate(hits, start=1):
            if hit:
                running_hits += 1
                ap_sum += running_hits / rank
                if first_rank is None:
                    first_rank = rank
        per_query[qid] = {
            "precision": hit_count / len(retrieved) if retrieved else 0.0,
            "recall": hit_count / len(relevant),
            "ndcg": dcg / ideal if ideal else 0.0,
            "mrr": 1 / first_rank if first_rank else 0.0,
            "map": ap_sum / len(relevant),
            "weknora_ndcg": dcg / upstream_ideal if upstream_ideal else 0.0,
            "weknora_map": ap_sum / hit_count if hit_count else 0.0,
            "returned": len(retrieved),
            "relevant": len(relevant),
            "hits": hit_count,
        }
    names = ("precision", "recall", "ndcg", "mrr", "map")
    means = {name: sum(row[name] for row in per_query.values()) / len(per_query) for name in names}
    upstream_means = {
        "ndcg": sum(row["weknora_ndcg"] for row in per_query.values()) / len(per_query),
        "map": sum(row["weknora_map"] for row in per_query.values()) / len(per_query),
    }
    return {
        "k": k,
        "query_count": len(benchmark.queries),
        "corpus_count": len(benchmark.corpus),
        "has_negative_passages": benchmark.has_negative_passages,
        "metrics": means,
        "weknora_formula_variants": upstream_means,
        "per_query": per_query,
        "dataset_sha256": benchmark.checksums,
        "suitable_for_comparison": len(benchmark.queries) >= 30 and benchmark.has_negative_passages,
    }
