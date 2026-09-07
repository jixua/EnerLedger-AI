from __future__ import annotations

import base64
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import mysql

from app.api.documents import (
    _encode_preview_asset_ref,
    get_document_preview_map,
    stream_document_preview_asset,
    stream_document_preview_content,
)
from app.domain.models import Document
from app.rag.config import settings
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
        parsed_bucket="parsed",
        parsed_object_key=f"parsed/11/7/91/versions/v{version}/lease-3/report.md",
        parser_backend="opendataloader",
        status=status,
        version=version,
        attempt_count=1,
        reparse_requested=False,
        created_at=_now(),
        updated_at=_now(),
    )


def _chunk(
    *,
    chunk_index: int,
    start_line: int,
    end_line: int,
    content: str,
    role: str = "mixed",
    strategy: str = "candidate_boundary + noop",
    legacy: bool = False,
    line_span_approx: bool = False,
) -> ChunkRecordDB:
    structure = (
        None
        if legacy
        else {
            "chunk_role": role,
            "split_strategy": strategy,
            "line_span_approx": line_span_approx,
        }
    )
    return ChunkRecordDB(
        id=100 + chunk_index,
        chunk_id=f"chunk-{chunk_index}",
        doc_id=91,
        document_version=3,
        set_id=7,
        user_id=11,
        content=content,
        content_hash=f"hash-{chunk_index}",
        chunk_type="mixed",
        start_line=start_line,
        end_line=end_line,
        start_page=chunk_index + 1,
        end_page=chunk_index + 1,
        structure_metadata=structure,
        chunk_index=chunk_index,
        create_time=_now(),
        update_time=_now(),
    )


@pytest.mark.asyncio
async def test_preview_map_uses_line_boundaries_without_joining_overlap_content() -> None:
    records = [
        _chunk(
            chunk_index=0,
            start_line=0,
            end_line=9,
            content="第一分片正文\n下一分片的 neighbor overlap",
        ),
        _chunk(
            chunk_index=1,
            start_line=10,
            end_line=19,
            content="上一分片的 neighbor overlap\n第二分片正文",
        ),
    ]
    db = _FakeSession(scalar_values=[_document()], scalar_lists=[records])

    response = await get_document_preview_map(91, user_id=11, db=db)

    assert response["document_version"] == 3
    assert response["boundary_precision"] == "line"
    assert response["map_reliable"] is True
    assert response["reparse_required"] is False
    assert [(item["start_line"], item["end_line"]) for item in response["boundaries"]] == [
        (0, 9),
        (10, 19),
    ]
    assert [item["boundary_index"] for item in response["boundaries"]] == [0, 1]
    assert response["boundaries"][0]["chunk_type"] == "mixed"
    assert response["boundaries"][0]["split_strategy"] == "candidate_boundary + noop"
    assert all("content" not in item for item in response["boundaries"])

    sql = str(db.statements[-1].compile(dialect=mysql.dialect()))
    assert "document_chunk.document_id" in sql
    assert "document_chunk.dataset_id" in sql
    assert "document_chunk.user_id" in sql
    assert "document_chunk.document_version" in sql
    assert "ORDER BY document_chunk.chunk_index ASC, document_chunk.id ASC" in sql
    assert "LIMIT" not in sql


@pytest.mark.asyncio
async def test_preview_map_filters_derived_elements_from_document_boundaries() -> None:
    records = [
        _chunk(chunk_index=0, start_line=0, end_line=9, content="主体一"),
        _chunk(
            chunk_index=1,
            start_line=4,
            end_line=4,
            content="表格派生语义文本",
            role="derived_element",
        ),
        _chunk(chunk_index=2, start_line=10, end_line=19, content="主体二"),
    ]
    db = _FakeSession(scalar_values=[_document()], scalar_lists=[records])

    response = await get_document_preview_map(91, user_id=11, db=db)

    assert response["source_chunk_count"] == 2
    assert response["derived_chunk_count"] == 1
    assert [item["chunk_index"] for item in response["boundaries"]] == [0, 2]


@pytest.mark.asyncio
async def test_preview_map_returns_strict_legacy_fallback_and_requires_reparse() -> None:
    records = [
        _chunk(chunk_index=0, start_line=0, end_line=9, content="旧分片一", legacy=True),
        _chunk(chunk_index=1, start_line=10, end_line=19, content="旧分片二", legacy=True),
    ]
    db = _FakeSession(scalar_values=[_document()], scalar_lists=[records])

    response = await get_document_preview_map(91, user_id=11, db=db)

    assert response["boundary_precision"] == "legacy_line"
    assert response["map_reliable"] is False
    assert response["reparse_required"] is True
    assert [item["chunk_index"] for item in response["boundaries"]] == [0, 1]


