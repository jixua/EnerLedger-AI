"""在线资料采集与数据集导入 API。"""

import asyncio
import json
import re
import tempfile
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.api.documents import (
    _dispatch_document,
    _owned_dataset,
    _save_upload_to_path,
    _validate_word_file_signature,
    queue_document_from_path,
)
from app.domain.auth import (
    ADMIN_USER_ID,
    get_actor_user_id,
    get_shared_owner_user_id,
    get_user_id,
    require_crawler_api_key,
)
from app.domain.models import Dataset, Document
from app.domain.schemas import (
    ArxivImportItem,
    ArxivImportRequest,
    ArxivImportResponse,
    ArxivSearchResponse,
    CrawlerReviewRequest,
    CrawlerSubmissionPage,
    CrawlerSubmissionRead,
)
from app.rag.config import settings
from app.rag.core.llm.provider_lifecycle import aclose_resolved_models
from app.rag.core.llm.user_model_resolver import aresolve_model
from app.rag.database import get_db
from app.rag.observability.logging import logger
from app.rag.services.storage.factory import StorageFactory
from app.services.arxiv_crawler import ArxivCrawlerError, arxiv_crawler
from app.services.arxiv_query_optimizer import (
    optimize_arxiv_query_with_ai,
    rule_based_arxiv_query,
    rule_based_fallback_warning,
)
from app.services.document_queue import (
    DOCUMENT_STATUS_PENDING_REVIEW,
    DOCUMENT_STATUS_REJECTED,
    reset_document_for_queue,
    utc_now,
)

router = APIRouter(prefix="/api/v1/crawler", tags=["资料采集"])


def _paper_filename(title: str) -> str:
    """把论文标题转换成可安全用于对象存储和下载的 PDF 文件名。"""

    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", title)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    normalized = re.sub(r"\.pdf$", "", normalized, flags=re.IGNORECASE).strip(" .")
    safe_title = normalized[:251].rstrip(" .") or "未命名论文"
    return f"{safe_title}.pdf"


def _submission_payload(document: Document, dataset_name: str) -> dict:
    return {
        "document_id": document.id,
        "dataset_id": document.dataset_id,
        "dataset_name": dataset_name,
        "filename": document.filename,
        "file_type": document.file_type,
        "file_size": document.file_size,
        "content_type": document.content_type,
        "document_status": document.status,
        "source_type": document.source_type,
        "source_url": document.source_url,
        "source_title": document.source_title,
        "source_metadata": document.source_metadata,
        "review_status": document.review_status,
        "review_note": document.review_note,
        "reviewed_at": document.reviewed_at,
        "created_at": document.created_at,
    }


def _normalize_source_url(value: str | None) -> str | None:
    normalized = value.strip() if value else ""
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="source_url 必须是有效的 HTTP(S) 地址")
    return normalized


def _parse_source_metadata(value: str | None, crawler_name: str) -> dict[str, object]:
    metadata: dict[str, object] = {"crawler_name": crawler_name}
    if not value:
        return metadata
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="metadata 必须是有效的 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="metadata 必须是 JSON 对象")
    metadata.update(parsed)
    metadata["crawler_name"] = crawler_name
    return metadata


