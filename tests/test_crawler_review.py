from __future__ import annotations

from collections import deque
from datetime import datetime

import pytest
from fastapi import HTTPException, Response

from app.api import crawler as crawler_api
from app.domain.models import Document
from app.domain.schemas import CrawlerReviewRequest
from app.services.document_queue import utc_now


class _FakeSession:
    def __init__(self, scalar_values=()):
        self.scalar_values = deque(scalar_values)
        self.commits = 0
        self.refreshes = 0

    async def scalar(self, _statement):
        return self.scalar_values.popleft() if self.scalar_values else None

    async def commit(self):
        self.commits += 1

    async def refresh(self, _value):
        self.refreshes += 1


def _pending_submission() -> Document:
    now = utc_now()
    return Document(
        id=51,
        dataset_id=7,
        user_id=1,
        filename="crawler-paper.pdf",
        file_type="pdf",
        file_size=123,
        content_type="application/pdf",
        raw_bucket="raw",
        raw_object_key="raw/1/7/file.pdf",
        parser_backend="opendataloader",
        status="PENDING_REVIEW",
        version=1,
        attempt_count=0,
        dispatch_status="IDLE",
        dispatch_attempt_count=0,
        source_type="EXTERNAL_CRAWLER",
        source_url="https://example.com/paper",
        source_title="Crawler paper",
        source_metadata={"crawler_name": "example"},
        review_status="PENDING",
        created_at=now,
        updated_at=now,
    )


def test_source_metadata_requires_an_object_and_keeps_trusted_crawler_name() -> None:
    assert crawler_api._parse_source_metadata('{"lang":"zh","crawler_name":"spoofed"}', "real") == {
        "lang": "zh",
        "crawler_name": "real",
    }
    with pytest.raises(HTTPException, match="metadata"):
        crawler_api._parse_source_metadata("[]", "real")


def test_crawler_upload_key_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(crawler_api.settings, "CRAWLER_UPLOAD_API_KEY", "")
    with pytest.raises(HTTPException) as disabled:
        crawler_api.require_crawler_api_key("anything")
    assert disabled.value.status_code == 503

    monkeypatch.setattr(crawler_api.settings, "CRAWLER_UPLOAD_API_KEY", "crawler-secret")
    with pytest.raises(HTTPException) as invalid:
        crawler_api.require_crawler_api_key("wrong")
    assert invalid.value.status_code == 401
    assert crawler_api.require_crawler_api_key("crawler-secret") is None


@pytest.mark.asyncio
async def test_approval_is_the_only_transition_that_dispatches_parse(monkeypatch) -> None:
    document = _pending_submission()
    db = _FakeSession([document, "能碳论文"])
    response = Response()
    dispatched: list[int] = []

    async def fake_dispatch(_db, value):
        dispatched.append(value.id)

    monkeypatch.setattr(crawler_api, "_dispatch_document", fake_dispatch)
    result = await crawler_api.review_crawler_submission(
        document_id=51,
        payload=CrawlerReviewRequest(decision="APPROVED"),
        response=response,
        user_id=1,
        db=db,
    )

    assert response.status_code == 202
    assert response.headers["Location"] == "/api/v1/documents/51"
    assert result["review_status"] == "APPROVED"
    assert document.status == "QUEUED"
    assert document.dispatch_status == "PENDING"
    assert isinstance(document.reviewed_at, datetime)
    assert dispatched == [51]


@pytest.mark.asyncio
async def test_rejection_keeps_the_file_out_of_the_parse_outbox(monkeypatch) -> None:
    document = _pending_submission()
    db = _FakeSession([document, "能碳论文"])
    response = Response()

    async def unexpected_dispatch(_db, _value):
        raise AssertionError("rejected submissions must not be dispatched")

    monkeypatch.setattr(crawler_api, "_dispatch_document", unexpected_dispatch)
    result = await crawler_api.review_crawler_submission(
        document_id=51,
        payload=CrawlerReviewRequest(decision="REJECTED", note="来源不可靠"),
        response=response,
        user_id=1,
        db=db,
    )

    assert response.status_code == 200
    assert result["review_status"] == "REJECTED"
    assert result["review_note"] == "来源不可靠"
    assert document.status == "REJECTED"
    assert document.dispatch_status == "IDLE"


@pytest.mark.asyncio
async def test_review_cannot_be_repeated() -> None:
    document = _pending_submission()
    document.review_status = "APPROVED"
    document.status = "QUEUED"
    db = _FakeSession([document])

    with pytest.raises(HTTPException) as exc_info:
        await crawler_api.review_crawler_submission(
            document_id=51,
            payload=CrawlerReviewRequest(decision="REJECTED"),
            response=Response(),
            user_id=1,
            db=db,
        )

    assert exc_info.value.status_code == 409
