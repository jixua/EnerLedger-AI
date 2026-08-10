from __future__ import annotations

from collections import deque
from datetime import timedelta
from io import BytesIO

import pytest
from fastapi import HTTPException, Response, UploadFile
from pydantic import ValidationError

import app.api.documents as documents_api
from app.api.documents import (
    _document_payload,
    delete_document,
    reparse_document,
    retry_document,
    update_document,
    upload_and_queue_document,
)
from app.domain.models import Dataset, Document
from app.domain.schemas import DocumentUpdate
from app.services.document_queue import utc_now


class _FakeStorage:
    def __init__(self):
        self.uploads = []
        self.removed = []

    def upload_from_path(self, bucket, object_key, source, content_type):
        self.uploads.append((bucket, object_key, source.read_bytes(), content_type))

    def remove_prefix(self, bucket, prefix):
        self.removed.append((bucket, prefix))
        return 1


class _FakeSession:
    def __init__(self, scalar_values=()):
        self.scalar_values = deque(scalar_values)
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.refreshes = 0

    async def scalar(self, _statement):
        return self.scalar_values.popleft() if self.scalar_values else None

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, value):
        self.refreshes += 1
        if isinstance(value, Document) and value.id is None:
            value.id = 88


def _dataset() -> Dataset:
    return Dataset(
        id=9,
        user_id=11,
        name="碳资料",
        status="ACTIVE",
        dense_embedding_config_id=1,
        sparse_embedding_config_id=2,
    )


def _document(*, status: str = "FAILED", version: int = 1) -> Document:
    now = utc_now()
    return Document(
        id=7,
        dataset_id=9,
        user_id=11,
        filename="old.pdf",
        file_type="pdf",
        file_size=10,
        content_type="application/pdf",
        raw_bucket="raw",
        raw_object_key="raw/old.pdf",
        status=status,
        version=version,
        attempt_count=3,
        created_at=now,
        updated_at=now,
        reparse_requested=False,
    )


def test_document_payload_exposes_parse_quality_without_internal_storage_fields() -> None:
    document = _document(status="READY")
    document.parse_quality_status = "PASSED"
    document.parse_quality = {
        "status": "PASSED",
        "page_count": 4,
        "warnings": [],
    }

    payload = _document_payload(document)

    assert payload["parse_quality_status"] == "PASSED"
    assert payload["parse_quality"] == {
        "status": "PASSED",
        "page_count": 4,
        "warnings": [],
    }
    assert payload["retrieval_ready"] is True
    assert "raw_object_key" not in payload
    assert "parsed_object_key" not in payload


def test_document_retrieval_ready_matches_pdf_quality_gate() -> None:
    legacy_pdf = _document(status="READY")
    assert _document_payload(legacy_pdf)["retrieval_ready"] is False

    passed_pdf = _document(status="READY")
    passed_pdf.parse_quality_status = "PASSED"
    passed_pdf.parse_quality = {"status": "PASSED"}
    assert _document_payload(passed_pdf)["retrieval_ready"] is True

    inconsistent_pdf = _document(status="READY")
    inconsistent_pdf.parse_quality_status = "PASSED"
    inconsistent_pdf.parse_quality = {"status": "FALLBACK_INCOMPLETE"}
    assert _document_payload(inconsistent_pdf)["retrieval_ready"] is False

    queued_pdf = _document(status="QUEUED")
    queued_pdf.parse_quality_status = "PASSED"
    queued_pdf.parse_quality = {"status": "PASSED"}
    assert _document_payload(queued_pdf)["retrieval_ready"] is False

    ready_docx = _document(status="READY")
    ready_docx.file_type = "docx"
    assert _document_payload(ready_docx)["retrieval_ready"] is False
    ready_docx.parse_quality_status = "PASSED"
    ready_docx.parse_quality = {"status": "PASSED"}
    assert _document_payload(ready_docx)["retrieval_ready"] is True


def test_document_list_quality_summary_omits_ocr_text_and_structured_assets() -> None:
    full_report = {
        "schema_version": 2,
        "status": "PASSED",
        "pdf_page_count": 4,
        "text_coverage_ratio": 1.0,
        "warnings": ["warning-1"],
        "fallback": {
            "dpi": 280,
            "processed_page_count": 2,
            "warnings": [],
            "results": [
                {
                    "page_number": 2,
                    "method": "ocr",
                    "text": "sensitive duplicated OCR body",
                    "markdown": "complete merged page markdown",
                },
                {"page_number": 3, "method": "vision", "structured_data": {"x": 1}},
            ],
            "ocr_results": {"2": {"text": "duplicated OCR body"}},
        },
        "content_validation": {
            "structural_passed": True,
            "evaluated_page_count": 4,
            "blocking_issues": [],
            "warnings": [],
        },
        "table_structure": {"tables": [{"cells": [{"text": "large table"}]}]},
        "image_assets": {"assets": [{"retrieval_text": "large visual result"}]},
    }

    document = _document(status="READY")
    document.parse_quality_status = "PASSED"
    document.parse_quality = full_report
    summary = _document_payload(document, quality_detail=False)["parse_quality"]

    assert summary == {
        "schema_version": 2,
        "status": "PASSED",
        "pdf_page_count": 4,
        "text_coverage_ratio": 1.0,
        "warning_count": 1,
        "warnings": ["warning-1"],
        "fallback": {
            "dpi": 280,
            "processed_page_count": 2,
            "ocr_page_count": 1,
            "vision_page_count": 1,
            "pages": [2, 3],
            "warning_count": 0,
            "warnings": [],
        },
        "content_validation": {
            "structural_passed": True,
            "evaluated_page_count": 4,
            "blocking_issue_count": 0,
            "warning_count": 0,
        },
    }
    assert "table_structure" not in summary
    assert "image_assets" not in summary
    assert "results" not in summary["fallback"]
    assert _document_payload(document)["parse_quality"] is full_report


