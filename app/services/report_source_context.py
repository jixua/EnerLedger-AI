"""Authoritative source manifests used to validate report evidence and coverage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Document, ReportQuestion, ReportRun
from app.rag.models.chunk_record import ChunkRecordDB
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


async def load_report_source_context(
    db: AsyncSession,
    *,
    run: ReportRun,
) -> tuple[ReportEvidenceContext, ReportChunkManifest]:
    chunks = (
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
    manifest_items = tuple(
        {
            "chunk_id": str(chunk.chunk_id),
            "chunk_index": int(chunk.chunk_index),
            "content_hash": str(chunk.content_hash),
        }
        for chunk in chunks
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
        document_chunks={
            str(chunk.chunk_id): {
                "content_hash": str(chunk.content_hash),
                "content": chunk.content,
            }
            for chunk in chunks
        },
        user_answer_hashes=answer_hashes,
    )
    return evidence_context, ReportChunkManifest(
        items=manifest_items,
        content_hash=_canonical_hash(manifest_items),
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
