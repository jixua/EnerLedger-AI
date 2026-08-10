from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import mysql

from app.api.documents import list_document_chunks
from app.domain.models import Document
from app.rag.models.chunk_record import ChunkRecordDB


class _FakeScalarResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _FakeSession:
    def __init__(self, *, scalar_values=(), scalar_lists=()):
        self.scalar_values = deque(scalar_values)
        self.scalar_lists = deque(scalar_lists)
        self.statements = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.scalar_values.popleft()

    async def scalars(self, statement):
        self.statements.append(statement)
        return _FakeScalarResult(self.scalar_lists.popleft())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _document(*, status: str = "READY", version: int = 3) -> Document:
    return Document(
        id=91,
        dataset_id=7,
        user_id=11,
        filename="排放因子.pdf",
        file_type="pdf",
        file_size=100,
        content_type="application/pdf",
        raw_bucket="raw",
        raw_object_key="raw/report.pdf",
        parser_backend="opendataloader",
        status=status,
        version=version,
        attempt_count=1,
        reparse_requested=False,
        created_at=_now(),
        updated_at=_now(),
    )


def _chunk(*, chunk_index: int, content: str, chunk_type: str = "mixed") -> ChunkRecordDB:
    return ChunkRecordDB(
        id=100 + chunk_index,
        chunk_id=f"chunk-{chunk_index}",
        doc_id=91,
        document_version=3,
        set_id=7,
        user_id=11,
        content=content,
        content_hash=f"hash-{chunk_index}",
        chunk_type=chunk_type,
        start_line=chunk_index * 10 + 1,
        end_line=chunk_index * 10 + 9,
        start_page=chunk_index + 1,
        end_page=chunk_index + 1,
        structure_metadata={
            "heading_trail": ["核算方法"],
            "split_strategy": "candidate_boundary + noop",
            "chunk_role": "mixed",
        },
        chunk_index=chunk_index,
        create_time=_now(),
        update_time=_now(),
    )


@pytest.mark.asyncio
async def test_list_document_chunks_returns_traceable_current_version_page() -> None:
    chunks = [
        _chunk(chunk_index=0, content="第一段排放因子说明"),
        _chunk(chunk_index=1, content="第二段核算边界", chunk_type="table"),
    ]
    db = _FakeSession(scalar_values=[_document(), 2], scalar_lists=[chunks])

    response = await list_document_chunks(
        91,
        offset=0,
        limit=20,
        query=None,
        chunk_type=None,
        user_id=11,
        db=db,
    )

    assert response["document_id"] == 91
    assert response["dataset_id"] == 7
    assert response["document_version"] == 3
    assert response["total"] == 2
    assert [item["chunk_index"] for item in response["items"]] == [0, 1]
    assert response["items"][0]["char_count"] == len("第一段排放因子说明")
    assert response["items"][1]["start_page"] == 2
    assert response["items"][0]["structure"]["heading_trail"] == ["核算方法"]
    assert "content_hash" not in response["items"][0]
    assert "user_id" not in response["items"][0]

    item_statement = db.statements[-1]
    sql = str(item_statement.compile(dialect=mysql.dialect()))
    assert "document_chunk.document_id" in sql
    assert "document_chunk.dataset_id" in sql
    assert "document_chunk.user_id" in sql
    assert "document_chunk.document_version" in sql
    assert "ORDER BY document_chunk.chunk_index ASC, document_chunk.id ASC" in sql
    assert "LIMIT %s, %s" in sql


@pytest.mark.asyncio
async def test_list_document_chunks_filters_content_and_type_with_escaped_like() -> None:
    db = _FakeSession(
        scalar_values=[_document(), 1],
        scalar_lists=[[_chunk(chunk_index=1, content="排放率 50%_样例", chunk_type="table")]],
    )

    response = await list_document_chunks(
        91,
        offset=20,
        limit=10,
        query="50%_",
        chunk_type=" TABLE ",
        user_id=11,
        db=db,
    )

    assert response["offset"] == 20
    assert response["limit"] == 10
    count_statement = db.statements[1]
    params = count_statement.compile(dialect=mysql.dialect()).params
    assert "50/%/_" in params.values()
    assert "table" in params.values()


@pytest.mark.asyncio
async def test_list_document_chunks_rejects_blank_chunk_type_after_normalization() -> None:
    db = _FakeSession(scalar_values=[_document()])

    with pytest.raises(HTTPException) as exc_info:
        await list_document_chunks(
            91,
            offset=0,
            limit=20,
            query=None,
            chunk_type="   ",
            user_id=11,
            db=db,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "分片类型不能为空"
    assert len(db.statements) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["QUEUED", "PROCESSING", "FAILED"])
async def test_list_document_chunks_rejects_non_ready_current_version(status: str) -> None:
    db = _FakeSession(scalar_values=[_document(status=status)])

    with pytest.raises(HTTPException) as exc_info:
        await list_document_chunks(
            91,
            offset=0,
            limit=20,
            query=None,
            chunk_type=None,
            user_id=11,
            db=db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "DOCUMENT_CHUNKS_NOT_READY"
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_list_document_chunks_preserves_document_tenant_boundary() -> None:
    db = _FakeSession(scalar_values=[None])

    with pytest.raises(HTTPException) as exc_info:
        await list_document_chunks(
            91,
            offset=0,
            limit=20,
            query=None,
            chunk_type=None,
            user_id=12,
            db=db,
        )

    assert exc_info.value.status_code == 404
    assert len(db.statements) == 1
