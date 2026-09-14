from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

import app.api.document_analysis as api_module
from app.api.document_analysis import (
    DocumentAnalysisRequest,
    analyze_document,
    download_document_analysis_docx,
    get_document_analysis,
    get_document_analysis_status,
    list_document_analysis_reports,
)
from app.domain.models import Document
from app.rag.core.llm.response import UsageInfo
from app.services.document_analysis import (
    DocumentAnalysisNotFoundError,
    DocumentAnalysisResult,
    DocumentAnalysisStorageError,
    DocumentAnalysisSummary,
)


class _Session:
    def __init__(self, scalar_values):
        self.scalar_values = deque(scalar_values)

    async def scalar(self, _statement):
        return self.scalar_values.popleft()


class _Rows:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _ListSession:
    def __init__(self, values):
        self.values = values

    async def execute(self, _statement):
        return _Rows(self.values)


def _document(status: str = "READY") -> Document:
    return Document(
        id=7,
        dataset_id=3,
        user_id=11,
        filename="报告.docx",
        file_type="docx",
        file_size=100,
        raw_bucket="raw",
        raw_object_key="raw/report.docx",
        parsed_bucket="private",
        parsed_object_key="parsed/11/3/7/versions/v2/attempt-1/report.md",
        status=status,
        version=2,
    )


@pytest.mark.asyncio
async def test_analysis_api_starts_background_run_and_returns_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class Run:
        def to_dict(self):
            return {
                "run_id": "run-1",
                "document_id": 7,
                "dataset_id": 3,
                "document_version": 2,
                "state": "PENDING",
                "stage": "WAITING",
                "started_at": None,
                "finished_at": None,
                "error_code": None,
                "error_message": None,
            }

    class Manager:
        async def start(self, **kwargs):
            calls.append(kwargs)
            return Run()

    monkeypatch.setattr(api_module, "document_analysis_run_manager", Manager())
    document = _document()
    response = await analyze_document(
        document_id=7,
        payload=DocumentAnalysisRequest(),
        user_id=11,
        db=_Session([document]),
    )

    assert response["state"] == "PENDING"
    assert response["document_version"] == 2
    assert calls == [{"document": document, "llm_config_id": None}]


@pytest.mark.asyncio
async def test_analysis_status_api_returns_current_background_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Run:
        def to_dict(self):
            return {
                "run_id": "run-1",
                "document_id": 7,
                "dataset_id": 3,
                "document_version": 2,
                "state": "RUNNING",
                "stage": "ANALYZING",
                "started_at": datetime(2026, 8, 20, tzinfo=UTC),
                "finished_at": None,
                "error_code": None,
                "error_message": None,
            }

    class Manager:
        async def get_status(self, **_kwargs):
            return Run()

    monkeypatch.setattr(api_module, "document_analysis_run_manager", Manager())
    response = await get_document_analysis_status(
        document_id=7,
        user_id=11,
        db=_Session([_document()]),
    )

    assert response["state"] == "RUNNING"
    assert response["stage"] == "ANALYZING"


@pytest.mark.asyncio
async def test_analysis_api_reads_persisted_report(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted = DocumentAnalysisResult(
        markdown="# 企业文档分析报告\n",
        sources=[],
        model_name="chat-model",
        model_config_id=3,
        usage=UsageInfo(),
        analyzed_chunk_count=4,
        evidence_batch_count=1,
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )

    class Store:
        async def load(self, **_kwargs):
            return persisted

    monkeypatch.setattr(api_module, "DocumentAnalysisStore", Store)
    response = await get_document_analysis(
        document_id=7,
        user_id=11,
        db=_Session([_document()]),
    )

    assert response["markdown"] == persisted.markdown
    assert response["generated_at"] == persisted.generated_at


@pytest.mark.asyncio
async def test_analysis_api_downloads_persisted_report_as_docx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted = DocumentAnalysisResult(
        markdown="# 企业文档分析报告\n",
        sources=[],
        model_name="chat-model",
        model_config_id=3,
        usage=UsageInfo(),
        analyzed_chunk_count=4,
        evidence_batch_count=1,
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )

    class Store:
        async def load(self, **_kwargs):
            return persisted

    monkeypatch.setattr(api_module, "DocumentAnalysisStore", Store)
    monkeypatch.setattr(api_module, "build_document_analysis_docx", lambda **_kwargs: b"PK-docx")
    response = await download_document_analysis_docx(
        document_id=7,
        user_id=11,
        db=_Session([_document()]),
    )

    body = b"".join([chunk async for chunk in response.body_iterator])
    assert body == b"PK-docx"
    assert response.media_type.endswith("wordprocessingml.document")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    disposition = response.headers["content-disposition"]
    assert "%E6%8A%A5%E5%91%8A-%E5%88%86%E6%9E%90%E6%8A%A5%E5%91%8A.docx" in disposition


@pytest.mark.asyncio
async def test_analysis_api_rejects_document_before_ready() -> None:
    with pytest.raises(HTTPException) as raised:
        await analyze_document(
            document_id=7,
            payload=DocumentAnalysisRequest(),
            user_id=11,
            db=_Session([_document("PROCESSING")]),
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "DOCUMENT_ANALYSIS_NOT_READY"


@pytest.mark.asyncio
async def test_analysis_report_index_lists_saved_reports_and_counts_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated_at = datetime(2026, 8, 20, tzinfo=UTC)
    documents = [_document(), _document(), _document()]
    for document_id, document in enumerate(documents, start=7):
        document.id = document_id

    class Store:
        async def load_summary(self, *, document):
            if document.id == 8:
                raise DocumentAnalysisNotFoundError("not generated")
            if document.id == 9:
                raise DocumentAnalysisStorageError("broken manifest")
            return DocumentAnalysisSummary(
                model_name="chat-model",
                model_config_id=3,
                analyzed_chunk_count=4,
                evidence_batch_count=1,
                source_count=2,
                generated_at=generated_at,
            )

    monkeypatch.setattr(api_module, "DocumentAnalysisStore", Store)
    response = await list_document_analysis_reports(
        user_id=11,
        db=_ListSession([(document, "企业材料") for document in documents]),
    )

    assert response.total == 1
    assert response.unavailable_count == 1
    assert response.items[0].document_id == 7
    assert response.items[0].dataset_name == "企业材料"
    assert response.items[0].source_count == 2
