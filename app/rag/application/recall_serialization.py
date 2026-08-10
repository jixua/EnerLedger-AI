"""召回命中序列化的单一来源。

SSE 流式端点（``recall_stream_runtime``）与纯召回 JSON 端点（``recall_json_runtime``）
共用本模块，确保两种载体（SSE 帧 / HTTP JSON）输出的 hits 结构一致，避免双链路漂移。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.rag.core.pipeline.chunk_content import ChunkSource
from app.rag.core.pipeline.recall import RecallDiagnostics, RecallResponse
from app.rag.core.pipeline.rerank import RerankedHit


def _source_fields(
    *,
    chunk_id: str,
    sources: Mapping[str, ChunkSource] | None,
    citation_indexes: Mapping[str, int] | None,
) -> dict:
    source = sources.get(chunk_id) if sources is not None else None
    return {
        "filename": source.filename if source is not None else None,
        "page": source.page if source is not None else None,
        "page_range": source.page_range if source is not None else None,
        "chunk_index": source.chunk_index if source is not None else None,
        "document_version": source.document_version if source is not None else None,
        "citation_index": (
            citation_indexes.get(chunk_id) if citation_indexes is not None else None
        ),
    }


def serialize_hits(
    response: RecallResponse,
    *,
    sources: Mapping[str, ChunkSource] | None = None,
    citation_indexes: Mapping[str, int] | None = None,
    include_content: bool = False,
) -> list[dict]:
    """序列化融合命中，并透出可验证来源与显式引用编号映射。

    ``citation_index`` 由调用方按实际消费场景传入：生成链路必须以最终进入 prompt 的
    ``[片段N]`` 为准；未进入上下文的命中保持 ``None``，避免预算截断后编号漂移。
    """
    result: list[dict] = []
    for result_rank, hit in enumerate(response.hits, start=1):
        chunk_id = str(hit.chunk_id)
        item = {
            "chunk_id": chunk_id,
            "doc_id": hit.doc_id,
            "dataset_id": hit.dataset_id,
            "result_rank": result_rank,
            "fused_score": hit.fused_score,
            "scores": hit.scores,
            **_source_fields(
                chunk_id=chunk_id,
                sources=sources,
                citation_indexes=citation_indexes,
            ),
        }
        if include_content:
            source = sources.get(chunk_id) if sources is not None else None
            item["content"] = source.content if source is not None else ""
        result.append(item)
    return result


def serialize_recall_diagnostics(diagnostics: RecallDiagnostics) -> dict:
    """把召回来源结构诊断序列化为对外稳定 JSON 字段。"""
    return {
        "source_mode": diagnostics.source_mode,
        "degraded": diagnostics.degraded,
        "active_sources": diagnostics.active_sources,
        "per_source_counts": diagnostics.per_source_counts,
        "empty_sources": diagnostics.empty_sources,
        "failed_sources": diagnostics.failed_sources,
    }


def serialize_reranked_hits(
    hits: list[RerankedHit],
    contents: dict[str, str],
    *,
    sources: Mapping[str, ChunkSource] | None = None,
    citation_indexes: Mapping[str, int] | None = None,
) -> list[dict]:
    """把重排后命中裁剪为最小候选；在融合字段基础上补 rerank 分与名次与 chunk 正文。

    ``rerank_score`` / ``rerank_rank`` 在 rerank 未生效（降级）或某候选未拿到 rerank
    分时为 ``None``——降级与「rerank 生效但该候选落入无分 tail」都用 ``None`` 表达，
    调用方据顶层 ``rerank_applied`` 区分二者。

    ``contents`` 为上游一次性回填的 ``chunk_id -> 正文`` 映射（与 rerank / 生成共用同一份，
    不在此重复查库）；某候选无正文时 ``content`` 为空串。前端据此展示召回片段正文，
    无需另起一次反查。纯召回 JSON 端点（``serialize_hits``）不回填正文、不带此字段。
    """
    result: list[dict] = []
    for result_rank, hit in enumerate(hits, start=1):
        chunk_id = str(hit.chunk_id)
        result.append(
            {
                "chunk_id": chunk_id,
                "doc_id": hit.doc_id,
                "dataset_id": hit.dataset_id,
                "result_rank": result_rank,
                "fused_score": hit.fused_score,
                "scores": hit.scores,
                "rerank_score": hit.rerank_score,
                "rerank_rank": hit.rerank_rank,
                "content": contents.get(hit.chunk_id, ""),
                **_source_fields(
                    chunk_id=chunk_id,
                    sources=sources,
                    citation_indexes=citation_indexes,
                ),
            }
        )
    return result
