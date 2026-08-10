from __future__ import annotations

from app.rag.application.recall_serialization import serialize_hits
from app.rag.core.pipeline.chunk_content import ChunkSource
from app.rag.core.pipeline.recall import RecallHit, RecallResponse


def test_serialize_hits_exposes_verified_source_and_citation_mapping() -> None:
    response = RecallResponse(
        query="核算边界",
        hits=[
            RecallHit(
                chunk_id="chunk-1",
                doc_id=11,
                dataset_id=21,
                fused_score=0.88,
                scores={"bm25": 2.1, "sparse": 0.5, "dense": 0.8},
            ),
            RecallHit(
                chunk_id="chunk-2",
                doc_id=12,
                dataset_id=21,
                fused_score=0.72,
                scores={"bm25": 1.1, "sparse": None, "dense": 0.7},
            ),
        ],
        per_source_counts={"bm25": 2, "sparse": 1, "dense": 2},
        failed_sources=[],
        elapsed_ms=4,
    )
    sources = {
        "chunk-1": ChunkSource(
            content="第一段",
            filename="年度报告.pdf",
            chunk_index=3,
            document_version=4,
            page=5,
            page_range={"start": 5, "end": 6},
        ),
        "chunk-2": ChunkSource(
            content="第二段",
            filename="核算说明.docx",
            chunk_index=8,
        ),
    }

    hits = serialize_hits(
        response,
        sources=sources,
        citation_indexes={"chunk-2": 1},
        include_content=True,
    )

    assert hits[0]["filename"] == "年度报告.pdf"
    assert hits[0]["page"] == 5
    assert hits[0]["page_range"] == {"start": 5, "end": 6}
    assert hits[0]["chunk_index"] == 3
    assert hits[0]["document_version"] == 4
    assert hits[0]["citation_index"] is None
    assert hits[0]["content"] == "第一段"
    assert hits[1]["filename"] == "核算说明.docx"
    assert hits[1]["page"] is None
    assert hits[1]["page_range"] is None
    assert hits[1]["chunk_index"] == 8
    assert hits[1]["document_version"] == 1
    assert hits[1]["citation_index"] == 1


def test_serialize_hits_uses_nulls_instead_of_fabricating_missing_source() -> None:
    response = RecallResponse(
        query="问题",
        hits=[
            RecallHit(
                chunk_id="orphan",
                doc_id=11,
                dataset_id=21,
                fused_score=0.4,
                scores={"bm25": 1.0},
            )
        ],
        per_source_counts={"bm25": 1},
        failed_sources=[],
        elapsed_ms=1,
    )

    assert serialize_hits(response)[0] == {
        "chunk_id": "orphan",
        "doc_id": 11,
        "dataset_id": 21,
        "result_rank": 1,
        "fused_score": 0.4,
        "scores": {"bm25": 1.0},
        "filename": None,
        "page": None,
        "page_range": None,
        "chunk_index": None,
        "document_version": None,
        "citation_index": None,
    }
