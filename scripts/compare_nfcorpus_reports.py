#!/usr/bin/env python3
"""Compare two NFCorpus @K reports using paired query bootstrap intervals."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

METRICS = ("precision", "recall", "ndcg", "mrr", "map")


def compare(left: dict, right: dict, *, samples: int, seed: int) -> dict:
    for key in ("benchmark", "split", "k", "query_count", "corpus_count", "dataset_sha256"):
        if left.get(key) != right.get(key):
            raise ValueError(f"两份报告的 {key} 不一致")
    if set(left["per_query"]) != set(right["per_query"]):
        raise ValueError("两份报告的问题 ID 不一致")
    if samples < 100:
        raise ValueError("bootstrap 次数须至少 100")
    query_ids = sorted(left["per_query"])
    count = len(query_ids)
    rng = random.Random(seed)
    rows = {}
    for metric in METRICS:
        differences = [
            left["per_query"][query_id][metric] - right["per_query"][query_id][metric]
            for query_id in query_ids
        ]
        bootstrapped = sorted(
            sum(differences[rng.randrange(count)] for _ in range(count)) / count
            for _ in range(samples)
        )
        lower = bootstrapped[int(samples * 0.025)]
        upper = bootstrapped[int(samples * 0.975) - 1]
        wins = sum(value > 1e-12 for value in differences)
        ties = sum(abs(value) <= 1e-12 for value in differences)
        rows[metric] = {
            "left": left["metrics"][metric],
            "right": right["metrics"][metric],
            "left_minus_right": sum(differences) / count,
            "paired_bootstrap_95pct": [lower, upper],
            "per_query_left_win": wins,
            "per_query_tie": ties,
            "per_query_right_win": count - wins - ties,
        }
    return {
        "benchmark": left["benchmark"],
        "split": left["split"],
        "k": left["k"],
        "query_count": count,
        "bootstrap_samples": samples,
        "bootstrap_seed": seed,
        "method": "paired query bootstrap, percentile interval, resampling with replacement",
        "metrics": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", type=Path, required=True)
    parser.add_argument("--right", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260928)
    args = parser.parse_args()
    result = compare(
        json.loads(args.left.read_text(encoding="utf-8")),
        json.loads(args.right.read_text(encoding="utf-8")),
        samples=args.samples,
        seed=args.seed,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
