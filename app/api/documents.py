"""文档上传、异步解析队列与完整生命周期 API。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import re
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlsplit
from uuid import uuid4

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
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import get_user_id
from app.domain.models import Dataset, Document, DocumentFolder
from app.domain.schemas import (
    DocumentChunkPage,
    DocumentPreviewMap,
    DocumentRead,
    DocumentUpdate,
)
from app.domain.text import repair_legacy_mojibake
from app.rag.config import settings
from app.rag.database import get_db
from app.rag.models.chunk_record import ChunkRecordDB
from app.rag.observability.logging import logger
from app.rag.services.storage.factory import StorageFactory
from app.services.document_dispatch import DocumentParseDispatcher, mark_document_dispatch_pending
from app.services.document_ingestion import SimpleDocumentIngestionService
from app.services.document_queue import (
    DOCUMENT_STATUS_FAILED,
    DOCUMENT_STATUS_PENDING_REVIEW,
    DOCUMENT_STATUS_PROCESSING,
    DOCUMENT_STATUS_QUEUED,
    DOCUMENT_STATUS_READY,
    DOCUMENT_STATUS_REJECTED,
    reset_document_for_queue,
    utc_now,
)

router = APIRouter(prefix="/api/v1", tags=["文档解析"])

SUPPORTED_FILE_TYPES = {"pdf", "doc", "docx", "html", "htm"}
_OLE_COMPOUND_FILE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
DOCUMENT_STATUSES = {
    DOCUMENT_STATUS_QUEUED,
    DOCUMENT_STATUS_PROCESSING,
    DOCUMENT_STATUS_READY,
    DOCUMENT_STATUS_FAILED,
    DOCUMENT_STATUS_PENDING_REVIEW,
    DOCUMENT_STATUS_REJECTED,
}

_MARKDOWN_IMAGE_PATTERN = re.compile(
    r"(?P<prefix>!\[[^\]\n]*\]\()"
    r"(?P<target><[^>\n]+>|[^)\s\n]+)"
    r"(?P<suffix>(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\))"
)
_PREVIEW_IMAGE_CONTENT_TYPES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}


async def _dispatch_document(db: AsyncSession, document: Document) -> None:
    """Try immediate publish; durable outbox reconciliation owns recovery."""

    await DocumentParseDispatcher().dispatch(document)


async def _owned_dataset(
    db: AsyncSession,
    dataset_id: int,
    user_id: int,
    *,
    for_update: bool = False,
) -> Dataset:
    statement = select(Dataset).where(
        Dataset.id == dataset_id,
        Dataset.user_id == user_id,
        Dataset.status == "ACTIVE",
    )
    if for_update:
        statement = statement.with_for_update()
    dataset = await db.scalar(statement)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在或不属于当前用户")
    return dataset


async def _owned_document(
    db: AsyncSession,
    document_id: int,
    user_id: int,
    *,
    for_update: bool = False,
) -> Document:
    statement = select(Document).where(
        Document.id == document_id,
        Document.user_id == user_id,
    )
    if for_update:
        statement = statement.with_for_update()
    document = await db.scalar(statement)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return document


async def _owned_folder(
    db: AsyncSession,
    folder_id: int,
    dataset_id: int,
    user_id: int,
    *,
    for_update: bool = False,
) -> DocumentFolder:
    statement = select(DocumentFolder).where(
        DocumentFolder.id == folder_id,
        DocumentFolder.dataset_id == dataset_id,
        DocumentFolder.user_id == user_id,
    )
    if for_update:
        statement = statement.with_for_update()
    folder = await db.scalar(statement)
    if folder is None:
        raise HTTPException(status_code=404, detail="文件夹不存在或不属于当前数据集")
    return folder


async def _save_upload_to_path(file: UploadFile, destination: Path) -> int:
    """分块落盘并在读取过程中执行大小限制，不构造完整文件 bytes。"""

    limit = settings.DOCUMENT_UPLOAD_MAX_BYTES
    total = 0
    with destination.open("wb") as output:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"文件超过上传上限 {limit} bytes",
                )
            output.write(chunk)
    if total == 0:
        raise HTTPException(status_code=422, detail="上传文件不能为空")
    return total


def _validate_word_file_signature(path: Path, file_type: str) -> None:
    """在进入队列前拒绝仅靠后缀伪装的 DOC/DOCX。"""

    if file_type not in {"doc", "docx"}:
        return
    with path.open("rb") as source:
        header = source.read(8)
    valid = (
        header.startswith(_OLE_COMPOUND_FILE_MAGIC)
        if file_type == "doc"
        else header.startswith(_ZIP_MAGICS)
    )
    if not valid:
        raise HTTPException(status_code=422, detail=f"文件内容不是有效的 {file_type.upper()} 文档")


_QUALITY_SUMMARY_FIELDS = (
    "schema_version",
    "status",
    "diagnostic_status",
    "business_blocking",
    "parser_backend",
    "page_count",
    "pdf_page_count",
    "markdown_page_count",
    "text_coverage_ratio",
    "ocr_required_pages",
    "ocr_page_count",
    "low_confidence_pages",
    "vision_incomplete_pages",
    "visually_assessed_pages",
    "visual_no_content_pages",
    "unit_type",
    "unit_count",
    "source_table_count",
    "structured_table_count",
    "source_image_reference_count",
    "source_supported_image_reference_count",
    "source_legacy_vml_image_reference_count",
    "image_asset_count",
    "image_upload_count",
    "suppressed_unexplained_image_count",
)
_QUALITY_LIST_WARNING_LIMIT = 20


def _document_retrieval_ready(document: Document) -> bool:
    """返回与召回门禁相同的文档可见性。"""

    if str(document.status or "").upper() != DOCUMENT_STATUS_READY:
        return False
    if str(document.file_type or "").lower() not in {"pdf", "doc", "docx"}:
        return True
    report = document.parse_quality if isinstance(document.parse_quality, dict) else {}
    return bool(
        str(document.parse_quality_status or "").upper() == "PASSED"
        and str(report.get("status") or "").upper() == "PASSED"
    )


def _quality_report_summary(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """压缩列表所需的质量摘要，不复制 OCR 正文或结构化单元格。"""

    if not isinstance(report, dict):
        return None
    summary = {
        field_name: report[field_name]
        for field_name in _QUALITY_SUMMARY_FIELDS
        if field_name in report
    }
    warnings = report.get("warnings")
    if isinstance(warnings, list):
        summary["warning_count"] = len(warnings)
        summary["warnings"] = warnings[:_QUALITY_LIST_WARNING_LIMIT]

    fallback = report.get("fallback")
    if isinstance(fallback, dict):
        raw_results = fallback.get("results")
        results = (
            [item for item in raw_results if isinstance(item, dict)]
            if isinstance(raw_results, list)
            else []
        )
        fallback_warnings = fallback.get("warnings")
        fallback_summary = {
            field_name: fallback[field_name]
            for field_name in (
                "status",
                "dpi",
                "pdf_page_count",
                "quality_page_count",
                "processed_page_count",
                "failed_page_count",
            )
            if field_name in fallback
        }
        fallback_summary.update(
            ocr_page_count=sum(
                str(item.get("method") or "").upper() == "OCR" for item in results
            ),
            vision_page_count=sum(
                str(item.get("method") or "").upper() == "VISION" for item in results
            ),
            pages=sorted(
                {
                    int(item["page_number"])
                    for item in results
                    if str(item.get("page_number") or "").isdigit()
                    and int(item["page_number"]) > 0
                }
            ),
        )
        if isinstance(fallback_warnings, list):
            fallback_summary["warning_count"] = len(fallback_warnings)
            fallback_summary["warnings"] = fallback_warnings[:_QUALITY_LIST_WARNING_LIMIT]
        summary["fallback"] = fallback_summary

    validation = report.get("content_validation")
    if isinstance(validation, dict):
        blocking_issues = validation.get("blocking_issues")
        validation_warnings = validation.get("warnings")
        validation_summary = {
            field_name: validation[field_name]
            for field_name in (
                "status",
                "structural_passed",
                "page_set_complete",
                "evaluated_page_count",
                "missing_markdown_pages",
                "unexpected_markdown_pages",
                "affected_pages",
            )
            if field_name in validation
        }
        if isinstance(blocking_issues, list):
            validation_summary["blocking_issue_count"] = len(blocking_issues)
        if isinstance(validation_warnings, list):
            validation_summary["warning_count"] = len(validation_warnings)
        summary["content_validation"] = validation_summary
    return summary


def _document_parse_time_ms(document: Document) -> int | None:
    """Prefer the complete processing attempt duration for API compatibility."""

    started_at = document.processing_started_at
    finished_at = document.finished_at
    if isinstance(started_at, datetime) and isinstance(finished_at, datetime):
        if started_at.tzinfo is not None:
            started_at = started_at.astimezone(UTC).replace(tzinfo=None)
        if finished_at.tzinfo is not None:
            finished_at = finished_at.astimezone(UTC).replace(tzinfo=None)
        duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        if duration_ms >= 0:
            return duration_ms
    return document.parse_time_ms


def _document_timestamp(value: datetime | None) -> datetime | None:
    """Expose MySQL's naive UTC document timestamps as timezone-aware UTC values."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _document_payload(document: Document, *, quality_detail: bool = True) -> dict:
    parse_quality = (
        document.parse_quality
        if quality_detail
        else _quality_report_summary(document.parse_quality)
    )
    return {
        "document_id": document.id,
        "dataset_id": document.dataset_id,
        "folder_id": document.folder_id,
        "filename": document.filename,
        "file_type": document.file_type,
        "file_size": document.file_size,
        "content_type": document.content_type,
        "parser_backend": document.parser_backend,
        "status": document.status,
        "version": document.version,
        "attempt_count": document.attempt_count,
        "available_at": _document_timestamp(document.available_at),
        "queued_at": _document_timestamp(document.queued_at),
        "processing_started_at": _document_timestamp(document.processing_started_at),
        "lease_expires_at": _document_timestamp(document.lease_expires_at),
        "finished_at": _document_timestamp(document.finished_at),
        "error_code": document.error_code,
        "error_message": repair_legacy_mojibake(document.error_message),
        "reparse_requested": document.reparse_requested,
        # HTML 本身没有固定页面；同时屏蔽旧版按字符数折算后已入库的伪页数。
        "page_count": (
            None
            if str(document.file_type or "").strip().lower() in {"html", "htm"}
            else document.page_count
        ),
        "chunk_count": document.chunk_count,
        "parse_time_ms": _document_parse_time_ms(document),
        "parse_quality_status": document.parse_quality_status,
        "parse_quality": parse_quality,
        "source_type": document.source_type or "MANUAL_UPLOAD",
        "source_url": document.source_url,
        "source_title": document.source_title,
        "source_metadata": document.source_metadata,
        "review_status": document.review_status or "NOT_REQUIRED",
        "review_note": document.review_note,
        "reviewed_at": _document_timestamp(document.reviewed_at),
        "retrieval_ready": _document_retrieval_ready(document),
        "created_at": _document_timestamp(document.created_at),
        "updated_at": _document_timestamp(document.updated_at),
    }


