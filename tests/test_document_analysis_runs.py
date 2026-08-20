from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

import app.services.document_analysis_runs as runs_module
from app.domain.models import Dataset, Document
from app.rag.core.llm.response import UsageInfo
from app.services.document_analysis import DocumentAnalysisResult
from app.services.document_analysis_runs import DocumentAnalysisRunManager


def _document() -> Document:
    return Document(
        id=7,
        dataset_id=3,
        user_id=11,
        filename="报告.pdf",
        file_type="pdf",
        file_size=100,
        raw_bucket="raw",
        raw_object_key="raw/report.pdf",
        parsed_bucket="private",
        parsed_object_key="parsed/11/3/7/versions/v2/report.md",
        status="READY",
        version=2,
    )


def _dataset() -> Dataset:
    return Dataset(
        id=3,
        user_id=11,
        name="企业资料",
        status="ACTIVE",
        dense_embedding_config_id=1,
        sparse_embedding_config_id=2,
        chat_config_id=3,
    )


class _Session:
    def __init__(self, values):
        self.values = deque(values)

    async def scalar(self, _statement):
        return self.values.popleft()


@pytest.mark.asyncio
async def test_background_run_is_deduplicated_and_completes_after_start_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document()
    release_analysis = asyncio.Event()
    saved = []

    @asynccontextmanager
    async def fake_db_context():
        yield _Session([document, _dataset(), document])

    class Service:
        async def analyze(self, **_kwargs):
            await release_analysis.wait()
            return DocumentAnalysisResult(
                markdown="# 企业文档分析报告\n",
                sources=[],
                model_name="chat-model",
                model_config_id=3,
                usage=UsageInfo(),
                analyzed_chunk_count=8,
                evidence_batch_count=2,
                generated_at=datetime(2026, 8, 20, tzinfo=UTC),
            )

    class Store:
        async def save(self, **kwargs):
            saved.append(kwargs)

    monkeypatch.setattr(runs_module, "get_db_context", fake_db_context)
    monkeypatch.setattr(runs_module, "DocumentAnalysisService", Service)
    monkeypatch.setattr(runs_module, "DocumentAnalysisStore", Store)

    manager = DocumentAnalysisRunManager()
    first = await manager.start(document=document, llm_config_id=None)
    second = await manager.start(document=document, llm_config_id=None)

    assert first is second
    assert first.state in {"PENDING", "RUNNING"}
    assert len(manager._tasks) == 1

    task = next(iter(manager._tasks))
    await asyncio.sleep(0)
    assert first.state == "RUNNING"
    release_analysis.set()
    await task

    status = await manager.get_status(document=document)
    assert status.state == "SUCCEEDED"
    assert status.stage == "COMPLETED"
    assert status.finished_at is not None
    assert len(saved) == 1
