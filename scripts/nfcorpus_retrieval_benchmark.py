#!/usr/bin/env python3
"""Download BEIR NFCorpus and evaluate this project's document retrieval."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.evaluation.beir_nfcorpus import load_nfcorpus, score_nfcorpus  # noqa: E402
from app.rag.evaluation.retrieval_benchmark import Benchmark  # noqa: E402

DATASET_DIR = ROOT / "benchmarks" / "beir" / "nfcorpus" / "data"
DOWNLOAD_URL = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip"
UPSTREAM_MD5 = "a89dba18a62ef92f7d323ec890a0d38d"
REQUIRED_FILES = ("corpus.jsonl", "queries.jsonl", "qrels/test.tsv")


def fetch(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "nfcorpus.zip"
    if not archive.is_file() or hashlib.md5(archive.read_bytes()).hexdigest() != UPSTREAM_MD5:  # noqa: S324
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".zip", delete=False) as temp:
            temporary = Path(temp.name)
        try:
            subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    "120",
                    "--output",
                    str(temporary),
                    DOWNLOAD_URL,
                ],
                check=True,
            )
            actual = hashlib.md5(temporary.read_bytes()).hexdigest()  # noqa: S324
            if actual != UPSTREAM_MD5:
                raise ValueError(f"BEIR 下载校验失败: {actual}")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    with zipfile.ZipFile(archive) as zipped:
        for name in REQUIRED_FILES:
            member = f"nfcorpus/{name}"
            payload = zipped.read(member)
            destination = directory / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.is_file() or destination.read_bytes() != payload:
                destination.write_bytes(payload)
    benchmark = load_nfcorpus(directory)
    print(
        json.dumps(
            {
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "queries": len(benchmark.queries),
                "corpus": len(benchmark.corpus),
                "qrels": sum(map(len, benchmark.qrels.values())),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def filename(doc_id: str) -> str:
    return f"nfcorpus-{hashlib.sha256(doc_id.encode()).hexdigest()[:24]}.md"


def _client(base_url: str, token_env: str) -> httpx.Client:
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise ValueError(f"请在环境变量 {token_env} 中提供当前项目的 Bearer JWT")
    return httpx.Client(
        base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=120
    )


def _docs(client: httpx.Client, dataset_id: int) -> list[dict]:
    response = client.get("/api/v1/documents", params={"dataset_id": dataset_id})
    response.raise_for_status()
    return response.json()


def _document_map(benchmark: Benchmark, docs: list[dict], *, complete: bool) -> dict[int, str]:
    expected = {filename(doc_id): doc_id for doc_id in benchmark.corpus}
    if len(expected) != len(benchmark.corpus):
        raise ValueError("文档文件名发生哈希碰撞")
    actual = {str(doc["filename"]): doc for doc in docs}
    if len(actual) != len(docs) or set(actual) - set(expected):
        raise ValueError("测试知识库混入了额外文档或有重复文件名")
    if complete and (set(actual) != set(expected) or any(doc["status"] != "READY" for doc in docs)):
        raise ValueError("测试知识库的语料尚未全部入库并处于 READY 状态")
    return {int(doc["document_id"]): expected[name] for name, doc in actual.items()}


def ingest(benchmark: Benchmark, args: argparse.Namespace) -> None:
    with _client(args.base_url, args.token_env) as client:
        docs = _docs(client, args.dataset_id)
        _document_map(benchmark, docs, complete=False)
        present = {str(doc["filename"]) for doc in docs}
        for count, (doc_id, content) in enumerate(benchmark.corpus.items(), start=1):
            name = filename(doc_id)
            if name in present:
                continue
            response = client.post(
                f"/api/v1/datasets/{args.dataset_id}/documents",
                files={"file": (name, content.encode("utf-8"), "text/markdown")},
            )
            response.raise_for_status()
            if count % 100 == 0:
                print(f"已检查/提交 {count}/{len(benchmark.corpus)} 篇文档", flush=True)
        deadline = time.monotonic() + args.wait_seconds
        while True:
            docs = _docs(client, args.dataset_id)
            _document_map(benchmark, docs, complete=False)
            counts: dict[str, int] = {}
            for doc in docs:
                status = str(doc["status"])
                counts[status] = counts.get(status, 0) + 1
            if counts.get("FAILED", 0) or counts.get("REJECTED", 0):
                raise ValueError(f"文档解析失败，请检查测试知识库: {counts}")
            if counts.get("READY") == len(benchmark.corpus):
                print(f"NFCorpus 全部 {len(benchmark.corpus)} 篇文档已就绪")
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等待文档就绪超时: {counts}")
            print(f"入库进度: {counts}", flush=True)
            time.sleep(15)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(benchmark: Benchmark, args: argparse.Namespace) -> None:
    with _client(args.base_url, args.token_env) as client:
        dataset_response = client.get(f"/api/v1/datasets/{args.dataset_id}")
        dataset_response.raise_for_status()
        dataset = dataset_response.json()
        doc_ids = _document_map(benchmark, _docs(client, args.dataset_id), complete=True)
        predictions = {}
        for count, (query_id, query) in enumerate(benchmark.queries.items(), start=1):
            response = client.post(
                "/api/v1/recall",
                json={
                    "query": query,
                    "dataset_ids": [args.dataset_id],
                    "include_content": False,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("failed_sources"):
                raise ValueError(f"问题 {query_id} 检索路故障: {payload['failed_sources']}")
            ranked = []
            for hit in payload["hits"]:
                document_id = int(hit["doc_id"])
                if document_id not in doc_ids:
                    raise ValueError(f"问题 {query_id} 返回语料外文档 {document_id}")
                source_id = doc_ids[document_id]
                if source_id not in ranked:
                    ranked.append(source_id)
            predictions[query_id] = ranked
            if count % 25 == 0:
                print(f"已检索 {count}/{len(benchmark.queries)} 个问题", flush=True)
        report = score_nfcorpus(benchmark, predictions, k=args.k)
        report["run"] = {
            "project_sha": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "dataset_id": args.dataset_id,
            "base_url": args.base_url,
            "retrieval_stage": "fused_candidates_before_rerank",
            "model_config_ids": {
                key: dataset.get(key)
                for key in ("dense_embedding_config_id", "sparse_embedding_config_id")
            },
        }
        report["predictions"] = predictions
        _write_json(args.output, report)
        print(
            json.dumps(
                {"output": str(args.output), "metrics": report["metrics"]},
                ensure_ascii=False,
                indent=2,
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATASET_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch", help="下载官方 NFCorpus 并按 BEIR 公布的 MD5 校验")
    sub.add_parser("inspect", help="检查测试划分与文件 SHA-256")
    for command in ("ingest", "run"):
        part = sub.add_parser(command)
        part.add_argument("--base-url", required=True)
        part.add_argument("--dataset-id", required=True, type=int)
        part.add_argument("--token-env", default="BENCHMARK_API_TOKEN")
        if command == "ingest":
            part.add_argument("--wait-seconds", type=int, default=7200)
        else:
            part.add_argument("--k", type=int, default=10)
            part.add_argument("--output", required=True, type=Path)
    score = sub.add_parser("score", help="用相同口径评估另一个系统导出的文档 ID 排名")
    score.add_argument("--predictions", type=Path, required=True)
    score.add_argument("--k", type=int, default=10)
    score.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            fetch(args.data_dir)
            return 0
        benchmark = load_nfcorpus(args.data_dir)
        if args.command == "inspect":
            print(
                json.dumps(
                    {
                        "queries": len(benchmark.queries),
                        "corpus": len(benchmark.corpus),
                        "qrels": sum(map(len, benchmark.qrels.values())),
                        "sha256": benchmark.checksums,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "ingest":
            ingest(benchmark, args)
        elif args.command == "run":
            run(benchmark, args)
        else:
            predictions = json.loads(args.predictions.read_text(encoding="utf-8"))
            report = score_nfcorpus(benchmark, predictions, k=args.k)
            _write_json(args.output, report)
            print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    except (
        ValueError,
        RuntimeError,
        OSError,
        httpx.HTTPError,
        TimeoutError,
        zipfile.BadZipFile,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"评测失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
