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
from app.domain.auth import ADMIN_USER_ID, get_user_id, require_crawler_api_key
from app.domain.models import Dataset, Document
from app.domain.schemas import (
    CrawlerReviewRequest,
    CrawlerSubmissionPage,
    CrawlerSubmissionRead,
)
from app.rag.config import settings
from app.rag.database import get_db
from app.rag.services.storage.factory import StorageFactory
from app.services.document_queue import (
    DOCUMENT_STATUS_PENDING_REVIEW,
    DOCUMENT_STATUS_REJECTED,
    reset_document_for_queue,
    utc_now,
)

router = APIRouter(prefix="/api/v1/crawler", tags=["资料采集"])
submission_router = APIRouter(
    prefix="/api/v1/document-submissions",
    tags=["外部文档审核"],
)


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


def _parse_source_metadata(value: str | None, source_name: str) -> dict[str, object]:
    metadata: dict[str, object] = {
        "source_name": source_name,
        "crawler_name": source_name,
    }
    if not value:
        return metadata
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="metadata 必须是有效的 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="metadata 必须是 JSON 对象")
    metadata.update(parsed)
    metadata["source_name"] = source_name
    metadata["crawler_name"] = source_name
    return metadata


@router.post(
    "/uploads",
    response_model=CrawlerSubmissionRead,
    status_code=status.HTTP_201_CREATED,
)
@submission_router.post(
    "",
    response_model=CrawlerSubmissionRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_crawler_submission(
    dataset_id: Annotated[int, Form(gt=0)],
    file: Annotated[UploadFile, File()],
    source_url: Annotated[str | None, Form(max_length=1024)] = None,
    title: Annotated[str | None, Form(max_length=512)] = None,
    source_name: Annotated[str | None, Form(min_length=1, max_length=64)] = None,
    crawler_name: Annotated[str, Form(min_length=1, max_length=64)] = "external-crawler",
    metadata: Annotated[str | None, Form(max_length=16384)] = None,
    _: None = Depends(require_crawler_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive an external file into MinIO without dispatching a parse task."""

    dataset = await _owned_dataset(db, dataset_id, ADMIN_USER_ID)
    normalized_source_name = (source_name or crawler_name).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", normalized_source_name):
        raise HTTPException(
            status_code=422,
            detail="source_name 只能包含字母、数字、点、下划线和连字符",
        )
    normalized_title = (title.strip() or None) if title else None
    normalized_source_url = _normalize_source_url(source_url)
    source_metadata = _parse_source_metadata(metadata, normalized_source_name)
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
@submission_router.get("", response_model=CrawlerSubmissionPage)
async def list_crawler_submissions(
    review_status: Literal["PENDING", "APPROVED", "REJECTED"] | None = Query(default="PENDING"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    user_id: int = Depends(get_user_id),
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
@submission_router.get("/{document_id}/file")
async def download_crawler_submission_file(
    document_id: int,
    user_id: int = Depends(get_user_id),
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
@submission_router.post(
    "/{document_id}/review",
    response_model=CrawlerSubmissionRead,
)
async def review_crawler_submission(
    document_id: int,
    payload: CrawlerReviewRequest,
    response: Response,
    user_id: int = Depends(get_user_id),
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

    dataset_name: str | None
    if (
        payload.decision == "APPROVED"
        and payload.dataset_id is not None
        and payload.dataset_id != document.dataset_id
    ):
        target_dataset = await _owned_dataset(
            db,
            payload.dataset_id,
            user_id,
            for_update=True,
        )
        document.dataset_id = target_dataset.id
        document.folder_id = None
        dataset_name = target_dataset.name
    else:
        dataset_name = await db.scalar(
            select(Dataset.name).where(Dataset.id == document.dataset_id)
        )
        if dataset_name is None:
            raise HTTPException(status_code=409, detail="目标数据集已不存在，无法完成审核")

    document.review_status = payload.decision
    document.review_note = payload.note
    document.reviewed_by_user_id = user_id
    document.reviewed_at = utc_now()
    if payload.decision == "APPROVED":
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
    return _submission_payload(document, dataset_name)