@pytest.mark.asyncio
async def test_upload_streams_to_storage_and_returns_queued_location(monkeypatch, tmp_path) -> None:
    storage = _FakeStorage()
    monkeypatch.setattr(documents_api.StorageFactory, "get_storage", lambda: storage)
    monkeypatch.setattr(documents_api.settings, "PARSE_TEMP_DIR", str(tmp_path))
    dataset = _dataset()
    db = _FakeSession([dataset, dataset])
    response = Response()
    upload = UploadFile(filename="report.pdf", file=BytesIO(b"pdf-binary"))

    result = await upload_and_queue_document(9, response, upload, 11, db)

    assert result["document_id"] == 88
    assert result["status"] == "QUEUED"
    assert result["version"] == 1
    assert result["attempt_count"] == 0
    assert result["parse_quality_status"] is None
    assert result["parse_quality"] is None
    assert response.headers["Location"] == "/api/v1/documents/88"
    assert storage.uploads[0][2] == b"pdf-binary"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_retry_recovers_failed_and_stale_processing_without_new_version() -> None:
    failed = _document(status="FAILED", version=4)
    failed.reparse_requested = True
    failed_db = _FakeSession([failed])
    response = Response()

    result = await retry_document(7, response, 11, failed_db)

    assert result["status"] == "QUEUED"
    assert result["version"] == 4
    assert result["reparse_requested"] is True
    assert result["attempt_count"] == 0

    stale = _document(status="PROCESSING", version=1)
    stale.lease_expires_at = utc_now() - timedelta(seconds=1)
    stale_db = _FakeSession([stale])
    assert (await retry_document(7, Response(), 11, stale_db))["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_reparse_increments_version_but_retry_does_not() -> None:
    document = _document(status="READY", version=2)
    db = _FakeSession([document])

    result = await reparse_document(7, Response(), 11, db)

    assert result["status"] == "QUEUED"
    assert result["version"] == 3
    assert result["reparse_requested"] is True


@pytest.mark.asyncio
async def test_document_filename_update_is_tenant_scoped_and_keeps_file_contract() -> None:
    document = _document(status="PROCESSING")
    raw_object_key = document.raw_object_key
    db = _FakeSession([document])

    result = await update_document(
        7,
        DocumentUpdate(filename="  renamed-report.pdf  "),
        11,
        db,
    )

    assert result["filename"] == "renamed-report.pdf"
    assert document.file_type == "pdf"
    assert document.raw_object_key == raw_object_key
    assert db.commits == 1

    with pytest.raises(ValidationError):
        DocumentUpdate(filename="   ")

    foreign_db = _FakeSession([None])
    with pytest.raises(HTTPException) as exc_info:
        await update_document(7, DocumentUpdate(filename="hidden.pdf"), 12, foreign_db)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_nonterminal_delete_is_rejected_and_terminal_document_can_be_deleted(
    monkeypatch,
) -> None:
    active = _document(status="PROCESSING")
    active.lease_expires_at = utc_now() + timedelta(seconds=60)
    with pytest.raises(HTTPException) as exc_info:
        await delete_document(7, 11, _FakeSession([active]))
    assert exc_info.value.status_code == 409

    expired = _document(status="PROCESSING")
    expired.lease_expires_at = utc_now() - timedelta(seconds=1)
    with pytest.raises(HTTPException) as expired_info:
        await delete_document(7, 11, _FakeSession([expired]))
    assert expired_info.value.status_code == 409
    assert expired_info.value.detail["code"] == "DOCUMENT_NOT_DELETABLE"

    ready = _document(status="READY")
    calls = []

    class _FakeIngestion:
        def __init__(self, **_kwargs):
            pass

        async def purge_document(self, document, db, *, include_raw):
            calls.append((document.id, include_raw))
            await db.commit()

    monkeypatch.setattr(documents_api, "SimpleDocumentIngestionService", _FakeIngestion)
    monkeypatch.setattr(documents_api.StorageFactory, "get_storage", lambda: _FakeStorage())
    await delete_document(7, 11, _FakeSession([ready]))
    assert calls == [(7, True)]