def _chunk_payload(record: ChunkRecordDB) -> dict:
    """把 Chunk 真值记录收敛为对外可追溯、无内部索引细节的响应。"""

    return {
        "chunk_id": record.chunk_id,
        "document_version": record.document_version,
        "chunk_index": record.chunk_index,
        "chunk_type": record.chunk_type,
        "content": record.content,
        "char_count": len(record.content),
        "start_line": record.start_line,
        "end_line": record.end_line,
        "start_page": record.start_page,
        "end_page": record.end_page,
        "structure": record.structure_metadata,
        "created_at": record.create_time,
        "updated_at": record.update_time,
    }


def _preview_not_ready(document: Document) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "DOCUMENT_PREVIEW_NOT_READY",
            "message": "文档当前版本尚未解析完成，暂时不能预览",
            "status": document.status,
        },
    )


def _iter_preview_file(path: Path, *, block_size: int = 1024 * 1024) -> Iterator[bytes]:
    """分块输出已下载的 Markdown，消费完毕或客户端断开时清理临时文件。"""

    try:
        with path.open("rb") as source:
            while content := source.read(block_size):
                yield content
    finally:
        path.unlink(missing_ok=True)


def _validated_preview_asset_path(value: str) -> PurePosixPath:
    """校验预览资产的文档内相对路径。

    代理只允许当前 Markdown 目录内的常见栅格图片；绝对路径、回退、
    Windows 分隔符和 SVG/HTML 等可执行内容均不会进入对象存储。
    """

    if not value or len(value.encode("utf-8")) > 1024 or "\x00" in value or "\\" in value:
        raise ValueError("非法预览资产引用")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("非法预览资产路径")
    path = PurePosixPath(value)
    if path.is_absolute() or path.suffix.lower() not in _PREVIEW_IMAGE_CONTENT_TYPES:
        raise ValueError("非法预览资产类型")
    return path


