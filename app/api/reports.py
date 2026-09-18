"""Versioned report task APIs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from io import BytesIO
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import get_user_id
from app.domain.models import (
    AgentConversationTurn,
    Document,
    ReportArtifact,
    ReportMaterial,
    ReportQuestion,
    ReportRun,
)
from app.domain.time import as_utc, utc_now
from app.rag.config import settings
from app.rag.database import get_db
from app.rag.models.db_models import LLMModelConfigDB
from app.rag.observability.logging import logger
from app.rag.services.storage.base import BaseObjectStorage
from app.rag.services.storage.factory import StorageFactory
from app.services.document_queue import DOCUMENT_STATUS_READY
from app.services.report_artifacts import (
    ReportArtifactError,
    artifact_download_name,
    purge_report_artifacts,
    read_report_artifact,
)
from app.services.report_budget import (
    ReportDocumentTooLargeError,
    assert_document_fits_context,
    load_document_payload_stats,
)
from app.services.report_dispatch import ReportRunDispatcher, mark_report_dispatch_pending
from app.services.report_inline_source import (
    SOURCE_KIND_DOCUMENT,
    SOURCE_KIND_INLINE,
    canonical_hash,
    inline_source_object_key,
    manifest_items,
    payload_chunks,
    serialize_inline_source,
    write_inline_source,
)
from app.services.report_ir import validate_report_field_value
from app.services.report_material import read_material_text
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
    output_formats: list[Literal["ONLINE", "MARKDOWN", "DOCX", "HTML"]] = Field(
        default_factory=lambda: ["ONLINE", "DOCX", "HTML"]
    )
    custom_template_document_id: int | None = Field(default=None, gt=0)
    # 对话直传的材料 id。与路径上的 document_id 二选一；两者都给或都没给都会被拒。
    material_id: str | None = Field(default=None, min_length=1, max_length=36)
    # 对话直传的版式模板 id。与 custom_template_document_id 二选一。
    template_material_id: str | None = Field(default=None, min_length=1, max_length=36)

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
    source: _ReportSource,
    template_snapshot: dict[str, Any],
    model_snapshot: dict[str, Any],
    document_manifest: dict[str, Any],
    custom_template_manifest: dict[str, Any] | None,
    payload: ReportCreateRequest,
) -> str:
    frozen = {
        "source_kind": source.kind,
        "document_id": source.document_id,
        "document_version": source.document_version,
        "parsed_bucket": source.bucket,
        "parsed_object_key": source.object_key,
        "template_asset_hash": template_snapshot["asset_hash"],
        "model_snapshot": model_snapshot,
        "document_manifest": document_manifest,
        "custom_template_manifest": custom_template_manifest,
        "request": payload.model_dump(mode="json"),
    }
    encoded = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _run_dict(run: ReportRun) -> dict[str, Any]:
    snapshot = run.template_snapshot if isinstance(run.template_snapshot, dict) else {}
    return {
        "run_id": run.id,
        "document_id": run.document_id,
        "dataset_id": run.dataset_id,
        "document_version": run.document_version,
        "report_type": run.report_type,
        # 面向用户展示的名称。R1–R7 是内部编号，不直接呈现给使用者。
        "report_type_name": str(snapshot.get("name") or run.report_type),
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
        "started_at": as_utc(run.started_at),
        "finished_at": as_utc(run.finished_at),
        "created_at": as_utc(run.created_at),
        "updated_at": as_utc(run.updated_at),
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


@dataclass(frozen=True, slots=True)
class _ReportSource:
    """创建任务时解析出的来源。两种来源在这里归一，下游不必再分叉。

    ``bucket`` / ``object_key`` 对文档来源是解析产物，对直传材料是冻结下来的分片
    JSON——语义都是「来源正文放在哪」。
    """

    kind: str
    dataset_id: int | None
    document_id: int | None
    document_version: int | None
    bucket: str
    object_key: str
    filename: str | None
    chunks: int
    chars: int
    document: Document | None = None
    material: ReportMaterial | None = None


async def _ready_document(db: AsyncSession, *, document_id: int | None, user_id: int) -> Document:
    if document_id is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "REPORT_SOURCE_REQUIRED", "message": "缺少报告来源"},
        )
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
    return document


async def _claimed_material(
    db: AsyncSession, *, material_id: str, user_id: int
) -> ReportMaterial:
    """按 id 取回暂存材料。

    刻意**不**因为材料已经用过就拒绝：材料与模板都挂在对话上，一份材料可能被同一段
    对话里的多次生成反复引用，这是预期用法。真正决定它还能不能用的是保留期。
    """
    material = await db.scalar(
        select(ReportMaterial)
        .where(ReportMaterial.id == material_id, ReportMaterial.user_id == user_id)
        .with_for_update()
    )
    if material is None:
        raise HTTPException(status_code=404, detail="上传的材料不存在或已被清理")
    return material


async def _inline_template_manifest(
    storage: BaseObjectStorage,
    *,
    material: ReportMaterial,
    text: str,
    run_id: str,
) -> dict[str, Any]:
    """把对话直传的版式模板冻结到本次任务上，返回写进 ``custom_template_manifest`` 的描述。

    与来源正文用同一套切分与清单：模板同样要能被 agent 按游标读完并给出覆盖记录，
    否则「照这份模板写」这件事无法证明。文档模板与内联模板在同一个字段里用
    ``source_kind`` 区分，读取方按它分派。
    """
    payload = serialize_inline_source(filename=material.filename, text=text)
    bucket = settings.MINIO_PRIVATE_BUCKET
    object_key = inline_source_object_key(
        user_id=int(material.user_id),
        run_id=run_id,
        digest=material.content_hash,
        kind="template",
    )
    await write_inline_source(storage, bucket=bucket, object_key=object_key, payload=payload)
    items = manifest_items(payload_chunks(payload))
    return {
        "source_kind": SOURCE_KIND_INLINE,
        "filename": material.filename,
        "bucket": bucket,
        "object_key": object_key,
        "chunk_manifest_sha256": canonical_hash(items),
        "chunk_count": len(items),
    }


async def _document_source(db: AsyncSession, *, document: Document) -> _ReportSource:
    chunks, chars = await load_document_payload_stats(
        db, document_id=int(document.id), document_version=int(document.version)
    )
    return _ReportSource(
        kind=SOURCE_KIND_DOCUMENT,
        dataset_id=int(document.dataset_id),
        document_id=int(document.id),
        document_version=int(document.version),
        bucket=document.parsed_bucket,
        object_key=document.parsed_object_key,
        filename=document.filename,
        chunks=chunks,
        chars=chars,
        document=document,
    )


async def _material_source(
    storage: BaseObjectStorage,
    *,
    material: ReportMaterial,
    text: str,
    run_id: str,
) -> _ReportSource:
    """把提取出的文本切分并冻结成这次任务专用的来源，然后删掉暂存对象。

    冻结而不是引暂存对象：报告任务要对「当时读到的正文」负责，暂存对象是可以被
    清理的，两者生命周期不同。
    """
    payload = serialize_inline_source(filename=material.filename, text=text)
    object_key = inline_source_object_key(
        user_id=int(material.user_id), run_id=run_id, digest=material.content_hash
    )
    await write_inline_source(
        storage,
        bucket=settings.MINIO_PRIVATE_BUCKET,
        object_key=object_key,
        payload=payload,
    )
    return _ReportSource(
        kind=SOURCE_KIND_INLINE,
        dataset_id=None,
        document_id=None,
        document_version=None,
        bucket=settings.MINIO_PRIVATE_BUCKET,
        object_key=object_key,
        filename=material.filename,
        chunks=int(payload["chunk_count"]),
        chars=int(payload["char_count"]),
        material=material,
    )


@router.post("/documents/{document_id}/reports", status_code=status.HTTP_202_ACCEPTED)
async def create_report(
    document_id: int | None,
    payload: ReportCreateRequest,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """创建报告任务。

    来源二选一：路径上的 ``document_id``（知识库文档），或 ``payload.material_id``
    （对话里直传、未入库的材料）。两者都给定或都没给都会被拒。
    """
    if payload.material_id is not None and document_id is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "REPORT_SOURCE_CONFLICT",
                "message": "文档来源与直传材料只能二选一",
            },
        )

    try:
        template = report_template_registry.get(payload.report_type)
    except ReportTemplateError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    run_id = str(uuid4())
    object_storage = StorageFactory.get_storage()
    document: Document | None = None
    source_material: ReportMaterial | None = None
    if payload.material_id is not None:
        source_material = await _claimed_material(
            db, material_id=payload.material_id, user_id=user_id
        )
        material_text = (
            await read_material_text(object_storage, material=source_material)
        ).decode("utf-8")
        source = await _material_source(
            object_storage, material=source_material, text=material_text, run_id=run_id
        )
    else:
        document = await _ready_document(db, document_id=document_id, user_id=user_id)
        source = await _document_source(db, document=document)

    custom_template = None
    custom_template_manifest = None
    template_material: ReportMaterial | None = None
    template_chunks = template_chars = 0
    if payload.template_material_id is not None:
        if payload.custom_template_document_id is not None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "CUSTOM_TEMPLATE_CONFLICT",
                    "message": "版式模板二选一：知识库文档或对话直传的材料",
                },
            )
        template_material = await _claimed_material(
            db, material_id=payload.template_material_id, user_id=user_id
        )
        if source_material is not None and template_material.id == source_material.id:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "CUSTOM_TEMPLATE_EQUALS_SOURCE",
                    "message": "来源材料和版式模板不能是同一份文件",
                },
            )
        template_text = (
            await read_material_text(object_storage, material=template_material)
        ).decode("utf-8")
        custom_template_manifest = await _inline_template_manifest(
            object_storage, material=template_material, text=template_text, run_id=run_id
        )
        template_chunks = int(custom_template_manifest["chunk_count"])
        template_chars = len(template_text)
    elif payload.custom_template_document_id is not None:
        if document is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "CUSTOM_TEMPLATE_REQUIRES_DOCUMENT",
                    "message": (
                        "知识库模板只能配知识库来源；来源是直传材料时请改用 "
                        "template_material_id"
                    ),
                },
            )
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
        document_template_manifest = await load_document_chunk_manifest(
            db, document=custom_template
        )
        if not document_template_manifest.items:
            raise HTTPException(
                status_code=409,
                detail={"code": "CUSTOM_TEMPLATE_EMPTY", "message": "上传模板没有可读内容"},
            )
        custom_template_manifest = {
            "source_kind": SOURCE_KIND_DOCUMENT,
            "document_id": int(custom_template.id),
            "document_version": int(custom_template.version),
            "filename": custom_template.filename,
            "chunk_manifest_sha256": document_template_manifest.content_hash,
            "chunk_count": len(document_template_manifest.items),
        }
        template_chunks, template_chars = await load_document_payload_stats(
            db,
            document_id=int(custom_template.id),
            document_version=int(custom_template.version),
        )

    # 报告 Agent 必须在一轮会话里读完整个来源，超出模型上下文预算的任务注定截断失败。
    # 与其等几十分钟后报 REPORT_IR_NOT_SUBMITTED，不如在创建时就明确拒绝。
    try:
        estimated_tokens = assert_document_fits_context(
            content_chars=source.chars + template_chars,
            chunk_count=source.chunks + template_chunks,
        )
    except ReportDocumentTooLargeError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    logger.info(
        "报告任务上下文预算检查通过 run_estimate_tokens={} 分片数={}",
        estimated_tokens,
        source.chunks + template_chunks,
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
        id=run_id,
        user_id=user_id,
        source_kind=source.kind,
        dataset_id=source.dataset_id,
        document_id=source.document_id,
        document_version=source.document_version,
        inline_source_filename=(
            source.filename if source.kind == SOURCE_KIND_INLINE else None
        ),
        parsed_bucket=source.bucket,
        parsed_object_key=source.object_key,
        report_type=template.report_type,
        template_id=template.template_id,
        template_version=template.version,
        template_snapshot=template_snapshot,
        model_snapshot=model_snapshot,
        document_manifest={},
        custom_template_document_id=custom_template.id if custom_template else None,
        custom_template_document_version=custom_template.version if custom_template else None,
        custom_template_manifest=custom_template_manifest,
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
        "source_kind": source.kind,
        "parsed_bucket": source.bucket,
        "parsed_object_key": source.object_key,
        "document_version": source.document_version,
        "source_filename": source.filename,
        "chunk_manifest_sha256": chunk_manifest.content_hash,
        "chunk_count": len(chunk_manifest.items),
        # 直传材料用它做预算校验；文档来源的规模每次从 document_chunk 现算。
        "char_count": source.chars,
    }
    run.input_hash = _input_hash(
        source=source,
        template_snapshot=template_snapshot,
        model_snapshot=model_snapshot,
        document_manifest=run.document_manifest,
        custom_template_manifest=run.custom_template_manifest,
        payload=payload,
    )
    if source.material is not None:
        source.material.consumed_at = now
    if template_material is not None:
        # 记下首次被使用的时间，便于诊断；不影响后续复用（材料挂在对话上，可反复引用）。
        template_material.consumed_at = template_material.consumed_at or now
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
    filename_query = select(Document.id, Document.filename).where(Document.user_id == user_id)
    filenames = dict((await db.execute(filename_query)).all())
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
                    document_filename=_source_filename(run, document_filenames),
                    artifact_type=artifact.artifact_type,
                ),
            }
        )
    return [
        {
            **_run_dict(run),
            "document_filename": _source_filename(run, document_filenames),
            "artifacts": artifacts_by_run.get(str(run.id), []),
        }
        for run in runs
    ]


def _source_filename(run: ReportRun, document_filenames: dict[int, str]) -> str | None:
    """列表里的「来源」列：文档来源取文档名，直传材料取上传时的原文件名。"""
    if run.source_kind == SOURCE_KIND_INLINE:
        return run.inline_source_filename
    if run.document_id is None:
        return None
    return document_filenames.get(int(run.document_id))


async def _resolve_source_filename(db: AsyncSession, run: ReportRun) -> str | None:
    """单个任务的来源名。列表接口有现成的文件名映射，单条查询就直接查库。"""
    if run.source_kind == SOURCE_KIND_INLINE:
        return run.inline_source_filename
    if run.document_id is None:
        return None
    document = await db.scalar(
        select(Document).where(Document.id == run.document_id, Document.user_id == run.user_id)
    )
    return document.filename if document is not None else None


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


@router.delete("/report-runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_report_run(
    run_id: str,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """删除一个报告任务，连同其产物、澄清问题与对话里的软引用。

    进行中的任务不能直接删：worker 仍持有租约并会回写该行，请先取消。
    """

    run = await _owned_run(db, run_id=run_id, user_id=user_id)
    if run.state in {"PENDING", "PROCESSING"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REPORT_RUN_ACTIVE", "message": "任务正在进行，请先取消再删除"},
        )

    await purge_report_artifacts(db, run_id=run_id)
    await db.execute(sa_delete(ReportQuestion).where(ReportQuestion.run_id == run_id))
    # 对话回合只持有 report_run_id 这个软引用。不清掉的话，聊天里会留下一张
    # 指向已删报告的空卡片，点开只会报错。
    await db.execute(
        update(AgentConversationTurn)
        .where(AgentConversationTurn.report_run_id == run_id)
        .values(report_run_id=None)
    )
    await db.execute(sa_delete(ReportRun).where(ReportRun.id == run_id))
    await db.commit()


@router.post("/report-runs/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_report_run(
    run_id: str,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    run = await _owned_run(db, run_id=run_id, user_id=user_id, for_update=True)
    if run.state != "FAILED":
        raise HTTPException(status_code=409, detail="只有失败任务可以手动重试")
    # 直传材料的正文是创建任务时冻结的快照，外部改不到，也就没有「文档版本已变化」可判。
    if run.source_kind != SOURCE_KIND_INLINE:
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
            "field_label": fields.get(question.field_id, {}).get("label") or question.field_id,
            "field_type": fields.get(question.field_id, {}).get("type"),
            "field_unit": fields.get(question.field_id, {}).get("unit"),
            "field_validation": fields.get(question.field_id, {}).get("validation") or {},
            "question_type": question.question_type,
            "question": question.question,
            "options": question.options,
            "required": question.required,
            "status": question.status,
            "answer": question.answer,
            "answered_at": as_utc(question.answered_at),
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
    document_filename = await _resolve_source_filename(db, run)
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
    """下载已生成的报告产物（Markdown / DOCX / HTML）。"""
    run = await _owned_run(db, run_id=run_id, user_id=user_id)
    artifact = await db.scalar(
        select(ReportArtifact).where(
            ReportArtifact.id == artifact_id,
            ReportArtifact.run_id == str(run.id),
        )
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="报告产物不存在")
    try:
        content = await read_report_artifact(artifact)
    except ReportArtifactError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "REPORT_ARTIFACT_UNAVAILABLE", "message": str(exc)},
        ) from exc
    filename = artifact_download_name(
        run=run,
        document_filename=await _resolve_source_filename(db, run),
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
