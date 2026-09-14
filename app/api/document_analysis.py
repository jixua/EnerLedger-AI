"""企业文档大模型分析 API。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import get_user_id
from app.domain.models import Dataset, Document
from app.rag.core.llm.response import UsageInfo
from app.rag.database import get_db
from app.services.document_analysis import (
    DocumentAnalysisError,
    DocumentAnalysisNotFoundError,
    DocumentAnalysisResult,
    DocumentAnalysisStore,
    DocumentAnalysisSummary,
)
from app.services.document_analysis_docx import build_document_analysis_docx
from app.services.document_analysis_runs import document_analysis_run_manager
from app.services.document_queue import DOCUMENT_STATUS_READY

router = APIRouter(prefix="/api/v1/documents", tags=["文档分析"])
report_index_router = APIRouter(prefix="/api/v1/analysis-reports", tags=["文档分析"])

_REPORT_INDEX_CONCURRENCY = 12


class DocumentAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_config_id: int | None = Field(default=None, gt=0)


class DocumentAnalysisSourceRead(BaseModel):
    citation_index: int
    chunk_id: str
    chunk_index: int
    chunk_type: str
    page: int | None
    page_range: dict[str, int] | None
    excerpt: str


class DocumentAnalysisResponse(BaseModel):
    document_id: int
    dataset_id: int
    document_version: int
    filename: str
    markdown: str
    sources: list[DocumentAnalysisSourceRead]
    model_name: str
    model_config_id: int
    usage: UsageInfo
    analyzed_chunk_count: int
    evidence_batch_count: int
    generated_at: datetime


class DocumentAnalysisRunResponse(BaseModel):
    run_id: str | None
    document_id: int
    dataset_id: int
    document_version: int
    state: str
    stage: str
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None


class DocumentAnalysisReportListItem(BaseModel):
    document_id: int
    dataset_id: int
    dataset_name: str
    document_version: int
    filename: str
    model_name: str
    model_config_id: int
    analyzed_chunk_count: int
    evidence_batch_count: int
    source_count: int
    generated_at: datetime


class DocumentAnalysisReportListResponse(BaseModel):
    items: list[DocumentAnalysisReportListItem]
    total: int
    unavailable_count: int


def _analysis_response(document: Document, result: DocumentAnalysisResult) -> dict:
    return {
        "document_id": document.id,
        "dataset_id": document.dataset_id,
        "document_version": document.version,
        "filename": document.filename,
        "markdown": result.markdown,
        "sources": [source.to_dict() for source in result.sources],
        "model_name": result.model_name,
        "model_config_id": result.model_config_id,
        "usage": result.usage,
        "analyzed_chunk_count": result.analyzed_chunk_count,
        "evidence_batch_count": result.evidence_batch_count,
        "generated_at": result.generated_at,
    }


async def _ready_document(document_id: int, user_id: int, db: AsyncSession) -> Document:
    document = await db.scalar(
        select(Document).where(Document.id == document_id, Document.user_id == user_id)
    )
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if document.status != DOCUMENT_STATUS_READY:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DOCUMENT_ANALYSIS_NOT_READY",
                "message": "文档当前版本尚未解析完成，暂时不能读取或生成分析报告",
                "status": document.status,
            },
        )
    return document


async def _load_report_list_item(
    *,
    document: Document,
    dataset_name: str,
    store: DocumentAnalysisStore,
    semaphore: asyncio.Semaphore,
) -> tuple[DocumentAnalysisReportListItem | None, bool]:
    async with semaphore:
        try:
            summary: DocumentAnalysisSummary = await store.load_summary(document=document)
        except DocumentAnalysisNotFoundError:
            return None, False
        except DocumentAnalysisError:
            return None, True
    return (
        DocumentAnalysisReportListItem(
            document_id=int(document.id),
            dataset_id=int(document.dataset_id),
            dataset_name=dataset_name,
            document_version=int(document.version),
            filename=document.filename,
            model_name=summary.model_name,
            model_config_id=summary.model_config_id,
            analyzed_chunk_count=summary.analyzed_chunk_count,
            evidence_batch_count=summary.evidence_batch_count,
            source_count=summary.source_count,
            generated_at=summary.generated_at,
        ),
        False,
    )


@report_index_router.get("", response_model=DocumentAnalysisReportListResponse)
async def list_document_analysis_reports(
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> DocumentAnalysisReportListResponse:
    """汇总当前用户仍可读取的文档分析报告。"""

    rows = (
        await db.execute(
            select(Document, Dataset.name)
            .join(Dataset, Dataset.id == Document.dataset_id)
            .where(
                Document.user_id == user_id,
                Document.status == DOCUMENT_STATUS_READY,
                Dataset.user_id == user_id,
                Dataset.status == "ACTIVE",
            )
            .order_by(Document.id.asc())
        )
    ).all()
    store = DocumentAnalysisStore()
    semaphore = asyncio.Semaphore(_REPORT_INDEX_CONCURRENCY)
    loaded = await asyncio.gather(
        *(
            _load_report_list_item(
                document=document,
                dataset_name=dataset_name,
                store=store,
                semaphore=semaphore,
            )
            for document, dataset_name in rows
        )
    )
    items = [item for item, _ in loaded if item is not None]
    unavailable_count = sum(1 for _, unavailable in loaded if unavailable)
    if unavailable_count and not items:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "DOCUMENT_ANALYSIS_INDEX_UNAVAILABLE",
                "message": "暂时无法读取分析报告清单，请稍后重试",
            },
        )
    items.sort(key=lambda item: item.generated_at.timestamp(), reverse=True)
    return DocumentAnalysisReportListResponse(
        items=items,
        total=len(items),
        unavailable_count=unavailable_count,
    )


@router.get("/{document_id}/analysis", response_model=DocumentAnalysisResponse)
async def get_document_analysis(
    document_id: int,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """从 MinIO 读取当前 READY 文档版本最近一次成功保存的分析报告。"""

    document = await _ready_document(document_id, user_id, db)
    try:
        result = await DocumentAnalysisStore().load(document=document)
    except DocumentAnalysisError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return _analysis_response(document, result)


@router.get("/{document_id}/analysis/docx")
async def download_document_analysis_docx(
    document_id: int,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """将 MinIO 中当前文档版本的已保存报告导出为 DOCX。"""

    document = await _ready_document(document_id, user_id, db)
    try:
        result = await DocumentAnalysisStore().load(document=document)
        content = build_document_analysis_docx(document=document, result=result)
    except DocumentAnalysisError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    stem = Path(document.filename).stem.strip() or "企业文档"
    filename = f"{stem}-分析报告.docx"
    encoded_filename = quote(filename, safe="")
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": (
                f"attachment; filename=analysis-report.docx; filename*=UTF-8''{encoded_filename}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{document_id}/analysis/status", response_model=DocumentAnalysisRunResponse)
async def get_document_analysis_status(
    document_id: int,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """查询当前文档版本的后台分析运行状态。"""

    document = await _ready_document(document_id, user_id, db)
    run = await document_analysis_run_manager.get_status(document=document)
    return run.to_dict()


@router.post("/{document_id}/analysis", response_model=DocumentAnalysisRunResponse, status_code=202)
async def analyze_document(
    document_id: int,
    payload: DocumentAnalysisRequest,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """启动独立后台分析；重复请求复用当前文档版本仍在运行的任务。"""

    document = await _ready_document(document_id, user_id, db)
    run = await document_analysis_run_manager.start(
        document=document,
        llm_config_id=payload.llm_config_id,
    )
    return run.to_dict()