@pytest.mark.asyncio
async def test_preview_map_skips_nested_legacy_derived_range() -> None:
    records = [
        _chunk(chunk_index=0, start_line=0, end_line=9, content="旧分片一", legacy=True),
        _chunk(chunk_index=1, start_line=3, end_line=7, content="旧表格派生块", legacy=True),
        _chunk(chunk_index=2, start_line=10, end_line=19, content="旧分片二", legacy=True),
    ]
    db = _FakeSession(scalar_values=[_document()], scalar_lists=[records])

    response = await get_document_preview_map(91, user_id=11, db=db)

    assert [item["chunk_index"] for item in response["boundaries"]] == [0, 2]
    assert [item["boundary_index"] for item in response["boundaries"]] == [0, 1]
    assert response["source_chunk_count"] == 2
    assert response["derived_chunk_count"] == 1
    assert response["map_reliable"] is False
    assert response["reparse_required"] is True


@pytest.mark.asyncio
async def test_preview_map_marks_semantic_depth_boundaries_as_approximate() -> None:
    records = [
        _chunk(
            chunk_index=0,
            start_line=0,
            end_line=9,
            content="语义细分片",
            strategy="candidate_boundary + semantic_depth_window",
            line_span_approx=True,
        )
    ]
    db = _FakeSession(scalar_values=[_document()], scalar_lists=[records])

    response = await get_document_preview_map(91, user_id=11, db=db)

    assert response["boundary_precision"] == "approximate_line"
    assert response["map_reliable"] is False
    assert response["reparse_required"] is False


@pytest.mark.asyncio
async def test_preview_map_accepts_line_aligned_semantic_boundaries_as_exact() -> None:
    records = [
        _chunk(
            chunk_index=0,
            start_line=0,
            end_line=9,
            content="语义细分片一",
            strategy="candidate_boundary + semantic_depth_window",
        ),
        _chunk(
            chunk_index=1,
            start_line=10,
            end_line=19,
            content="语义细分片二",
            strategy="candidate_boundary + semantic_depth_window",
        ),
    ]
    db = _FakeSession(scalar_values=[_document()], scalar_lists=[records])

    response = await get_document_preview_map(91, user_id=11, db=db)

    assert response["boundary_precision"] == "line"
    assert response["map_reliable"] is True
    assert response["reparse_required"] is False


@pytest.mark.asyncio
async def test_preview_map_does_not_claim_reliability_without_source_chunks() -> None:
    db = _FakeSession(scalar_values=[_document()], scalar_lists=[[]])

    response = await get_document_preview_map(91, user_id=11, db=db)

    assert response["source_chunk_count"] == 0
    assert response["boundaries"] == []
    assert response["map_reliable"] is False
    assert response["reparse_required"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [get_document_preview_map, stream_document_preview_content])
async def test_document_preview_rejects_non_ready_document(endpoint) -> None:
    db = _FakeSession(scalar_values=[_document(status="PROCESSING")])

    with pytest.raises(HTTPException) as exc_info:
        await endpoint(91, user_id=11, db=db)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "DOCUMENT_PREVIEW_NOT_READY"
    assert len(db.statements) == 1


class _WritingStorage:
    def __init__(self, content: bytes):
        self.content = content
        self.calls = []

    def download_to_path(self, bucket: str, object_key: str, dst: Path) -> None:
        self.calls.append((bucket, object_key, dst))
        dst.write_bytes(self.content)


