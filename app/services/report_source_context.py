"""Authoritative source manifests used to validate report evidence and coverage.

来源有两种形态，覆盖校验与证据校验对它们一视同仁：

- ``DOCUMENT``：已入库解析的文档，分片在 ``document_chunk`` 表里。
- ``INLINE``：对话直传的材料，不进知识库；分片冻结在这次任务的对象存储上，
  由 ``report_inline_source`` 负责切分与读取。

两条通路产出的是同一个 ``ReportChunkManifest``，所以下游一行都不用分叉。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Document, ReportQuestion, ReportRun
from app.rag.models.chunk_record import ChunkRecordDB
from app.rag.services.storage.base import BaseObjectStorage
from app.rag.services.storage.factory import StorageFactory
from app.services.report_inline_source import (
    SOURCE_KIND_INLINE,
    manifest_items,
    payload_chunks,
    read_inline_source,
)
from app.services.report_ir import ReportEvidenceContext


@dataclass(frozen=True, slots=True)
class ReportChunkManifest:
    items: tuple[dict[str, Any], ...]
    content_hash: str

    @property
    def chunk_ids(self) -> tuple[str, ...]:
        return tuple(str(item["chunk_id"]) for item in self.items)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _inline_manifest(
    *, bucket: str, object_key: str, storage: BaseObjectStorage | None = None
) -> ReportChunkManifest:
    payload = await read_inline_source(
        storage or StorageFactory.get_storage(), bucket=bucket, object_key=object_key
    )
    items = manifest_items(payload_chunks(payload))
    return ReportChunkManifest(items=items, content_hash=_canonical_hash(items))


async def load_inline_chunk_manifest(
    run: ReportRun, *, storage: BaseObjectStorage | None = None
) -> ReportChunkManifest:
    """读回冻结的内联来源正文，重建清单。

    清单哈希现算，与创建任务时写进 ``document_manifest`` 的值比对由调用方负责——
    这里只保证「读回来的这批分片」是一致的。
    """
    return await _inline_manifest(
        bucket=run.parsed_bucket, object_key=run.parsed_object_key, storage=storage
    )


async def load_inline_template_manifest(
    run: ReportRun, *, storage: BaseObjectStorage | None = None
) -> ReportChunkManifest:
    """读回冻结的内联版式模板，重建清单。位置记在 ``custom_template_manifest`` 里。"""
    manifest = run.custom_template_manifest or {}
    return await _inline_manifest(
        bucket=str(manifest.get("bucket") or run.parsed_bucket),
        object_key=str(manifest.get("object_key") or ""),
        storage=storage,
    )


async def load_report_source_context(
    db: AsyncSession,
    *,
    run: ReportRun,
    storage: BaseObjectStorage | None = None,
) -> tuple[ReportEvidenceContext, ReportChunkManifest]:
    if run.source_kind == SOURCE_KIND_INLINE:
        inline_chunks = payload_chunks(
            await read_inline_source(
                storage or StorageFactory.get_storage(),
                bucket=run.parsed_bucket,
                object_key=run.parsed_object_key,
            )
        )
        evidence_chunks = {
            chunk.chunk_id: {"content_hash": chunk.content_hash, "content": chunk.content}
            for chunk in inline_chunks
        }
        manifest_entries = manifest_items(inline_chunks)
    else:
        rows = (
            await db.scalars(
                select(ChunkRecordDB)
                .where(
                    ChunkRecordDB.doc_id == run.document_id,
                    ChunkRecordDB.document_version == run.document_version,
                    ChunkRecordDB.user_id == run.user_id,
                    ChunkRecordDB.set_id == run.dataset_id,
                )
                .order_by(ChunkRecordDB.chunk_index, ChunkRecordDB.id)
            )
        ).all()
        evidence_chunks = {
            str(chunk.chunk_id): {
                "content_hash": str(chunk.content_hash),
                "content": chunk.content,
            }
            for chunk in rows
        }
        manifest_entries = tuple(
            {
                "chunk_id": str(chunk.chunk_id),
                "chunk_index": int(chunk.chunk_index),
                "content_hash": str(chunk.content_hash),
            }
            for chunk in rows
        )
    questions = (
        await db.scalars(
            select(ReportQuestion)
            .where(
                ReportQuestion.run_id == run.id,
                ReportQuestion.status == "ANSWERED",
            )
            .order_by(ReportQuestion.id)
        )
    ).all()
    answer_hashes = {
        int(question.id): _canonical_hash((question.answer or {}).get("value"))
        for question in questions
    }
    evidence_context = ReportEvidenceContext(
        document_chunks=evidence_chunks,
        user_answer_hashes=answer_hashes,
    )
    return evidence_context, ReportChunkManifest(
        items=manifest_entries,
        content_hash=_canonical_hash(manifest_entries),
    )


def validate_chunk_coverage(coverage: dict[str, Any], manifest: ReportChunkManifest) -> list[str]:
    errors: list[str] = []
    if coverage.get("complete") is not True:
        errors.append("Agent 未读取到文档分片末尾")
    observed = coverage.get("chunks")
    if not isinstance(observed, list):
        return [*errors, "Agent 分片覆盖记录缺失"]
    normalized_items: list[dict[str, Any]] = []
    try:
        for item in observed:
            if not isinstance(item, dict):
                raise TypeError
            normalized_items.append(
                {
                    "chunk_id": str(item["chunk_id"]),
                    "chunk_index": int(item["chunk_index"]),
                    "content_hash": str(item["content_hash"]),
                }
            )
    except (KeyError, TypeError, ValueError):
        return [*errors, "Agent 分片覆盖记录格式无效"]
    normalized = tuple(normalized_items)
    if normalized != manifest.items:
        errors.append("Agent 分片覆盖记录与冻结文档不一致")
    if coverage.get("manifest_hash") != manifest.content_hash:
        errors.append("Agent 分片 Manifest 哈希不一致")
    return errors


async def load_document_chunk_manifest(
    db: AsyncSession, *, document: Document
) -> ReportChunkManifest:
    chunks = (
        await db.scalars(
            select(ChunkRecordDB)
            .where(
                ChunkRecordDB.doc_id == document.id,
                ChunkRecordDB.document_version == document.version,
                ChunkRecordDB.user_id == document.user_id,
                ChunkRecordDB.set_id == document.dataset_id,
            )
            .order_by(ChunkRecordDB.chunk_index, ChunkRecordDB.id)
        )
    ).all()
    items = tuple(
        {
            "chunk_id": str(chunk.chunk_id),
            "chunk_index": int(chunk.chunk_index),
            "content_hash": str(chunk.content_hash),
        }
        for chunk in chunks
    )
    return ReportChunkManifest(items=items, content_hash=_canonical_hash(items))
