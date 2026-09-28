#!/usr/bin/env python3
"""Run the same BEIR NFCorpus document benchmark against WeKnora's search API."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.evaluation.beir_nfcorpus import load_nfcorpus, score_nfcorpus  # noqa: E402
from scripts.nfcorpus_retrieval_benchmark import DATASET_DIR, filename  # noqa: E402


def filenames(benchmark) -> dict[str, str]:
    """Keep duplicate source bytes while satisfying WeKnora's per-file-type dedup."""
    seen: set[str] = set()
    result: dict[str, str] = {}
    for doc_id, content in benchmark.corpus.items():
        extension = ".txt" if content in seen else ".md"
        seen.add(content)
        result[doc_id] = filename(doc_id).removesuffix(".md") + extension
    return result


def client(base_url: str, token_env: str) -> httpx.Client:
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise ValueError(f"请在 {token_env} 环境变量中提供 WeKnora JWT")
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
        trust_env=False,
    )


def knowledge(client: httpx.Client, kb_id: str) -> list[dict]:
    records: list[dict] = []
    page = 1
    while True:
        response = client.get(
            f"/api/v1/knowledge-bases/{kb_id}/knowledge",
            params={"page": page, "page_size": 1000},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success") or not isinstance(payload.get("data"), list):
            raise ValueError("WeKnora 文档列表响应异常")
        records.extend(payload["data"])
        if len(records) >= int(payload["total"]):
            return records
        page += 1


def document_map(benchmark, records: list[dict], *, complete: bool) -> dict[str, str]:
    expected = {name: doc_id for doc_id, name in filenames(benchmark).items()}
    actual = {str(record["file_name"]): record for record in records}
    if len(expected) != len(benchmark.corpus) or len(actual) != len(records):
        raise ValueError("NFCorpus 文件名冲突或 WeKnora 文档重名")
    if set(actual) - set(expected):
        raise ValueError("WeKnora 测试知识库中存在 NFCorpus 以外的文档")
    if complete and (
        set(actual) != set(expected)
        or any(record["parse_status"] != "completed" for record in records)
    ):
        raise ValueError("WeKnora 知识库尚未全部解析成功")
    return {str(record["id"]): expected[name] for name, record in actual.items()}


def ingest(benchmark, args: argparse.Namespace) -> None:
    names = filenames(benchmark)
    with client(args.base_url, args.token_env) as api:
        records = knowledge(api, args.kb_id)
        document_map(benchmark, records, complete=False)
        present = {str(record["file_name"]) for record in records}
        for count, (doc_id, content) in enumerate(benchmark.corpus.items(), start=1):
            name = names[doc_id]
            if name in present:
                continue
            response = api.post(
                f"/api/v1/knowledge-bases/{args.kb_id}/knowledge/file",
                files={
                    "file": (
                        name,
                        content.encode("utf-8"),
                        "text/plain" if name.endswith(".txt") else "text/markdown",
                    )
                },
            )
            response.raise_for_status()
            if not response.json().get("success"):
                raise ValueError(f"WeKnora 上传失败: {name}")
            if count % 100 == 0:
                print(f"已检查/提交 {count}/{len(benchmark.corpus)} 篇文档", flush=True)
        deadline = time.monotonic() + args.wait_seconds
        while True:
            records = knowledge(api, args.kb_id)
            document_map(benchmark, records, complete=False)
            counts: dict[str, int] = {}
            for record in records:
                status = str(record["parse_status"])
                counts[status] = counts.get(status, 0) + 1
            if counts.get("failed") or counts.get("cancelled"):
                raise ValueError(f"WeKnora 文档解析失败: {counts}")
            if counts.get("completed") == len(benchmark.corpus):
                print(f"WeKnora NFCorpus 全部 {len(benchmark.corpus)} 篇文档已就绪")
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等待 WeKnora 文档解析超时: {counts}")
            print(f"WeKnora 入库进度: {counts}", flush=True)
            time.sleep(15)


def run(benchmark, args: argparse.Namespace) -> None:
    with client(args.base_url, args.token_env) as api:
        mapped = document_map(benchmark, knowledge(api, args.kb_id), complete=True)
        predictions: dict[str, list[str]] = {}
        for count, (query_id, query) in enumerate(benchmark.queries.items(), start=1):
            response = api.post(
                f"/api/v1/knowledge-bases/{args.kb_id}/hybrid-search",
                json={
                    "query_text": query,
                    "match_count": args.match_count,
                    "vector_threshold": 0,
                    "keyword_threshold": 0,
                    "skip_context_enrichment": True,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("success") or not isinstance(payload.get("data"), list):
                raise ValueError(f"问题 {query_id} 的 WeKnora 检索响应异常")
            ranked: list[str] = []
            for hit in payload["data"]:
                knowledge_id = str(hit["knowledge_id"])
                if knowledge_id not in mapped:
                    raise ValueError(f"问题 {query_id} 返回语料外文档 {knowledge_id}")
                source_id = mapped[knowledge_id]
                if source_id not in ranked:
                    ranked.append(source_id)
            predictions[query_id] = ranked
            if count % 25 == 0:
                print(f"WeKnora 已检索 {count}/{len(benchmark.queries)} 个问题", flush=True)
    report = score_nfcorpus(benchmark, predictions, k=args.k)
    report["run"] = {
        "weknora_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=args.weknora_checkout, text=True
        ).strip(),
        "knowledge_base_id": args.kb_id,
        "base_url": args.base_url,
        "retrieval_stage": "hybrid_search_before_rerank",
        "match_count": args.match_count,
    }
    report["predictions"] = predictions
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"output": str(args.output), "metrics": report["metrics"]}, ensure_ascii=False, indent=2
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATASET_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("ingest", "run"):
        part = sub.add_parser(command)
        part.add_argument("--base-url", required=True)
        part.add_argument("--kb-id", required=True)
        part.add_argument("--token-env", default="WEKNORA_BENCHMARK_TOKEN")
        if command == "ingest":
            part.add_argument("--wait-seconds", type=int, default=14400)
        else:
            part.add_argument("--k", type=int, default=10)
            part.add_argument("--match-count", type=int, default=64)
            part.add_argument("--weknora-checkout", type=Path, required=True)
            part.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    benchmark = load_nfcorpus(args.data_dir)
    if args.command == "ingest":
        ingest(benchmark, args)
    else:
        run(benchmark, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
