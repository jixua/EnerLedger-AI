"""在线资料采集与数据集导入 API。"""

import asyncio
import re
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.documents import _owned_dataset, queue_document_from_path
from app.domain.auth import get_user_id
from app.domain.schemas import (
    ArxivImportItem,
    ArxivImportRequest,
    ArxivImportResponse,
    ArxivSearchResponse,
)
from app.rag.config import settings
from app.rag.database import get_db
from app.rag.observability.logging import logger
from app.services.arxiv_crawler import ArxivCrawlerError, arxiv_crawler

router = APIRouter(prefix="/api/v1/crawler", tags=["资料采集"])


def _paper_filename(title: str) -> str:
    """把论文标题转换成可安全用于对象存储和下载的 PDF 文件名。"""

    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", title)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    normalized = re.sub(r"\.pdf$", "", normalized, flags=re.IGNORECASE).strip(" .")
    safe_title = normalized[:251].rstrip(" .") or "未命名论文"
    return f"{safe_title}.pdf"


@router.get("/arxiv", response_model=ArxivSearchResponse)
async def search_arxiv_papers(
    query: Annotated[str, Query(min_length=2, max_length=120)],
    max_results: Annotated[int, Query(ge=1, le=20)] = 10,
    _: int = Depends(get_user_id),
) -> ArxivSearchResponse:
    """按关键词采集 arXiv 最新论文的描述性元数据。"""

    try:
        return await arxiv_crawler.search(query, max_results=max_results)
    except ArxivCrawlerError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "ARXIV_UPSTREAM_ERROR", "message": str(exc)},
        ) from exc


@router.post(
    "/arxiv/import",
    response_model=ArxivImportResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_arxiv_papers(
    payload: ArxivImportRequest,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> ArxivImportResponse:
    """依次下载所选论文，并复用普通文档上传的 MinIO 与解析队列链路。"""

    await _owned_dataset(db, payload.dataset_id, user_id)
    parse_temp_root = Path(settings.PARSE_TEMP_DIR)
    await asyncio.to_thread(parse_temp_root.mkdir, parents=True, exist_ok=True)
    items: list[ArxivImportItem] = []

    with tempfile.TemporaryDirectory(
        prefix="energy-carbon-arxiv-",
        dir=parse_temp_root,
    ) as temp_dir:
        for index, paper in enumerate(payload.papers):
            arxiv_id = paper.arxiv_id
            filename = _paper_filename(paper.title)
            source_path = Path(temp_dir) / f"paper-{index}.pdf"
            try:
                await arxiv_crawler.download_pdf(
                    arxiv_id,
                    source_path,
                    max_bytes=settings.DOCUMENT_UPLOAD_MAX_BYTES,
                )
                document = await queue_document_from_path(
                    dataset_id=payload.dataset_id,
                    user_id=user_id,
                    filename=filename,
                    source_path=source_path,
                    content_type="application/pdf",
                    db=db,
                    ownership_checked=True,
                )
                items.append(
                    ArxivImportItem(
                        arxiv_id=arxiv_id,
                        status="QUEUED",
                        document_id=document.id,
                        filename=filename,
                    )
                )
            except ArxivCrawlerError as exc:
                items.append(
                    ArxivImportItem(
                        arxiv_id=arxiv_id,
                        status="FAILED",
                        filename=filename,
                        message=str(exc),
                    )
                )
            except HTTPException as exc:
                message = exc.detail if isinstance(exc.detail, str) else "导入论文失败"
                items.append(
                    ArxivImportItem(
                        arxiv_id=arxiv_id,
                        status="FAILED",
                        filename=filename,
                        message=message,
                    )
                )
            except Exception as exc:
                await db.rollback()
                logger.bind(
                    event="arxiv_import_failed",
                    arxiv_id=arxiv_id,
                    dataset_id=payload.dataset_id,
                    error_type=type(exc).__name__,
                ).exception("arXiv 论文导入失败")
                items.append(
                    ArxivImportItem(
                        arxiv_id=arxiv_id,
                        status="FAILED",
                        filename=filename,
                        message="导入失败，请稍后重试",
                    )
                )
            finally:
                source_path.unlink(missing_ok=True)

    queued_count = sum(item.status == "QUEUED" for item in items)
    return ArxivImportResponse(
        dataset_id=payload.dataset_id,
        queued_count=queued_count,
        failed_count=len(items) - queued_count,
        items=items,
    )