def _encode_preview_asset_ref(relative_path: PurePosixPath | str) -> str:
    path = _validated_preview_asset_path(str(relative_path))
    return base64.urlsafe_b64encode(path.as_posix().encode("utf-8")).decode("ascii").rstrip("=")


def _decode_preview_asset_ref(asset_ref: str) -> PurePosixPath:
    if not asset_ref or len(asset_ref) > 2048:
        raise ValueError("非法预览资产引用")
    padding = "=" * (-len(asset_ref) % 4)
    try:
        decoded = base64.b64decode(
            f"{asset_ref}{padding}",
            altchars=b"-_",
            validate=True,
        ).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("非法预览资产引用") from exc
    return _validated_preview_asset_path(decoded)


def _preview_asset_relative_path(
    target: str,
    *,
    parsed_bucket: str,
    parsed_object_key: str,
) -> PurePosixPath | None:
    """仅解析属于当前 Markdown 产物目录的图片目标。"""

    normalized_target = target.strip()
    if normalized_target.startswith("<") and normalized_target.endswith(">"):
        normalized_target = normalized_target[1:-1].strip()
    parsed = urlsplit(normalized_target)
    if parsed.scheme.lower() in {"data", "blob"} or parsed.scheme.lower() not in {
        "",
        "http",
        "https",
    }:
        return None

    parsed_root = PurePosixPath(parsed_object_key).parent
    if parsed.scheme:
        decoded_url_path = unquote(parsed.path)
        marker = f"/{parsed_bucket}/{parsed_root.as_posix()}/"
        marker_index = decoded_url_path.find(marker)
        if marker_index < 0:
            return None
        relative_value = decoded_url_path[marker_index + len(marker) :]
    else:
        if parsed.netloc or parsed.path.startswith("/"):
            return None
        relative_value = unquote(parsed.path)
        while relative_value.startswith("./"):
            relative_value = relative_value[2:]

    try:
        return _validated_preview_asset_path(relative_value)
    except ValueError:
        return None


def _rewrite_preview_markdown_line(line: str, document: Document) -> str:
    """将当前文档的私有图片引用替换为租户受控代理地址。"""

    if not document.parsed_bucket or not document.parsed_object_key:
        return line

    def replace(match: re.Match[str]) -> str:
        relative_path = _preview_asset_relative_path(
            match.group("target"),
            parsed_bucket=document.parsed_bucket,
            parsed_object_key=document.parsed_object_key,
        )
        if relative_path is None:
            return match.group(0)
        asset_ref = _encode_preview_asset_ref(relative_path)
        proxy_url = (
            f"/api/v1/documents/{document.id}/preview/versions/{document.version}"
            f"/assets/{asset_ref}"
        )
        return f"{match.group('prefix')}{proxy_url}{match.group('suffix')}"

    return _MARKDOWN_IMAGE_PATTERN.sub(replace, line)