@pytest.mark.asyncio
async def test_preview_content_streams_current_markdown_and_cleans_temp_file(
    tmp_path, monkeypatch
) -> None:
    markdown = "# 能碳报告\n\n完整文档正文。".encode()
    storage = _WritingStorage(markdown)
    monkeypatch.setattr(settings, "PARSE_TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(
        "app.api.documents.StorageFactory.get_storage",
        lambda: storage,
    )
    db = _FakeSession(scalar_values=[_document(version=3)])

    response = await stream_document_preview_content(91, user_id=11, db=db)
    body = b"".join([part async for part in response.body_iterator])

    assert body == markdown
    assert response.headers["x-document-version"] == "3"
    assert response.headers["content-length"] == str(len(markdown))
    assert response.headers["cache-control"] == "no-store"
    assert response.media_type == "text/markdown; charset=utf-8"
    assert [(bucket, key) for bucket, key, _ in storage.calls] == [
        ("parsed", "parsed/11/7/91/versions/v3/lease-3/report.md")
    ]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_preview_content_rewrites_only_current_private_image_urls(
    tmp_path, monkeypatch
) -> None:
    current_image = (
        "http://minio:9000/parsed/parsed/11/7/91/versions/v3/lease-3/"
        "image/images/page-001.png"
    )
    other_document_image = (
        "http://minio:9000/parsed/parsed/11/7/92/versions/v3/lease-3/"
        "image/images/page-001.png"
    )
    markdown = (
        f"![当前文档]({current_image})\n"
        f"![其他文档]({other_document_image})\n"
        "![外部资源](https://assets.example/chart.png)\n"
        "![内联资源](data:image/png;base64,AAAA)\n"
    ).encode()
    storage = _WritingStorage(markdown)
    monkeypatch.setattr(settings, "PARSE_TEMP_DIR", str(tmp_path))
    monkeypatch.setattr("app.api.documents.StorageFactory.get_storage", lambda: storage)
    db = _FakeSession(scalar_values=[_document(version=3)])

    response = await stream_document_preview_content(91, user_id=11, db=db)
    body = b"".join([part async for part in response.body_iterator]).decode()

    asset_ref = _encode_preview_asset_ref("image/images/page-001.png")
    proxy_url = f"/api/v1/documents/91/preview/versions/3/assets/{asset_ref}"
    assert f"![当前文档]({proxy_url})" in body
    assert other_document_image in body
    assert "https://assets.example/chart.png" in body
    assert "data:image/png;base64,AAAA" in body
    assert "parsed/11/7/91" not in body
    assert response.headers["content-length"] == str(len(body.encode()))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_preview_asset_is_tenant_scoped_and_resolved_inside_current_version(
    tmp_path, monkeypatch
) -> None:
    image = b"\x89PNG\r\nprivate-image"
    storage = _WritingStorage(image)
    monkeypatch.setattr(settings, "PARSE_TEMP_DIR", str(tmp_path))
    monkeypatch.setattr("app.api.documents.StorageFactory.get_storage", lambda: storage)
    db = _FakeSession(scalar_values=[_document(version=3)])
    asset_ref = _encode_preview_asset_ref("image/images/page-001.png")

    response = await stream_document_preview_asset(
        91,
        3,
        asset_ref,
        user_id=11,
        db=db,
    )
    body = b"".join([part async for part in response.body_iterator])

    assert body == image
    assert response.media_type == "image/png"
    assert response.headers["x-document-version"] == "3"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert [(bucket, key) for bucket, key, _ in storage.calls] == [
        (
            "parsed",
            "parsed/11/7/91/versions/v3/lease-3/image/images/page-001.png",
        )
    ]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_preview_asset_rejects_stale_version_and_path_traversal() -> None:
    stale_db = _FakeSession(scalar_values=[_document(version=3)])
    asset_ref = _encode_preview_asset_ref("image/images/page-001.png")

    with pytest.raises(HTTPException) as stale_info:
        await stream_document_preview_asset(91, 2, asset_ref, user_id=11, db=stale_db)
    assert stale_info.value.status_code == 404

    traversal_ref = base64.urlsafe_b64encode(b"../secret.png").decode().rstrip("=")
    traversal_db = _FakeSession(scalar_values=[_document(version=3)])
    with pytest.raises(HTTPException) as traversal_info:
        await stream_document_preview_asset(
            91,
            3,
            traversal_ref,
            user_id=11,
            db=traversal_db,
        )
    assert traversal_info.value.status_code == 404


class _FailingStorage:
    def download_to_path(self, bucket: str, object_key: str, dst: Path) -> None:
        raise OSError("storage unavailable")


@pytest.mark.asyncio
async def test_preview_content_maps_storage_error_to_502_and_cleans_temp_file(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "PARSE_TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(
        "app.api.documents.StorageFactory.get_storage",
        lambda: _FailingStorage(),
    )
    db = _FakeSession(scalar_values=[_document()])

    with pytest.raises(HTTPException) as exc_info:
        await stream_document_preview_content(91, user_id=11, db=db)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "DOCUMENT_PREVIEW_STORAGE_UNAVAILABLE"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_document_preview_preserves_tenant_boundary() -> None:
    db = _FakeSession(scalar_values=[None])

    with pytest.raises(HTTPException) as exc_info:
        await get_document_preview_map(91, user_id=12, db=db)

    assert exc_info.value.status_code == 404
    assert len(db.statements) == 1