@router.post(
    "/uploads",
    response_model=CrawlerSubmissionRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_crawler_submission(
    dataset_id: Annotated[int, Form(gt=0)],
    file: Annotated[UploadFile, File()],
    source_url: Annotated[str | None, Form(max_length=1024)] = None,
    title: Annotated[str | None, Form(max_length=512)] = None,
    crawler_name: Annotated[str, Form(min_length=1, max_length=64)] = "external-crawler",
    metadata: Annotated[str | None, Form(max_length=16384)] = None,
    _: None = Depends(require_crawler_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive a crawler file into MinIO without dispatching a parse task."""

    dataset = await _owned_dataset(db, dataset_id, ADMIN_USER_ID)
    normalized_crawler_name = crawler_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", normalized_crawler_name):
        raise HTTPException(
            status_code=422,
            detail="crawler_name 只能包含字母、数字、点、下划线和连字符",
        )
    normalized_title = (title.strip() or None) if title else None
    normalized_source_url = _normalize_source_url(source_url)
    source_metadata = _parse_source_metadata(metadata, normalized_crawler_name)
    filename = re.split(r"[/\\]", file.filename or "")[-1]
    file_type = Path(filename).suffix.lower().lstrip(".")
    parse_temp_root = Path(settings.PARSE_TEMP_DIR)
    await asyncio.to_thread(parse_temp_root.mkdir, parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="energy-carbon-crawler-upload-",
        dir=parse_temp_root,
    ) as temp_dir:
        source_path = Path(temp_dir) / f"source.{file_type or 'bin'}"
        await _save_upload_to_path(file, source_path)
        _validate_word_file_signature(source_path, file_type)
        document = await queue_document_from_path(
            dataset_id=dataset_id,
            user_id=ADMIN_USER_ID,
            filename=filename,
            source_path=source_path,
            content_type=file.content_type or "application/octet-stream",
            db=db,
            ownership_checked=True,
            review_required=True,
            source_type="EXTERNAL_CRAWLER",
            source_url=normalized_source_url,
            source_title=normalized_title,
            source_metadata=source_metadata,
        )

    return _submission_payload(document, dataset.name)


@router.get("/submissions", response_model=CrawlerSubmissionPage)
async def list_crawler_submissions(
    review_status: Literal["PENDING", "APPROVED", "REJECTED"] | None = Query(default="PENDING"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    user_id: int = Depends(get_shared_owner_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    filters = [
        Document.user_id == user_id,
        Document.source_type == "EXTERNAL_CRAWLER",
    ]
    if review_status is not None:
        filters.append(Document.review_status == review_status)
    total = int(
        await db.scalar(select(func.count()).select_from(Document).where(*filters)) or 0
    )
    rows = (
        await db.execute(
            select(Document, Dataset.name)
            .join(Dataset, Dataset.id == Document.dataset_id)
            .where(*filters)
            .order_by(Document.created_at.desc(), Document.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return {
        "items": [_submission_payload(document, dataset_name) for document, dataset_name in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/submissions/{document_id}/file")
async def download_crawler_submission_file(
    document_id: int,
    user_id: int = Depends(get_shared_owner_user_id),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    document = await db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.user_id == user_id,
            Document.source_type == "EXTERNAL_CRAWLER",
        )
    )
    if document is None:
        raise HTTPException(status_code=404, detail="待审核资料不存在")

    parse_temp_root = Path(settings.PARSE_TEMP_DIR)
    await asyncio.to_thread(parse_temp_root.mkdir, parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        prefix="energy-carbon-review-",
        suffix=f".{document.file_type}",
        dir=parse_temp_root,
        delete=False,
    )
    temporary_path = Path(temporary.name)
    temporary.close()
    try:
        await asyncio.to_thread(
            StorageFactory.get_storage().download_to_path,
            document.raw_bucket,
            document.raw_object_key,
            temporary_path,
        )
    except Exception as exc:
        temporary_path.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail="读取待审核原文件失败") from exc
    return FileResponse(
        temporary_path,
        media_type=document.content_type or "application/octet-stream",
        filename=document.filename,
        background=BackgroundTask(temporary_path.unlink, missing_ok=True),
    )


@router.post(
    "/submissions/{document_id}/review",
    response_model=CrawlerSubmissionRead,
)
async def review_crawler_submission(
    document_id: int,
    payload: CrawlerReviewRequest,
    response: Response,
    user_id: int = Depends(get_shared_owner_user_id),
    actor_user_id: int = Depends(get_actor_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    document = await db.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.user_id == user_id,
            Document.source_type == "EXTERNAL_CRAWLER",
        )
        .with_for_update()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="待审核资料不存在")
    if document.review_status != "PENDING" or document.status != DOCUMENT_STATUS_PENDING_REVIEW:
        raise HTTPException(
            status_code=409,
            detail={"code": "SUBMISSION_ALREADY_REVIEWED", "message": "该资料已完成审核"},
        )

    target_dataset_id = (
        payload.dataset_id if payload.decision == "APPROVED" else document.dataset_id
    )
    dataset = await _owned_dataset(db, target_dataset_id, user_id)

    document.review_status = payload.decision
    document.review_note = payload.note
    document.reviewed_by_user_id = actor_user_id
    document.reviewed_at = utc_now()
    if payload.decision == "APPROVED":
        document.dataset_id = dataset.id
        document.folder_id = None
        reset_document_for_queue(document, reparse=False)
        response.status_code = status.HTTP_202_ACCEPTED
    else:
        document.status = DOCUMENT_STATUS_REJECTED
        document.dispatch_status = "IDLE"
        document.dispatch_available_at = None
        document.dispatch_lease_token = None
        document.dispatch_lease_expires_at = None
        document.dispatch_error = None

    await db.commit()
    await db.refresh(document)
    if payload.decision == "APPROVED":
        await _dispatch_document(db, document)
        response.headers["Location"] = f"/api/v1/documents/{document.id}"
    return _submission_payload(document, dataset.name)


@router.get("/arxiv", response_model=ArxivSearchResponse)
async def search_arxiv_papers(
    query: Annotated[str, Query(min_length=2, max_length=120)],
    dataset_id: Annotated[int, Query(gt=0)],
    max_results: Annotated[int, Query(ge=1, le=20)] = 10,
    ai_optimize: bool = True,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> ArxivSearchResponse:
    """用数据集 Chat 模型优化自然语言主题，再采集 arXiv 论文元数据。"""

    try:
        dataset = await _owned_dataset(db, dataset_id, user_id)
        optimization = rule_based_arxiv_query(query)
        if ai_optimize and dataset.chat_config_id is None:
            optimization = rule_based_arxiv_query(
                query,
                warning=rule_based_fallback_warning(
                    query,
                    reason="目标数据集未绑定对话模型",
                ),
            )
        elif ai_optimize and dataset.chat_config_id is not None:
            resolved = None
            try:
                resolved = await aresolve_model(
                    user_id=user_id,
                    config_id=int(dataset.chat_config_id),
                    capability="CHAT",
                    db=db,
                )
                optimization = await optimize_arxiv_query_with_ai(
                    query,
                    provider=resolved.provider,
                    model_name=resolved.model_name,
                )
            except Exception as exc:  # noqa: BLE001 - AI 优化失败不阻断基础检索
                logger.bind(
                    event="arxiv_query_optimization_failed",
                    dataset_id=dataset_id,
                    llm_config_id=dataset.chat_config_id,
                    error_type=type(exc).__name__,
                ).warning(
                    "arXiv AI 检索词优化失败，回退规则检索，原因={}",
                    type(exc).__name__,
                )
                optimization = rule_based_arxiv_query(
                    query,
                    warning=rule_based_fallback_warning(
                        query,
                        reason="AI 检索词优化失败",
                    ),
                )
            finally:
                await aclose_resolved_models([resolved])

        return await arxiv_crawler.search(
            query,
            max_results=max_results,
            search_query=optimization.search_query,
            optimized_query=optimization.optimized_query,
            optimization_mode=optimization.mode,
            optimization_model=optimization.model_name,
            optimization_warning=optimization.warning,
        )
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
                    source_type="ARXIV",
                    source_url=f"https://arxiv.org/abs/{arxiv_id}",
                    source_title=paper.title,
                    source_metadata={"arxiv_id": arxiv_id},
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