def _rewrite_preview_markdown_file(
    source_path: Path,
    target_path: Path,
    document: Document,
) -> None:
    """逐行重写 Markdown，避免大文档全量读入内存。"""

    with source_path.open("r", encoding="utf-8", newline="") as source:
        with target_path.open("w", encoding="utf-8", newline="") as target:
            for line in source:
                target.write(_rewrite_preview_markdown_line(line, document))


def _boundary_payload(record: ChunkRecordDB) -> dict:
    structure = record.structure_metadata if isinstance(record.structure_metadata, dict) else {}
    heading_trail = structure.get("heading_trail")
    return {
        "chunk_id": record.chunk_id,
        "chunk_index": int(record.chunk_index),
        "chunk_type": record.chunk_type,
        "start_line": int(record.start_line),
        "end_line": int(record.end_line),
        "start_page": record.start_page,
        "end_page": record.end_page,
        "heading_trail": [str(value) for value in heading_trail]
        if isinstance(heading_trail, list)
        else [],
        "split_strategy": str(structure["split_strategy"])
        if structure.get("split_strategy")
        else None,
    }


def _strict_line_boundaries(
    records: list[ChunkRecordDB],
    *,
    allow_overlapping_lines: bool = False,
) -> list[dict] | None:
    """只接受按 chunk_index 严格递增且行范围不重叠的主体分片。"""

    boundaries: list[dict] = []
    previous_index: int | None = None
    previous_end: int | None = None
    for record in records:
        chunk_index = record.chunk_index
        start_line = record.start_line
        end_line = record.end_line
        if (
            isinstance(chunk_index, bool)
            or not isinstance(chunk_index, int)
            or isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or isinstance(end_line, bool)
            or not isinstance(end_line, int)
            or chunk_index < 0
            or start_line < 0
            or end_line < start_line
            or (previous_index is not None and chunk_index <= previous_index)
            or (
                not allow_overlapping_lines
                and previous_end is not None
                and start_line <= previous_end
            )
        ):
            return None
        boundaries.append(_boundary_payload(record))
        previous_index = chunk_index
        previous_end = end_line
    return boundaries


def _legacy_line_boundaries(records: list[ChunkRecordDB]) -> tuple[list[dict], int]:
    """为旧版本保守恢复主体边界，并跳过落在已接受主体范围内的派生候选。

    旧记录没有 ``chunk_role``，无法声称边界可靠；但 LinkRag 默认 noop 的 source
    chunk 行范围严格递增，而表格/图片派生块位于 source 范围内部。这里仅接受这一
    可验证的单调子序列，所有跳过项只计作历史推断，不改变 ``map_reliable=false``。
    """

    boundaries: list[dict] = []
    skipped = 0
    previous_index: int | None = None
    previous_end: int | None = None
    for record in records:
        chunk_index = record.chunk_index
        start_line = record.start_line
        end_line = record.end_line
        if (
            isinstance(chunk_index, bool)
            or not isinstance(chunk_index, int)
            or isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or isinstance(end_line, bool)
            or not isinstance(end_line, int)
            or chunk_index < 0
            or start_line < 0
            or end_line < start_line
            or (previous_index is not None and chunk_index <= previous_index)
        ):
            return [], len(records)
        previous_index = chunk_index
        if previous_end is not None and start_line <= previous_end:
            skipped += 1
            continue
        boundaries.append(_boundary_payload(record))
        previous_end = end_line
    return boundaries, skipped


def _preview_boundary_map(
    document: Document,
    records: list[ChunkRecordDB],
) -> dict:
    """从当前版本 Chunk 真值记录生成连续阅读所需的边界图。"""

    derived_records = [
        record
        for record in records
        if isinstance(record.structure_metadata, dict)
        and str(record.structure_metadata.get("chunk_role") or "").strip().lower()
        == "derived_element"
    ]
    source_records = [record for record in records if record not in derived_records]
    has_legacy_record = any(
        not isinstance(record.structure_metadata, dict)
        or not str(record.structure_metadata.get("split_strategy") or "").strip()
        for record in source_records
    )
    has_approximate_line = any(
        isinstance(record.structure_metadata, dict)
        and record.structure_metadata.get("line_span_approx") is True
        for record in source_records
    )
    precision: Literal["line", "approximate_line", "legacy_line"]
    if has_legacy_record:
        precision = "legacy_line"
    elif has_approximate_line:
        precision = "approximate_line"
    else:
        precision = "line"

    inferred_derived_count = 0
    if has_legacy_record:
        boundaries, inferred_derived_count = _legacy_line_boundaries(source_records)
        resolved_boundaries = boundaries or None
    else:
        resolved_boundaries = _strict_line_boundaries(
            source_records,
            allow_overlapping_lines=precision == "approximate_line",
        )
        boundaries = resolved_boundaries or []
    boundaries = [
        {"boundary_index": boundary_index, **boundary}
        for boundary_index, boundary in enumerate(boundaries)
    ]
    has_source_boundaries = bool(source_records) and resolved_boundaries is not None
    # 只有确实落在 Markdown 行内的边界才标为近似；语义切分若恰好
    # 沿换行切开，现有 start_line/end_line 足以提供可靠定位。
    structurally_reliable = (
        not has_legacy_record and has_source_boundaries and precision == "line"
    )
    reparse_required = has_legacy_record or not has_source_boundaries
    return {
        "document_id": document.id,
        "dataset_id": document.dataset_id,
        "document_version": document.version,
        "boundary_precision": precision,
        "map_reliable": structurally_reliable,
        "reparse_required": reparse_required,
        "source_chunk_count": len(boundaries),
        "derived_chunk_count": len(derived_records) + inferred_derived_count,
        "boundaries": boundaries,
    }


