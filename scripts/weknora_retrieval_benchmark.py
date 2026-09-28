#!/usr/bin/env python3
"""Import WeKnora passages and evaluate this project's retrieval API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.evaluation.retrieval_benchmark import (  # noqa: E402
    Benchmark,
    load_benchmark,
    score_ranked_passages,
)

DEFAULT_SAMPLES = ROOT / "benchmarks" / "weknora" / "samples"
UPSTREAM_SHA = "9114e4e4f905be71976a77c684d3f92731e6f9a6"
UPSTREAM_CHECKSUMS = {
    "answers.parquet": "ff052131209af612274b19534d7a9a4ec8a40c48aeef72b2f8ce046e7517255e",
    "corpus.parquet": "7cddf67a5ed45d4191aa1408e86353293d31aa71986226d97ee228a7607ec15e",
    "qas.parquet": "fd4b49ed769f758e14b807e4f6c4b6581b163e68b13af8b1d3d4cf1bdac88f78",
    "qrels.parquet": "72ec24350f5298b4e1ba854ddf1d9d88e421265e61ef7a711c7debaa88c324f3",
    "queries.parquet": "4e727c93692ed7676fcad97c91ac284a527f1e7e5a8bf41216c5e23facbadc56",
}


def fetch_official_sample(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, expected in UPSTREAM_CHECKSUMS.items():
        destination = directory / name
        if (
            destination.is_file()
            and hashlib.sha256(destination.read_bytes()).hexdigest() == expected
        ):
            continue
        url = f"https://raw.githubusercontent.com/Tencent/WeKnora/{UPSTREAM_SHA}/dataset/samples/{name}"
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = response.read()
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise ValueError(f"官方样例校验失败: {name}，期望 {expected}，实际 {actual}")
        destination.write_bytes(payload)
        print(f"已下载并校验 {destination}")


def passage_filename(pid: str) -> str:
    return f"weknora-passage-{hashlib.sha256(pid.encode()).hexdigest()[:24]}.md"


def _client(base_url: str, token_env: str) -> httpx.Client:
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise ValueError(f"请在环境变量 {token_env} 中提供当前项目的 Bearer JWT")
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )


def _get_docs(client: httpx.Client, dataset_id: int) -> list[dict]:
    response = client.get("/api/v1/documents", params={"dataset_id": dataset_id})
    response.raise_for_status()
    return response.json()


def _document_map(benchmark: Benchmark, docs: list[dict], *, require_ready: bool) -> dict[int, str]:
    expected = {passage_filename(pid): pid for pid in benchmark.corpus}
    if len(expected) != len(benchmark.corpus):
        raise ValueError("passage 文件名发生哈希碰撞")
    actual = {str(doc["filename"]): doc for doc in docs}
    if set(actual) != set(expected) or len(actual) != len(docs):
        raise ValueError("测试知识库的文档必须与 corpus 一一对应，且不能混入其他文档")
    if require_ready:
        unready = [name for name, doc in actual.items() if doc["status"] != "READY"]
        if unready:
            raise ValueError(f"仍有文档未就绪: {unready}")
    return {int(actual[name]["id"]): pid for name, pid in expected.items()}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _metadata(benchmark: Benchmark, args: argparse.Namespace) -> dict:
    return {
        "dataset_sha256": benchmark.checksums,
        "project_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "dataset_id": args.dataset_id,
        "base_url": args.base_url,
        "retrieval_stage": "fused_candidates_before_rerank",
    }


def _ingest(benchmark: Benchmark, args: argparse.Namespace) -> None:
    with _client(args.base_url, args.token_env) as client:
        docs = _get_docs(client, args.dataset_id)
        if docs:
            raise ValueError("导入要求空的专用测试知识库；已有文档时请直接运行 run")
        for pid, content in benchmark.corpus.items():
            filename = passage_filename(pid)
            response = client.post(
                f"/api/v1/datasets/{args.dataset_id}/documents",
                files={"file": (filename, content.encode("utf-8"), "text/markdown")},
            )
            response.raise_for_status()
            print(f"已提交 {filename}")
        deadline = time.monotonic() + args.wait_seconds
        while True:
            docs = _get_docs(client, args.dataset_id)
            _document_map(benchmark, docs, require_ready=False)
            states = {str(doc["filename"]): str(doc["status"]) for doc in docs}
            failed = {
                name: state for name, state in states.items() if state in {"FAILED", "REJECTED"}
            }
            if failed:
                raise ValueError(f"文档解析失败: {failed}")
            if all(state == "READY" for state in states.values()):
                print(f"{len(docs)} 个段落已就绪，知识库 ID={args.dataset_id}")
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等待文档就绪超时，当前状态: {states}")
            time.sleep(5)


def _run(benchmark: Benchmark, args: argparse.Namespace) -> None:
    with _client(args.base_url, args.token_env) as client:
        dataset_response = client.get(f"/api/v1/datasets/{args.dataset_id}")
        dataset_response.raise_for_status()
        dataset = dataset_response.json()
        doc_to_pid = _document_map(
            benchmark, _get_docs(client, args.dataset_id), require_ready=True
        )
        predictions = {}
        query_details = {}
        for qid, query in benchmark.queries.items():
            response = client.post(
                "/api/v1/recall",
                json={"query": query, "dataset_ids": [args.dataset_id], "include_content": False},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("failed_sources"):
                raise ValueError(f"问题 {qid} 检索路故障: {payload['failed_sources']}")
            ranked = []
            for hit in payload["hits"]:
                doc_id = int(hit["doc_id"])
                if doc_id not in doc_to_pid:
                    raise ValueError(f"问题 {qid} 检索到了测试语料外的文档 {doc_id}")
                pid = doc_to_pid[doc_id]
                if pid not in ranked:
                    ranked.append(pid)
            predictions[qid] = ranked
            query_details[qid] = {
                "request_id": payload.get("request_id"),
                "elapsed_ms": payload.get("elapsed_ms"),
                "raw_chunk_count": len(payload["hits"]),
                "ranked_passage_ids": ranked,
            }
        report = score_ranked_passages(benchmark, predictions, k=args.k)
        report["run"] = _metadata(benchmark, args)
        report["run"]["model_config_ids"] = {
            key: dataset.get(key)
            for key in ("dense_embedding_config_id", "sparse_embedding_config_id")
        }
        report["queries"] = query_details
        _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "metrics": report["metrics"],
                    "suitable_for_comparison": report["suitable_for_comparison"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch", help="按固定上游提交下载并校验五个官方样例文件")
    sub.add_parser("inspect", help="校验并统计官方样例")
    for command in ("ingest", "run"):
        part = sub.add_parser(command)
        part.add_argument("--base-url", required=True)
        part.add_argument("--dataset-id", required=True, type=int)
        part.add_argument("--token-env", default="BENCHMARK_API_TOKEN")
        if command == "ingest":
            part.add_argument("--wait-seconds", type=int, default=900)
        else:
            part.add_argument("--k", type=int, default=10)
            part.add_argument("--output", required=True, type=Path)
    score = sub.add_parser("score", help="用同一计分器评估外部系统导出的 passage ID 排名")
    score.add_argument("--predictions", type=Path, required=True)
    score.add_argument("--k", type=int, default=10)
    score.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            fetch_official_sample(args.samples)
            return 0
        benchmark = load_benchmark(args.samples)
        if args.command == "inspect":
            print(
                json.dumps(
                    {
                        "queries": len(benchmark.queries),
                        "corpus": len(benchmark.corpus),
                        "qrels": sum(map(len, benchmark.qrels.values())),
                        "has_negative_passages": benchmark.has_negative_passages,
                        "sha256": benchmark.checksums,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "ingest":
            _ingest(benchmark, args)
        elif args.command == "run":
            _run(benchmark, args)
        else:
            predictions = json.loads(args.predictions.read_text(encoding="utf-8"))
            report = score_ranked_passages(benchmark, predictions, k=args.k)
            _write_json(args.output, report)
            print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError, httpx.HTTPError, TimeoutError) as exc:
        print(f"评测失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
