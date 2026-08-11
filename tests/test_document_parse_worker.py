from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.workers.document_parse_worker as worker_module
from app.domain.models import Document
from app.rag.core.mq.messages import DocumentIngestionMessage
from app.services.document_ingestion import DocumentQualityGateError
from app.services.document_queue import DocumentClaim, utc_now
from app.workers.document_parse_worker import DocumentParseWorker


class _FakeSession:
    def __init__(self, document):
        self.document = document

    async def scalar(self, _statement):
        return self.document


class _FakeStorage:
    def download_to_path(self, bucket, object_key, destination: Path):
        assert (bucket, object_key) == ("raw", "raw/report.pdf")
        destination.write_bytes(b"pdf")


class _FakeQueue:
    def __init__(self):
        self.lease_seconds = 7200
        self.failures = []
        self.renewals = 0

    async def owns_lease(self, _db, _claim):
        return True

    async def renew_lease(self, _db, _claim):
        self.renewals += 1
        return True

    async def fail_or_retry(self, _db, claim, error, *, retryable, **metadata):
        self.failures.append((claim, error, retryable, metadata))
        return "QUEUED"


class _FakeIngestion:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def ingest(self, document, source_path, db, **kwargs):
        assert source_path.read_bytes() == b"pdf"
        assert await kwargs["lease_guard"]() is True
        self.calls.append((document, db, kwargs))
        if self.error:
            raise self.error


class _PushedQueue(_FakeQueue):
    def __init__(self):
        super().__init__()
        self.claimed_ids = []

    async def snapshot(self, _db, *, document_id):
        return SimpleNamespace(
            document_id=document_id,
            user_id=11,
            dataset_id=9,
            version=2,
            status="QUEUED",
            available_at=utc_now(),
            lease_expires_at=None,
        )

    async def claim_document(self, _db, *, document_id, worker_id):
        self.claimed_ids.append((document_id, worker_id))
        return DocumentClaim(document_id, "token", 1, False, False)


def _document() -> Document:
    now = utc_now()
    return Document(
        id=7,
        dataset_id=9,
        user_id=11,
        filename="report.pdf",
        file_type="pdf",
        file_size=10,
        raw_bucket="raw",
        raw_object_key="raw/report.pdf",
        status="PROCESSING",
        version=2,
        attempt_count=1,
        lease_token="token",
        lease_expires_at=now,
    )


def _session_factory(document):
    @asynccontextmanager
    async def factory():
        yield _FakeSession(document)

    return factory


@pytest.mark.asyncio
async def test_worker_downloads_source_and_passes_reparse_and_fencing(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(worker_module.settings, "PARSE_TEMP_DIR", str(tmp_path))
    document = _document()
    queue = _FakeQueue()
    ingestion = _FakeIngestion()
    worker = DocumentParseWorker(
        queue=queue,
        ingestion=ingestion,
        storage=_FakeStorage(),
        session_context_factory=_session_factory(document),
        heartbeat_interval=3600,
    )
    claim = DocumentClaim(7, "token", 1, False, True)

    await worker._process_claim(claim)

    assert len(ingestion.calls) == 1
    kwargs = ingestion.calls[0][2]
    assert kwargs["replace_existing"] is True
    assert kwargs["lease_token"] == "token"
    assert kwargs["manage_failure"] is False
    assert queue.failures == []


@pytest.mark.asyncio
async def test_pushed_message_claims_exact_document_without_queue_scan(monkeypatch) -> None:
    queue = _PushedQueue()
    worker = DocumentParseWorker(
        queue=queue,
        ingestion=_FakeIngestion(),
        storage=_FakeStorage(),
        session_context_factory=_session_factory(_document()),
        heartbeat_interval=3600,
        worker_id="rabbit-worker",
    )

    async def completed(_claim):
        return "READY"

    monkeypatch.setattr(worker, "_process_claim", completed)
    message = DocumentIngestionMessage.build(
        document_id=7,
        user_id=11,
        dataset_id=9,
        document_version=2,
    )

    await worker._handle_message(message.serialize(), {})

    assert queue.claimed_ids == [(7, "rabbit-worker")]


@pytest.mark.asyncio
async def test_worker_schedules_backoff_after_ingestion_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(worker_module.settings, "PARSE_TEMP_DIR", str(tmp_path))
    document = _document()
    queue = _FakeQueue()
    ingestion = _FakeIngestion(RuntimeError("provider unavailable"))
    worker = DocumentParseWorker(
        queue=queue,
        ingestion=ingestion,
        storage=_FakeStorage(),
        session_context_factory=_session_factory(document),
        heartbeat_interval=3600,
    )
    claim = DocumentClaim(7, "token", 1, False, False)

    await worker._process_claim(claim)

    assert len(queue.failures) == 1
    failed_claim, error, retryable, metadata = queue.failures[0]
    assert failed_claim == claim
    assert str(error) == "provider unavailable"
    assert retryable is True
    assert metadata == {
        "error_code": None,
        "parse_quality_status": None,
        "parse_quality": None,
    }


@pytest.mark.asyncio
async def test_worker_preserves_quality_failure_inside_cleanup_exception_group(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(worker_module.settings, "PARSE_TEMP_DIR", str(tmp_path))
    document = _document()
    queue = _FakeQueue()
    quality_error = DocumentQualityGateError(
        quality_status="OCR_REQUIRED",
        quality_report={"status": "OCR_REQUIRED", "ocr_required_pages": [1]},
        error_code="PDF_OCR_REQUIRED",
        message="OCR 尚未完成",
        retryable=False,
    )
    ingestion = _FakeIngestion(
        ExceptionGroup(
            "quality gate plus cleanup failure",
            [quality_error, RuntimeError("cleanup failed")],
        )
    )
    worker = DocumentParseWorker(
        queue=queue,
        ingestion=ingestion,
        storage=_FakeStorage(),
        session_context_factory=_session_factory(document),
        heartbeat_interval=3600,
    )

    await worker._process_claim(DocumentClaim(7, "token", 1, False, False))

    assert len(queue.failures) == 1
    _claim, error, retryable, metadata = queue.failures[0]
    assert error is quality_error
    assert retryable is False
    assert metadata == {
        "error_code": "PDF_OCR_REQUIRED",
        "parse_quality_status": "OCR_REQUIRED",
        "parse_quality": {
            "status": "OCR_REQUIRED",
            "ocr_required_pages": [1],
        },
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"heartbeat_interval": 0},
        {"heartbeat_interval": 60, "queue": None},
    ],
)
def test_worker_rejects_invalid_runtime_limits(overrides) -> None:
    worker_overrides = dict(overrides)
    queue = worker_overrides.pop("queue", _FakeQueue())
    if queue is None:
        queue = _FakeQueue()
        queue.lease_seconds = 60
    with pytest.raises(ValueError):
        DocumentParseWorker(
            queue=queue,
            ingestion=_FakeIngestion(),
            storage=_FakeStorage(),
            session_context_factory=_session_factory(_document()),
            **worker_overrides,
        )