@router.post(
    "/datasets/{dataset_id}/documents",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DocumentRead,
)
async def upload_and_queue_document(
    dataset_id: int,
    response: Response,
    file: UploadFile = File(...),
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
    folder_id: Annotated[int | None, Form(gt=0)] = None,
) -> dict:
    """流式保存原文件，提交状态后立即向 RabbitMQ 发布解析任务。"""

    await _owned_dataset(db, dataset_id, user_id)
    if folder_id is not None:
        await _owned_folder(db, folder_id, dataset_id, user_id)

    filename = PurePath(file.filename or "").name
    file_type = PurePath(filename).suffix.lower().lstrip(".")
    if not filename or file_type not in SUPPORTED_FILE_TYPES:
        supported = ", ".join(sorted(SUPPORTED_FILE_TYPES))
        raise HTTPException(status_code=415, detail=f"不支持的文件格式，可用格式: {supported}")
    if len(filename) > 255:
        raise HTTPException(status_code=422, detail="文件名不能超过 255 个字符")

    storage = StorageFactory.get_storage()
    object_key = f"raw/{user_id}/{dataset_id}/{uuid4().hex}/{filename}"
    content_type = (file.content_type or "application/octet-stream")[:128]
    parse_temp_root = Path(settings.PARSE_TEMP_DIR)
    await asyncio.to_thread(parse_temp_root.mkdir, parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="energy-carbon-upload-",
        dir=parse_temp_root,
    ) as temp_dir:
        source_path = Path(temp_dir) / f"source.{file_type}"
        await _save_upload_to_path(file, source_path)
        _validate_word_file_signature(source_path, file_type)
        document = await queue_document_from_path(
            dataset_id=dataset_id,
            user_id=user_id,
            filename=filename,
            source_path=source_path,
            content_type=content_type,
            db=db,
            storage=storage,
            object_key=object_key,
            ownership_checked=True,
            folder_id=folder_id,
        )

    response.headers["Location"] = f"/api/v1/documents/{document.id}"
    return _document_payload(document)


async def queue_document_from_path(
    *,
    dataset_id: int,
    user_id: int,
    filename: str,
    source_path: Path,
    content_type: str,
    db: AsyncSession,
    storage: Any | None = None,
    object_key: str | None = None,
    ownership_checked: bool = False,
    review_required: bool = False,
    source_type: str = "MANUAL_UPLOAD",
    source_url: str | None = None,
    source_title: str | None = None,
    source_metadata: dict[str, Any] | None = None,
    folder_id: int | None = None,
) -> Document:
    """保存受支持文件；普通上传立即入队，外部采集文件等待人工审核。"""

    if not ownership_checked:
        await _owned_dataset(db, dataset_id, user_id)
    safe_filename = PurePath(filename).name
    file_type = PurePath(safe_filename).suffix.lower().lstrip(".")
    if not safe_filename or file_type not in SUPPORTED_FILE_TYPES:
        raise HTTPException(status_code=415, detail="不支持的文件格式")
    if len(safe_filename) > 255:
        raise HTTPException(status_code=422, detail="文件名不能超过 255 个字符")
    file_size = source_path.stat().st_size
    if file_size <= 0:
        raise HTTPException(status_code=422, detail="文件不能为空")
    if file_size > settings.DOCUMENT_UPLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件超过上传上限 {settings.DOCUMENT_UPLOAD_MAX_BYTES} bytes",
        )
    _validate_word_file_signature(source_path, file_type)

    storage = storage or StorageFactory.get_storage()
    object_key = object_key or (
        f"raw/{user_id}/{dataset_id}/{uuid4().hex}/{safe_filename}"
    )
    normalized_content_type = (content_type or "application/octet-stream")[:128]
    try:
        await asyncio.to_thread(
            storage.upload_from_path,
            settings.MINIO_RAW_BUCKET,
            object_key,
            source_path,
            normalized_content_type,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="原始文件上传对象存储失败") from exc

    # 对象上传期间数据集可能被并发删除；提交 Document 前重新加锁校验，使删除与插入串行。
    try:
        await _owned_dataset(db, dataset_id, user_id, for_update=True)
        if folder_id is not None:
            await _owned_folder(
                db,
                folder_id,
                dataset_id,
                user_id,
                for_update=True,
            )
    except HTTPException:
        try:
            await asyncio.to_thread(storage.remove_prefix, settings.MINIO_RAW_BUCKET, object_key)
        except Exception:
            logger.bind(
                event="raw_upload_cleanup_failed",
                document_object_key=object_key,
            ).warning("数据集已删除后清理原文件对象失败")
        raise

    now = utc_now()
    document_status = (
        DOCUMENT_STATUS_PENDING_REVIEW if review_required else DOCUMENT_STATUS_QUEUED
    )
    document = Document(
        dataset_id=dataset_id,
        user_id=user_id,
        folder_id=folder_id,
        filename=safe_filename,
        file_type=file_type,
        file_size=file_size,
        content_type=normalized_content_type,
        raw_bucket=settings.MINIO_RAW_BUCKET,
        raw_object_key=object_key,
        parser_backend="opendataloader" if file_type == "pdf" else "builtin",
        status=document_status,
        version=1,
        attempt_count=0,
        available_at=None if review_required else now,
        queued_at=None if review_required else now,
        dispatch_status="IDLE" if review_required else "PENDING",
        dispatch_available_at=None if review_required else now,
        source_type=source_type,
        source_url=source_url,
        source_title=source_title,
        source_metadata=source_metadata,
        review_status="PENDING" if review_required else "NOT_REQUIRED",
    )
    if not review_required:
        mark_document_dispatch_pending(document, now=now)
    try:
        db.add(document)
        await db.commit()
        await db.refresh(document)
    except Exception:
        await db.rollback()
        try:
            await asyncio.to_thread(storage.remove_prefix, settings.MINIO_RAW_BUCKET, object_key)
        except Exception as cleanup_exc:
            logger.bind(
                event="raw_upload_cleanup_failed",
                document_object_key=object_key,
                error_type=type(cleanup_exc).__name__,
            ).warning("数据库写入失败后清理原文件对象失败")
        raise

    if not review_required:
        await _dispatch_document(db, document)
    return document


