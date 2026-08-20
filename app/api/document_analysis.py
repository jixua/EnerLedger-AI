"""企业文档大模型分析 API。"""

from __future__ import annotations

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
from app.domain.models import Document
from app.rag.core.llm.response import UsageInfo
from app.rag.database import get_db
from app.services.document_analysis import (
    DocumentAnalysisError,
    DocumentAnalysisResult,
    DocumentAnalysisStore,
)
from app.services.document_analysis_docx import build_document_analysis_docx
from app.services.document_analysis_runs import document_analysis_run_manager
from app.services.document_queue import DOCUMENT_STATUS_READY

router = APIRouter(prefix="/api/v1/documents", tags=["文档分析"])


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
