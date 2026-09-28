#!/usr/bin/env python3
"""Evaluate EnerLedger LambdaMART->top20->rerank against WeKnora hybrid->top20->rerank.

Run inside the isolated EnerLedger API container so the production recall pipeline,
LambdaMART bundle and the existing DashScope account are used without exporting keys.
The same rerank provider instance scores both candidate lists. Any failed retrieval,
ranking or rerank call aborts the experiment; no fallback is silently scored.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import httpx

from app.rag.application.recall_pipeline_provider import (
    aresolve_recall_execution,
    build_recall_request_from_config,
    get_recall_pipeline,
)
from app.rag.application.recall_stream_runtime import build_ltr_routes
from app.rag.core.llm.provider_lifecycle import aclose_dataset_execution_contexts
from app.rag.core.llm.providers.dashscope import DashScopeProvider
from app.rag.core.pipeline.chunk_content import fetch_chunk_contents
from app.rag.core.pipeline.ltr.candidate_routing import CANDIDATE_CONTRACT_VERSION
from app.rag.core.pipeline.ltr.ranker import load_lambda_mart_ranker
from app.rag.database import close_database
from app.rag.evaluation.beir_nfcorpus import load_nfcorpus, score_nfcorpus

RERANK_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
RERANK_MODEL = "qwen3.7-text-rerank"
TOP_N = 20


def _source_filename(source_id: str) -> str:
    return f"nfcorpus-{hashlib.sha256(source_id.encode()).hexdigest()[:24]}"


async def _document_maps(
    local_client: httpx.AsyncClient,
    remote_client: httpx.AsyncClient,
    local_base_url: str,
    weknora_base_url: str,
    dataset_id: int,
    kb_id: str,
    corpus: dict[str, str],
) -> tuple[dict[int, str], dict[str, str]]:
    local = await local_client.get(f"{local_base_url}/api/v1/documents?dataset_id={dataset_id}")
    local.raise_for_status()
    local_docs = local.json()
    expected = {_source_filename(source_id): source_id for source_id in corpus}
    if (
        len(local_docs) != len(corpus)
        or {str(doc["filename"]).removesuffix(".md") for doc in local_docs} != set(expected)
        or any(doc["status"] != "READY" for doc in local_docs)
    ):
        raise ValueError("EnerLedger 测试语料不是完整 READY 的 NFCorpus")
    local_map = {
        int(doc["document_id"]): expected[str(doc["filename"]).removesuffix(".md")]
        for doc in local_docs
    }

    remote_docs: list[dict] = []
    page = 1
    while True:
        response = await remote_client.get(
            f"{weknora_base_url}/api/v1/knowledge-bases/{kb_id}/knowledge",
            params={"page": page, "page_size": 1000},
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("success") or not isinstance(body.get("data"), list):
            raise ValueError("WeKnora 文档列表响应异常")
        remote_docs.extend(body["data"])
        if len(remote_docs) >= int(body["total"]):
            break
        page += 1
    if (
        len(remote_docs) != len(corpus)
        or {str(doc["file_name"]).removesuffix(".md").removesuffix(".txt") for doc in remote_docs}
        != set(expected)
        or any(doc["parse_status"] != "completed" for doc in remote_docs)
    ):
        raise ValueError("WeKnora 测试语料不是完整解析的 NFCorpus")
    remote_map = {
        str(doc["id"]): expected[str(doc["file_name"]).removesuffix(".md").removesuffix(".txt")]
        for doc in remote_docs
    }
    return local_map, remote_map


async def _rerank(
    provider: DashScopeProvider, query: str, hits: list[tuple[str, str]]
) -> list[str]:
    if not hits or any(not content.strip() for _, content in hits):
        raise ValueError("重排输入为空或存在无正文候选")
    result = await provider.rerank(
        query,
        [content for _, content in hits],
        model=RERANK_MODEL,
        top_n=len(hits),
    )
    indices = [item.index for item in result.results]
    if len(indices) != len(hits) or set(indices) != set(range(len(hits))):
        raise ValueError("重排模型没有返回完整且唯一的候选索引")
    if any(not 0 <= item.score <= 1 for item in result.results):
        raise ValueError("重排模型返回无效分数")
    ranked_docs: list[str] = []
    for index in indices:
        doc_id = hits[index][0]
        if doc_id not in ranked_docs:
            ranked_docs.append(doc_id)
    return ranked_docs


async def _weknora_search(
    client: httpx.AsyncClient, base_url: str, kb_id: str, query: str
) -> list[dict]:
    """Retry transient server errors; never replace them with an empty ranking."""
    for attempt in range(4):
        response = await client.post(
            f"{base_url}/api/v1/knowledge-bases/{kb_id}/hybrid-search",
            json={
                "query_text": query,
                "match_count": TOP_N,
                "vector_threshold": 0,
                "keyword_threshold": 0,
                "skip_context_enrichment": True,
            },
        )
        if response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
            await asyncio.sleep(2**attempt)
            continue
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success") or not isinstance(payload.get("data"), list):
            raise ValueError("WeKnora 混合召回响应异常")
        return payload["data"]
    raise AssertionError("unreachable")


async def main(args: argparse.Namespace) -> None:
    benchmark = load_nfcorpus(args.data_dir)
    local_token = args.local_token_file.read_text().strip()
    remote_token = args.weknora_token_file.read_text().strip()
    if not local_token or not remote_token:
        raise ValueError("测试环境 JWT 文件为空")
    local_client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {local_token}"}, timeout=120, trust_env=False
    )
    remote_client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {remote_token}"}, timeout=120, trust_env=False
    )
    recall_cfg, contexts = await aresolve_recall_execution(args.user_id, [args.dataset_id])
    ranker = load_lambda_mart_ranker(args.model_dir)
    provider = DashScopeProvider(
        api_key=contexts[args.dataset_id].dense_embedding.provider.api_key,
        api_base_url=RERANK_URL,
        model_name=RERANK_MODEL,
        timeout_ms=120000,
    )
    try:
        # Each product's document list is checked independently against the full corpus.
        local_map, remote_map = await _document_maps(
            local_client,
            remote_client,
            args.local_base_url.rstrip("/"),
            args.weknora_base_url.rstrip("/"),
            args.dataset_id,
            args.kb_id,
            benchmark.corpus,
        )
        fingerprint = {
            "dataset_checksums": benchmark.checksums,
            "dataset_id": args.dataset_id,
            "knowledge_base_id": args.kb_id,
            "rerank_model": RERANK_MODEL,
            "candidate_contract_version": CANDIDATE_CONTRACT_VERSION,
        }
        checkpoint_path = args.output_dir / "checkpoint.json"
        if checkpoint_path.is_file():
            checkpoint = json.loads(checkpoint_path.read_text())
            if checkpoint.get("fingerprint") != fingerprint:
                raise ValueError("断点文件与本次数据集或模型配置不一致")
        else:
            checkpoint = {
                "fingerprint": fingerprint,
                "project_predictions": {},
                "weknora_predictions": {},
                "diagnostics": {},
            }
        project_predictions: dict[str, list[str]] = checkpoint["project_predictions"]
        weknora_predictions: dict[str, list[str]] = checkpoint["weknora_predictions"]
        diagnostics: dict[str, dict] = checkpoint["diagnostics"]
        for count, (query_id, query) in enumerate(benchmark.queries.items(), start=1):
            if query_id in project_predictions and query_id in weknora_predictions:
                continue
            request = build_recall_request_from_config(
                query=query,
                user_id=args.user_id,
                dataset_ids=[args.dataset_id],
                recall_cfg=recall_cfg,
                dataset_contexts=contexts,
                apply_ltr_serving_contract=True,
            )
            if request.candidate_contract_version != CANDIDATE_CONTRACT_VERSION:
                raise ValueError("LambdaMART 候选契约不匹配")
            recalled = await get_recall_pipeline().execute(request)
            if recalled.failed_sources or set(recalled.route_hits) != {"bm25", "sparse", "dense"}:
                raise ValueError(f"{query_id}: 本项目三路召回未完整执行")
            candidates = recalled.candidate_hits or recalled.hits
            contents = await fetch_chunk_contents(
                [hit.chunk_id for hit in candidates], args.user_id
            )
            routes = build_ltr_routes(recalled.route_hits, candidates, contents)
            ranked = await ranker.rank(
                query=query, routes=routes, candidate_contents=contents, allow_fallback=False
            )
            if ranked.mode not in {"ltr", "ltr_short_low_confidence"}:
                raise ValueError(f"{query_id}: LambdaMART 意外降级: {ranked.mode}")
            by_chunk = {hit.chunk_id: hit for hit in candidates}
            local_top20 = [
                (local_map[by_chunk[cid].doc_id], contents[cid])
                for cid in ranked.ranked_chunk_ids[:TOP_N]
            ]
            if len(local_top20) != TOP_N:
                raise ValueError(f"{query_id}: LambdaMART 有效候选不足 20")
            project_predictions[query_id] = await _rerank(provider, query, local_top20)

            remote_hits = await _weknora_search(
                remote_client, args.weknora_base_url.rstrip("/"), args.kb_id, query
            )
            remote_top20 = [
                (remote_map[str(hit["knowledge_id"])], str(hit["content"]))
                for hit in remote_hits[:TOP_N]
            ]
            if len(remote_top20) != TOP_N:
                raise ValueError(f"{query_id}: WeKnora 有效候选不足 20")
            weknora_predictions[query_id] = await _rerank(provider, query, remote_top20)
            diagnostics[query_id] = {
                "project_fused_candidates": len(candidates),
                "project_ltr_candidates": len(ranked.ranked_chunk_ids),
                "project_ltr_mode": ranked.mode,
                "project_route_counts": recalled.per_source_counts,
                "weknora_search_candidates": len(remote_hits),
                "project_distinct_documents_in_top20": len(set(doc for doc, _ in local_top20)),
                "weknora_distinct_documents_in_top20": len(set(doc for doc, _ in remote_top20)),
            }
            args.output_dir.mkdir(parents=True, exist_ok=True)
            temporary = checkpoint_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(checkpoint, ensure_ascii=False))
            temporary.replace(checkpoint_path)
            if count % 10 == 0 or args.limit:
                print(f"已完成 {count}/{len(benchmark.queries)} 个问题", flush=True)
            if args.limit and count >= args.limit:
                break
        if args.limit:
            print(
                json.dumps(
                    {"pilot_queries": len(diagnostics), "diagnostics": diagnostics},
                    ensure_ascii=False,
                )
            )
            return
        for name, predictions in (
            ("enerledger", project_predictions),
            ("weknora", weknora_predictions),
        ):
            report = score_nfcorpus(benchmark, predictions, k=args.k)
            report["run"] = {
                "retrieval_stage": (
                    "lambdamart_top20_rerank" if name == "enerledger" else "hybrid_top20_rerank"
                ),
                "rerank_model": RERANK_MODEL,
                "rerank_endpoint": RERANK_URL,
                "rerank_input_top_n": TOP_N,
                "candidate_contract_version": (
                    CANDIDATE_CONTRACT_VERSION if name == "enerledger" else None
                ),
                "ltr_model_version": ranker.model_version if name == "enerledger" else None,
                "dataset_id": args.dataset_id if name == "enerledger" else None,
                "knowledge_base_id": args.kb_id if name == "weknora" else None,
                "diagnostics": diagnostics,
            }
            report["predictions"] = predictions
            filename = (
                f"enerledger-ltr-rerank-k{args.k}.json"
                if name == "enerledger"
                else f"weknora-hybrid-rerank-k{args.k}.json"
            )
            output = args.output_dir / filename
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(
                json.dumps(
                    {"report": str(output), "metrics": report["metrics"]},
                    ensure_ascii=False,
                )
            )
    finally:
        ranker.close()
        await provider.aclose()
        await aclose_dataset_execution_contexts(contexts.values())
        await local_client.aclose()
        await remote_client.aclose()
        await close_database()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--local-token-file", type=Path, required=True)
    parser.add_argument("--weknora-token-file", type=Path, required=True)
    parser.add_argument("--local-base-url", default="http://host.docker.internal:18000")
    parser.add_argument("--weknora-base-url", default="http://host.docker.internal:18080")
    parser.add_argument("--dataset-id", type=int, default=1)
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--kb-id", required=True)
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path("/app/models/ltr/candidate-difference-v3-20260728-final33"),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument(
        "--limit", type=int, default=0, help="只跑前 N 题做链路冒烟测试，不产出指标"
    )
    asyncio.run(main(parser.parse_args()))