# 保留旧函数名，避免内部调用方升级期间 import 失败；HTTP 契约已经改为 202 + QUEUED。
upload_and_parse_document = upload_and_queue_document


async def _load_ordered_documents(
    db: AsyncSession,
    *filters: Any,
) -> list[Document]:
    """Sort narrow IDs first so MySQL never filesorts wide JSON document rows."""

    document_ids = list(
        (
            await db.scalars(
                select(Document.id)
                .where(*filters)
                .order_by(Document.created_at.desc(), Document.id.desc())
            )
        ).all()
    )
    if not document_ids:
        return []

    documents = list(
        (await db.scalars(select(Document).where(Document.id.in_(document_ids)))).all()
    )
    documents_by_id = {int(document.id): document for document in documents}
    return [documents_by_id[int(document_id)] for document_id in document_ids]


@router.get("/datasets/{dataset_id}/documents", response_model=list[DocumentRead])
async def list_documents(
    dataset_id: int,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _owned_dataset(db, dataset_id, user_id)
    documents = await _load_ordered_documents(
        db,
        Document.dataset_id == dataset_id,
        Document.user_id == user_id,
        Document.review_status.in_(("NOT_REQUIRED", "APPROVED")),
    )
    return [_document_payload(document, quality_detail=False) for document in documents]


@router.get("/documents", response_model=list[DocumentRead])
async def list_all_documents(
    dataset_id: int | None = Query(default=None, gt=0),
    document_status: str | None = Query(default=None, alias="status"),
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """一次读取当前用户的文档队列，可按数据集或处理状态筛选。"""

    normalized_status = document_status.strip().upper() if document_status else None
    if normalized_status is not None and normalized_status not in DOCUMENT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_DOCUMENT_STATUS",
                "message": f"status 必须是 {', '.join(sorted(DOCUMENT_STATUSES))} 之一",
            },
        )

    filters = [
        Document.user_id == user_id,
        Document.review_status.in_(("NOT_REQUIRED", "APPROVED")),
    ]
    if dataset_id is not None:
        filters.append(Document.dataset_id == dataset_id)
    if normalized_status is not None:
        filters.append(Document.status == normalized_status)
    documents = await _load_ordered_documents(db, *filters)
    return [_document_payload(document, quality_detail=False) for document in documents]


