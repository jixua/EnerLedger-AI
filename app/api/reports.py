"""Versioned report task APIs."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import get_user_id
from app.domain.models import Document, ReportArtifact, ReportQuestion, ReportRun
from app.domain.time import utc_now
from app.rag.config import settings
from app.rag.database import get_db
from app.rag.models.db_models import LLMModelConfigDB
from app.rag.observability.logging import logger
from app.services.document_queue import DOCUMENT_STATUS_READY
from app.services.report_budget import (
    ReportDocumentTooLargeError,
    assert_document_fits_context,
    load_document_payload_stats,
)
from app.services.report_artifacts import (
    ReportArtifactError,
    artifact_download_name,
    read_report_artifact,
)
from app.services.report_dispatch import ReportRunDispatcher, mark_report_dispatch_pending
from app.services.report_ir import validate_report_field_value
from app.services.report_model_policy import (
    ReportModelEndpointError,
    validate_report_model_endpoint,
)
from app.services.report_source_context import (
    load_document_chunk_manifest,
    load_report_source_context,
)
from app.services.report_templates import (
    ReportTemplate,
    ReportTemplateError,
    report_template_registry,
)

router = APIRouter(prefix="/api/v1", tags=["报告生成"])


class ReportCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_type: Literal["R1", "R2", "R3", "R4", "R5", "R6", "R7"]
    llm_config_id: int = Field(gt=0)
    language: Literal["zh-CN"] = "zh-CN"
    reporting_year: int | None = Field(default=None, ge=1900, le=2200)
    user_instructions: str | None = Field(default=None, max_length=2000)
    output_formats: list[Literal["ONLINE", "MARKDOWN", "DOCX"]] = Field(
        default_factory=lambda: ["ONLINE", "DOCX"]
    )
    custom_template_document_id: int | None = Field(default=None, gt=0)

    @field_validator("user_instructions")
    @classmethod
    def normalize_instructions(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("output_formats")
    @classmethod
    def unique_output_formats(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("至少选择一种输出格式")
        return list(dict.fromkeys(value))


class ReportAnswerItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: int = Field(gt=0)
    value: Any
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("value")
    @classmethod
    def _limit_answer_size(cls, value: Any) -> Any:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        if len(encoded) > settings.REPORT_ANSWER_MAX_CHARS:
            raise ValueError(
                f"补充内容过长（上限 {settings.REPORT_ANSWER_MAX_CHARS} 字符），"
                "请精简后重新提交"
            )
        return value


class ReportAnswersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[ReportAnswerItem] = Field(min_length=1, max_length=100)


def _input_hash(
    *,
    document: Document,
    template_snapshot: dict[str, Any],
    model_snapshot: dict[str, Any],
    document_manifest: dict[str, Any],
    custom_template_manifest: dict[str, Any] | None,
    payload: ReportCreateRequest,
) -> str:
    frozen = {
        "document_id": int(document.id),
        "document_version": int(document.version),
        "parsed_bucket": document.parsed_bucket,
        "parsed_object_key": document.parsed_object_key,
        "template_asset_hash": template_snapshot["asset_hash"],
        "model_snapshot": model_snapshot,
        "document_manifest": document_manifest,
        "custom_template_manifest": custom_template_manifest,
        "request": payload.model_dump(mode="json"),
    }
    encoded = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _run_dict(run: ReportRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "document_id": run.document_id,
        "dataset_id": run.dataset_id,
        "document_version": run.document_version,
        "report_type": run.report_type,
        "template_id": run.template_id,
        "template_version": run.template_version,
        "custom_template_document_id": run.custom_template_document_id,
        "custom_template_document_version": run.custom_template_document_version,
        "mode": run.mode,
        "language": run.language,
        "reporting_year": run.reporting_year,
        "output_formats": run.output_formats,
        "llm_config_id": run.llm_config_id,
        "llm_snapshot_version": run.llm_snapshot_version,
        "state": run.state,
        "stage": run.stage,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


async def _owned_run(
    db: AsyncSession, *, run_id: str, user_id: int, for_update: bool = False
) -> ReportRun:
    statement = select(ReportRun).where(ReportRun.id == run_id, ReportRun.user_id == user_id)
    if for_update:
        statement = statement.with_for_update()
    run = await db.scalar(statement)
    if run is None:
        raise HTTPException(status_code=404, detail="报告任务不存在")
    return run


@router.get("/report-templates")
async def list_report_templates(
    _user_id: Annotated[int, Depends(get_user_id)],
) -> list[dict[str, Any]]:
    try:
        return [template.to_public_dict() for template in report_template_registry.list()]
    except ReportTemplateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.post("/documents/{document_id}/reports", status_code=status.HTTP_202_ACCEPTED)
async def create_report(
    document_id: int,
    payload: ReportCreateRequest,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    document = await db.scalar(
        select(Document).where(Document.id == document_id, Document.user_id == user_id)
    )
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if (
        document.status != DOCUMENT_STATUS_READY
        or not document.parsed_bucket
        or not document.parsed_object_key
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REPORT_DOCUMENT_NOT_READY",
                "message": "文档当前版本尚未完成解析，不能生成报告",
            },
        )

    try:
        template = report_template_registry.get(payload.report_type)
    except ReportTemplateError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    custom_template = None
    custom_template_manifest = None
    if payload.custom_template_document_id is not None:
        if int(payload.custom_template_document_id) == int(document.id):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "CUSTOM_TEMPLATE_EQUALS_SOURCE",
                    "message": "源文件和报告模板必须是不同文档",
                },
            )
        custom_template = await db.scalar(
            select(Document).where(
                Document.id == payload.custom_template_document_id,
                Document.user_id == user_id,
                Document.dataset_id == document.dataset_id,
            )
        )
        if (
            custom_template is None
            or custom_template.status != DOCUMENT_STATUS_READY
            or not custom_template.parsed_bucket
            or not custom_template.parsed_object_key
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CUSTOM_TEMPLATE_NOT_READY",
                    "message": "上传的报告模板尚未完成解析，不能创建报告",
                },
            )
        custom_template_manifest = await load_document_chunk_manifest(
            db, document=custom_template
        )
        if not custom_template_manifest.items:
            raise HTTPException(
                status_code=409,
                detail={"code": "CUSTOM_TEMPLATE_EMPTY", "message": "上传模板没有可读内容"},
            )

    # 报告 Agent 必须在一轮会话里读完整个文档，超出模型上下文预算的任务注定截断失败。
    # 与其等几十分钟后报 REPORT_IR_NOT_SUBMITTED，不如在创建时就明确拒绝。
    source_chunks, source_chars = await load_document_payload_stats(
        db, document_id=int(document.id), document_version=int(document.version)
    )
    if custom_template is not None:
        custom_chunks, custom_chars = await load_document_payload_stats(
            db,
            document_id=int(custom_template.id),
            document_version=int(custom_template.version),
        )
    else:
        custom_chunks = custom_chars = 0
    try:
        estimated_tokens = assert_document_fits_context(
            content_chars=source_chars + custom_chars,
            chunk_count=source_chunks + custom_chunks,
        )
    except ReportDocumentTooLargeError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    logger.info(
        "报告任务上下文预算检查通过 run_estimate_tokens={} 分片数={}",
        estimated_tokens,
        source_chunks + custom_chunks,
    )

    llm_config = await db.scalar(
        select(LLMModelConfigDB).where(LLMModelConfigDB.id == payload.llm_config_id)
    )
    if (
        llm_config is None
        or not llm_config.is_active
        or llm_config.capability.upper() != "CHAT"
        or not llm_config.supports_tool_calling
        or (llm_config.scope == "USER" and int(llm_config.owner_user_id) != int(user_id))
        or llm_config.scope not in {"SYSTEM", "USER"}
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REPORT_MODEL_UNAVAILABLE",
                "message": "所选模型不可用、无权访问或不具备 CHAT 与工具调用能力",
            },
        )

    allowed_model_hosts = frozenset(
        item.strip().lower()
        for item in settings.REPORT_MODEL_ALLOWED_HOSTS.split(",")
        if item.strip()
    )
    try:
        validate_report_model_endpoint(
            llm_config.api_base_url,
            allowed_hosts=allowed_model_hosts,
            allow_private=settings.REPORT_MODEL_ALLOW_PRIVATE_ENDPOINTS,
        )
    except ReportModelEndpointError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "REPORT_MODEL_ENDPOINT_BLOCKED", "message": str(exc)},
        ) from exc

    now = utc_now()
    template_snapshot = template.to_snapshot()
    model_snapshot = {
        "llm_config_id": int(llm_config.id),
        "snapshot_version": int(llm_config.snapshot_version),
        "scope": llm_config.scope,
        "owner_user_id": llm_config.owner_user_id,
        "provider_type": llm_config.provider_type,
        "model_name": llm_config.model_name,
        "display_name": llm_config.display_name,
        "capability": llm_config.capability,
        "protocol": llm_config.protocol,
        "api_base_url": llm_config.api_base_url,
        "supports_tool_calling": bool(llm_config.supports_tool_calling),
    }
    run = ReportRun(
        id=str(uuid4()),
        user_id=user_id,
        dataset_id=document.dataset_id,
        document_id=document.id,
        document_version=document.version,
        parsed_bucket=document.parsed_bucket,
        parsed_object_key=document.parsed_object_key,
        report_type=template.report_type,
        template_id=template.template_id,
        template_version=template.version,
        template_snapshot=template_snapshot,
        model_snapshot=model_snapshot,
        document_manifest={},
        custom_template_document_id=custom_template.id if custom_template else None,
        custom_template_document_version=custom_template.version if custom_template else None,
        custom_template_manifest=(
            {
                "document_id": int(custom_template.id),
                "document_version": int(custom_template.version),
                "filename": custom_template.filename,
                "chunk_manifest_sha256": custom_template_manifest.content_hash,
                "chunk_count": len(custom_template_manifest.items),
            }
            if custom_template and custom_template_manifest
            else None
        ),
        mode="GENERATE",
        language=payload.language,
        reporting_year=payload.reporting_year,
        user_instructions=payload.user_instructions,
        output_formats=payload.output_formats,
        llm_config_id=llm_config.id,
        llm_snapshot_version=llm_config.snapshot_version,
        state="PENDING",
        stage="WAITING",
        input_hash="0" * 64,
        available_at=now,
    )
    _evidence_context, chunk_manifest = await load_report_source_context(db, run=run)
    if not chunk_manifest.items:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REPORT_DOCUMENT_CHUNKS_MISSING",
                "message": "文档当前版本没有可用于报告分析的稳定分片",
            },
        )
    run.document_manifest = {
        "parsed_bucket": document.parsed_bucket,
        "parsed_object_key": document.parsed_object_key,
        "document_version": int(document.version),
        "chunk_manifest_sha256": chunk_manifest.content_hash,
        "chunk_count": len(chunk_manifest.items),
    }
    run.input_hash = _input_hash(
        document=document,
        template_snapshot=template_snapshot,
        model_snapshot=model_snapshot,
        document_manifest=run.document_manifest,
        custom_template_manifest=run.custom_template_manifest,
        payload=payload,
    )
    mark_report_dispatch_pending(run, now=now)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    await ReportRunDispatcher().dispatch(run)
    return _run_dict(run)


@router.get("/report-runs/{run_id}")
async def get_report_run(
    run_id: str,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    return _run_dict(await _owned_run(db, run_id=run_id, user_id=user_id))


@router.get("/report-runs")
async def list_report_runs(
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """当前用户的报告任务总览（跨文档），带文档名与可下载产物。"""
    runs = (
        await db.scalars(
            select(ReportRun)
            .where(ReportRun.user_id == user_id)
            .order_by(ReportRun.created_at.desc())
            .limit(limit)
        )
    ).all()
    filenames = dict(
        (await db.execute(select(Document.id, Document.filename).where(Document.user_id == user_id)))
        .all()
    )
    return await _runs_with_artifacts(db, list(runs), document_filenames=filenames)


async def _runs_with_artifacts(
    db: AsyncSession,
    runs: list[ReportRun],
    *,
    document_filenames: dict[int, str],
) -> list[dict[str, Any]]:
    if not runs:
        return []
    runs_by_id = {str(run.id): run for run in runs}
    artifact_rows = (
        await db.scalars(
            select(ReportArtifact)
            .where(ReportArtifact.run_id.in_(list(runs_by_id)))
            .order_by(ReportArtifact.id)
        )
    ).all()
    artifacts_by_run: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifact_rows:
        run = runs_by_id.get(artifact.run_id)
        if run is None:
            continue
        artifacts_by_run.setdefault(artifact.run_id, []).append(
            {
                "id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "content_type": artifact.content_type,
                "content_hash": artifact.content_hash,
                "size_bytes": artifact.size_bytes,
                "filename": artifact_download_name(
                    run=run,
                    document_filename=document_filenames.get(int(run.document_id)),
                    artifact_type=artifact.artifact_type,
                ),
            }
        )
    return [
        {
            **_run_dict(run),
            "document_filename": document_filenames.get(int(run.document_id)),
            "artifacts": artifacts_by_run.get(str(run.id), []),
        }
        for run in runs
    ]


@router.get("/documents/{document_id}/report-runs")
async def list_document_report_runs(
    document_id: int,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict[str, Any]]:
    document_exists = await db.scalar(
        select(Document.id).where(Document.id == document_id, Document.user_id == user_id)
    )
    if document_exists is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    runs = (
        await db.scalars(
            select(ReportRun)
            .where(ReportRun.document_id == document_id, ReportRun.user_id == user_id)
            .order_by(ReportRun.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [_run_dict(run) for run in runs]


@router.post("/report-runs/{run_id}/cancel")
async def cancel_report_run(
    run_id: str,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    run = await _owned_run(db, run_id=run_id, user_id=user_id, for_update=True)
    if run.state not in {"PENDING", "PROCESSING", "NEEDS_INPUT"}:
        raise HTTPException(status_code=409, detail="当前报告任务不能取消")
    run.state = "CANCELLED"
    run.stage = "CANCELLED"
    run.finished_at = utc_now()
    run.available_at = None
    run.lease_token = None
    run.lease_owner = None
    run.lease_expires_at = None
    run.dispatch_status = "CANCELLED"
    run.dispatch_available_at = None
    await db.commit()
    await db.refresh(run)
    return _run_dict(run)


@router.post("/report-runs/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_report_run(
    run_id: str,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    run = await _owned_run(db, run_id=run_id, user_id=user_id, for_update=True)
    if run.state != "FAILED":
        raise HTTPException(status_code=409, detail="只有失败任务可以手动重试")
    document = await db.scalar(
        select(Document).where(Document.id == run.document_id, Document.user_id == user_id)
    )
    if (
        document is None
        or document.status != DOCUMENT_STATUS_READY
        or int(document.version) != int(run.document_version)
        or document.parsed_bucket != run.parsed_bucket
        or document.parsed_object_key != run.parsed_object_key
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REPORT_DOCUMENT_VERSION_CHANGED",
                "message": "源文档版本已经变化，请创建新的报告任务",
            },
        )
    now = utc_now()
    run.state = "PENDING"
    run.stage = "MANUAL_RETRY"
    run.attempt_count = 0
    run.available_at = now
    run.finished_at = None
    run.error_code = None
    run.error_message = None
    mark_report_dispatch_pending(run, now=now)
    await db.commit()
    await db.refresh(run)
    await ReportRunDispatcher().dispatch(run)
    return _run_dict(run)


@router.get("/report-runs/{run_id}/questions")
async def list_report_questions(
    run_id: str,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    run = await _owned_run(db, run_id=run_id, user_id=user_id)
    template = ReportTemplate.from_snapshot(run.template_snapshot)
    fields = {str(field["id"]): field for field in template.definition.get("fields") or []}
    questions = (
        await db.scalars(
            select(ReportQuestion)
            .where(ReportQuestion.run_id == run_id)
            .order_by(ReportQuestion.id)
        )
    ).all()
    return [
        {
            "question_id": question.id,
            "field_id": question.field_id,
            "field_type": fields.get(question.field_id, {}).get("type"),
            "field_unit": fields.get(question.field_id, {}).get("unit"),
            "field_validation": fields.get(question.field_id, {}).get("validation") or {},
            "question_type": question.question_type,
            "question": question.question,
            "options": question.options,
            "required": question.required,
            "status": question.status,
            "answer": question.answer,
            "answered_at": question.answered_at,
        }
        for question in questions
    ]


@router.post("/report-runs/{run_id}/answers", status_code=status.HTTP_202_ACCEPTED)
async def answer_report_questions(
    run_id: str,
    payload: ReportAnswersRequest,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    run = await _owned_run(db, run_id=run_id, user_id=user_id, for_update=True)
    if run.state != "NEEDS_INPUT":
        raise HTTPException(
            status_code=409,
            detail={"code": "REPORT_NOT_WAITING_FOR_INPUT", "message": "任务当前无需补充信息"},
        )
    ids = [answer.question_id for answer in payload.answers]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=422, detail="补充问题不能重复")
    questions = (
        await db.scalars(
            select(ReportQuestion)
            .where(ReportQuestion.run_id == run_id, ReportQuestion.id.in_(ids))
            .with_for_update()
        )
    ).all()
    by_id = {int(question.id): question for question in questions}
    if set(by_id) != set(ids) or any(question.status != "OPEN" for question in questions):
        raise HTTPException(status_code=409, detail="补充问题不存在或已经回答")
    template = ReportTemplate.from_snapshot(run.template_snapshot)
    fields = {str(field["id"]): field for field in template.definition.get("fields") or []}
    for answer in payload.answers:
        question = by_id[answer.question_id]
        field = fields.get(question.field_id)
        value_errors = (
            validate_report_field_value(field, answer.value)
            if field is not None
            else ["问题字段不属于冻结模板"]
        )
        if value_errors:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "REPORT_ANSWER_INVALID",
                    "field_id": question.field_id,
                    "errors": value_errors,
                },
            )
    now = utc_now()
    for answer in payload.answers:
        question = by_id[answer.question_id]
        question.answer = {"value": answer.value, "notes": answer.notes}
        question.status = "ANSWERED"
        question.answered_by = user_id
        question.answered_at = now

    remaining = await db.scalar(
        select(func.count(ReportQuestion.id)).where(
            ReportQuestion.run_id == run_id,
            ReportQuestion.required.is_(True),
            ReportQuestion.status == "OPEN",
            ReportQuestion.id.not_in(ids),
        )
    )
    if int(remaining or 0) == 0:
        run.state = "PENDING"
        run.stage = "RESUMING"
        run.available_at = now
        run.error_code = None
        run.error_message = None
        mark_report_dispatch_pending(run, now=now)
    await db.commit()
    await db.refresh(run)
    if run.state == "PENDING":
        await ReportRunDispatcher().dispatch(run)
    return _run_dict(run)


@router.get("/report-runs/{run_id}/report")
async def get_report_ir(
    run_id: str,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    run = await _owned_run(db, run_id=run_id, user_id=user_id)
    if run.state != "SUCCEEDED" or run.report_ir is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "REPORT_NOT_READY", "message": "报告尚未生成完成"},
        )
    artifacts = (
        await db.scalars(
            select(ReportArtifact)
            .where(ReportArtifact.run_id == run_id)
            .order_by(ReportArtifact.id)
        )
    ).all()
    document = await db.scalar(select(Document).where(Document.id == run.document_id))
    document_filename = document.filename if document is not None else None
    return {
        "run": _run_dict(run),
        "report_ir": run.report_ir,
        "validation_report": run.validation_report,
        "manifest": run.manifest,
        "artifacts": [
            {
                "id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "content_type": artifact.content_type,
                "content_hash": artifact.content_hash,
                "size_bytes": artifact.size_bytes,
                "filename": artifact_download_name(
                    run=run,
                    document_filename=document_filename,
                    artifact_type=artifact.artifact_type,
                ),
            }
            for artifact in artifacts
        ],
    }


@router.get("/report-runs/{run_id}/artifacts/{artifact_id}")
async def download_report_artifact(
    run_id: str,
    artifact_id: int,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """下载已生成的报告产物（Markdown / DOCX）。"""
    run = await _owned_run(db, run_id=run_id, user_id=user_id)
    artifact = await db.scalar(
        select(ReportArtifact).where(
            ReportArtifact.id == artifact_id,
            ReportArtifact.run_id == str(run.id),
        )
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="报告产物不存在")
    document = await db.scalar(select(Document).where(Document.id == run.document_id))
    try:
        content = await read_report_artifact(artifact)
    except ReportArtifactError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "REPORT_ARTIFACT_UNAVAILABLE", "message": str(exc)},
        ) from exc
    filename = artifact_download_name(
        run=run,
        document_filename=document.filename if document is not None else None,
        artifact_type=artifact.artifact_type,
    )
    return StreamingResponse(
        BytesIO(content),
        media_type=artifact.content_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename=report.{artifact.artifact_type.lower()}; "
                f"filename*=UTF-8''{quote(filename, safe='')}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