@router.get("/documents/{document_id}", response_model=DocumentRead)
async def get_document_status(
    document_id: int,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return _document_payload(await _owned_document(db, document_id, user_id))


@router.get("/documents/{document_id}/chunks", response_model=DocumentChunkPage)
async def list_document_chunks(
    document_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    query: str | None = Query(default=None, alias="q", max_length=200),
    chunk_type: str | None = Query(default=None, min_length=1, max_length=32),
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """查看 READY 文档当前版本的分片正文、顺序与来源位置。"""

    document = await _owned_document(db, document_id, user_id)
    if document.status != DOCUMENT_STATUS_READY:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DOCUMENT_CHUNKS_NOT_READY",
                "message": "文档当前版本尚未解析完成，暂时不能查看分片",
                "status": document.status,
            },
        )

    normalized_query = query.strip() if query else None
    normalized_type = chunk_type.strip().lower() if chunk_type else None
    if chunk_type is not None and not normalized_type:
        raise HTTPException(status_code=422, detail="分片类型不能为空")
    conditions = [
        ChunkRecordDB.doc_id == document.id,
        ChunkRecordDB.set_id == document.dataset_id,
        ChunkRecordDB.user_id == user_id,
        ChunkRecordDB.document_version == document.version,
    ]
    if normalized_query:
        conditions.append(
            or_(
                ChunkRecordDB.content.contains(normalized_query, autoescape=True),
                ChunkRecordDB.chunk_id.contains(normalized_query, autoescape=True),
            )
        )
    if normalized_type:
        conditions.append(ChunkRecordDB.chunk_type == normalized_type)

    total = int(
        await db.scalar(
            select(func.count()).select_from(ChunkRecordDB).where(*conditions)
        )
        or 0
    )
    records = (
        await db.scalars(
            select(ChunkRecordDB)
            .where(*conditions)
            .order_by(ChunkRecordDB.chunk_index.asc(), ChunkRecordDB.id.asc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return {
        "document_id": document.id,
        "dataset_id": document.dataset_id,
        "document_version": document.version,
        "items": [_chunk_payload(record) for record in records],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/documents/{document_id}/preview/content")
async def stream_document_preview_content(
    document_id: int,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """流式返回 READY 文档当前版本的完整解析 Markdown。"""

    document = await _owned_document(db, document_id, user_id)
    if document.status != DOCUMENT_STATUS_READY:
        raise _preview_not_ready(document)
    if not document.parsed_bucket or not document.parsed_object_key:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "DOCUMENT_PREVIEW_STORAGE_UNAVAILABLE",
                "message": "文档解析产物位置不完整，请重新解析后再试",
            },
        )

    temp_root = Path(settings.PARSE_TEMP_DIR)
    source_path: Path | None = None
    preview_path: Path | None = None
    try:
        await asyncio.to_thread(temp_root.mkdir, parents=True, exist_ok=True)
        source_descriptor, source_name = tempfile.mkstemp(
            prefix=f"document-preview-source-{document.id}-",
            suffix=".md",
            dir=temp_root,
        )
        os.close(source_descriptor)
        source_path = Path(source_name)
        preview_descriptor, preview_name = tempfile.mkstemp(
            prefix=f"document-preview-output-{document.id}-",
            suffix=".md",
            dir=temp_root,
        )
        os.close(preview_descriptor)
        preview_path = Path(preview_name)
    except OSError as exc:
        if source_path is not None:
            source_path.unlink(missing_ok=True)
        if preview_path is not None:
            preview_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=502,
            detail={
                "code": "DOCUMENT_PREVIEW_STORAGE_UNAVAILABLE",
                "message": "无法准备文档预览，请稍后重试",
            },
        ) from exc

    try:
        storage = StorageFactory.get_storage()
        await asyncio.to_thread(
            storage.download_to_path,
            document.parsed_bucket,
            document.parsed_object_key,
            source_path,
        )
        await asyncio.to_thread(
            _rewrite_preview_markdown_file,
            source_path,
            preview_path,
            document,
        )
        content_length = preview_path.stat().st_size
    except Exception as exc:
        source_path.unlink(missing_ok=True)
        preview_path.unlink(missing_ok=True)
        logger.bind(
            event="document_preview_download_failed",
            document_id=document.id,
            user_id=user_id,
            error_type=type(exc).__name__,
        ).warning("下载文档预览产物失败")
        raise HTTPException(
            status_code=502,
            detail={
                "code": "DOCUMENT_PREVIEW_STORAGE_UNAVAILABLE",
                "message": "读取文档解析产物失败，请稍后重试",
            },
        ) from exc
    finally:
        if source_path is not None:
            source_path.unlink(missing_ok=True)

    return StreamingResponse(
        _iter_preview_file(preview_path),
        media_type="text/markdown; charset=utf-8",
        headers={
            "X-Document-Version": str(document.version),
            "Content-Length": str(content_length),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/documents/{document_id}/preview/versions/{document_version}/assets/{asset_ref}"
)
async def stream_document_preview_asset(
    document_id: int,
    document_version: int,
    asset_ref: str,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """流式返回当前 READY 版本 Markdown 内的私有图片资产。

    ``asset_ref`` 只承载 Markdown 目录内的 URL-safe 相对路径；服务端会
    重新校验 Bearer JWT 对应的用户、文档当前版本与路径边界，对外不返回
    bucket 或完整 object key。
    """

    document = await _owned_document(db, document_id, user_id)
    if document.status != DOCUMENT_STATUS_READY:
        raise _preview_not_ready(document)
    if (
        document_version <= 0
        or int(document.version or 0) != document_version
        or not document.parsed_bucket
        or not document.parsed_object_key
    ):
        raise HTTPException(status_code=404, detail="预览资产不存在")
    try:
        relative_path = _decode_preview_asset_ref(asset_ref)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="预览资产不存在") from exc

    parsed_root = PurePosixPath(document.parsed_object_key).parent
    asset_object_key = (parsed_root / relative_path).as_posix()
    content_type = _PREVIEW_IMAGE_CONTENT_TYPES[relative_path.suffix.lower()]
    temp_root = Path(settings.PARSE_TEMP_DIR)
    temp_path: Path | None = None
    try:
        await asyncio.to_thread(temp_root.mkdir, parents=True, exist_ok=True)
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=f"document-preview-asset-{document.id}-",
            suffix=relative_path.suffix.lower(),
            dir=temp_root,
        )
        os.close(file_descriptor)
        temp_path = Path(temp_name)
        storage = StorageFactory.get_storage()
        await asyncio.to_thread(
            storage.download_to_path,
            document.parsed_bucket,
            asset_object_key,
            temp_path,
        )
        content_length = temp_path.stat().st_size
    except Exception as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        logger.bind(
            event="document_preview_asset_download_failed",
            document_id=document.id,
            document_version=document_version,
            user_id=user_id,
            error_type=type(exc).__name__,
        ).warning("下载文档预览资产失败")
        raise HTTPException(
            status_code=502,
            detail={
                "code": "DOCUMENT_PREVIEW_ASSET_UNAVAILABLE",
                "message": "读取文档预览资产失败，请稍后重试",
            },
        ) from exc

    return StreamingResponse(
        _iter_preview_file(temp_path),
        media_type=content_type,
        headers={
            "X-Document-Version": str(document.version),
            "Content-Length": str(content_length),
            "Cache-Control": "private, no-store",
            "Content-Disposition": "inline",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/documents/{document_id}/preview/map", response_model=DocumentPreviewMap)
async def get_document_preview_map(
    document_id: int,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """返回 READY 文档当前版本的完整主体分片边界，不拼接 Chunk 正文。"""

    document = await _owned_document(db, document_id, user_id)
    if document.status != DOCUMENT_STATUS_READY:
        raise _preview_not_ready(document)
    conditions = [
        ChunkRecordDB.doc_id == document.id,
        ChunkRecordDB.set_id == document.dataset_id,
        ChunkRecordDB.user_id == user_id,
        ChunkRecordDB.document_version == document.version,
    ]
    records = list(
        (
            await db.scalars(
                select(ChunkRecordDB)
                .where(*conditions)
                .order_by(ChunkRecordDB.chunk_index.asc(), ChunkRecordDB.id.asc())
            )
        ).all()
    )
    return _preview_boundary_map(document, records)


@router.patch("/documents/{document_id}", response_model=DocumentRead)
async def update_document(
    document_id: int,
    payload: DocumentUpdate,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """修改用户可见文件名或虚拟分类；解析与存储对象保持不变。"""

    document = await _owned_document(db, document_id, user_id, for_update=True)
    updates = payload.model_dump(exclude_unset=True)
    if "folder_id" in updates and updates["folder_id"] is not None:
        await _owned_folder(
            db,
            updates["folder_id"],
            document.dataset_id,
            user_id,
            for_update=True,
        )
    for field_name, value in updates.items():
        setattr(document, field_name, value)
    await db.commit()
    await db.refresh(document)
    return _document_payload(document)


@router.post(
    "/documents/{document_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DocumentRead,
)
async def retry_document(
    document_id: int,
    response: Response,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """重新入队失败任务，或收回 lease 已过期的永久 PROCESSING 任务。"""

    document = await _owned_document(db, document_id, user_id, for_update=True)
    now = utc_now()
    stale_processing = document.status == DOCUMENT_STATUS_PROCESSING and (
        document.lease_expires_at is None or document.lease_expires_at <= now
    )
    if document.status != DOCUMENT_STATUS_FAILED and not stale_processing:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DOCUMENT_NOT_RETRYABLE",
                "message": "只有失败或租约已过期的解析任务可以重试",
            },
        )
    reset_document_for_queue(document, reparse=False)
    await db.commit()
    await db.refresh(document)
    await _dispatch_document(db, document)
    response.headers["Location"] = f"/api/v1/documents/{document.id}"
    return _document_payload(document)


@router.post(
    "/documents/{document_id}/reparse",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DocumentRead,
)
async def reparse_document(
    document_id: int,
    response: Response,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """为 READY/FAILED 文档创建新版本并用同一原文件重新解析。"""

    document = await _owned_document(db, document_id, user_id, for_update=True)
    if document.status not in {DOCUMENT_STATUS_READY, DOCUMENT_STATUS_FAILED}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DOCUMENT_NOT_REPARSABLE",
                "message": "只有已完成或失败的文档可以重新解析",
            },
        )
    reset_document_for_queue(document, reparse=True)
    await db.commit()
    await db.refresh(document)
    await _dispatch_document(db, document)
    response.headers["Location"] = f"/api/v1/documents/{document.id}"
    return _document_payload(document)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: int,
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> None:
    """删除非活跃文档及原文件、解析产物和三路索引。"""

    document = await _owned_document(db, document_id, user_id, for_update=True)
    if document.status not in {
        DOCUMENT_STATUS_READY,
        DOCUMENT_STATUS_FAILED,
        DOCUMENT_STATUS_PENDING_REVIEW,
        DOCUMENT_STATUS_REJECTED,
    }:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DOCUMENT_NOT_DELETABLE",
                "message": "只有待审核、已拒绝、解析完成或最终失败的文档可以删除",
            },
        )

    service = SimpleDocumentIngestionService(storage=StorageFactory.get_storage())
    try:
        await service.purge_document(document, db, include_raw=True)
    except Exception as exc:
        await db.rollback()
        logger.bind(
            event="document_delete_failed",
            document_id=document_id,
            user_id=user_id,
            error_type=type(exc).__name__,
        ).error("清理文档对象或索引失败")
        raise HTTPException(status_code=502, detail="文档存储或索引清理失败，请稍后重试") from exc
