"""Lease-bound internal tools exposed only to the Pi report service."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import ReportQuestion, ReportRun
from app.domain.time import utc_now
from app.rag.config import settings
from app.rag.database import get_db
from app.rag.models.chunk_record import ChunkRecordDB
from app.services.report_agent_tokens import (
    ReportAgentTokenError,
    verify_report_agent_token,
)
from app.services.report_calculations import ReportCalculationError, execute_registered_formula
from app.services.report_budget import STRUCTURE_HINT_CHARS_PER_CHUNK
from app.services.report_ir import report_ir_contract_schema, validate_report_ir
from app.services.report_source_context import (
    load_report_source_context,
    validate_chunk_coverage,
)
from app.services.report_templates import ReportTemplate, report_template_registry

router = APIRouter(prefix="/internal/report-agent", tags=["报告 Agent 内部接口"])


class ChunkPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cursor: str | None = Field(default=None, pattern=r"^\d+$", max_length=20)
    limit: int = Field(default=20, ge=1, le=50)


class ReferenceSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=20)


class CalculationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    formula_id: str = Field(min_length=1, max_length=128)
    inputs: dict[str, Any]
    parameters: dict[str, Any] = Field(default_factory=dict)
    parameter_evidence_ids: list[str] = Field(default_factory=list, max_length=100)


class CheckpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str = Field(min_length=1, max_length=64)
    evidence: list[dict[str, Any]] = Field(max_length=10000)
    field_ledger: list[dict[str, Any]] = Field(max_length=1000)


class ClarificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_id: str = Field(min_length=1, max_length=128)
    question_type: Literal["MISSING_FIELD", "CONFLICT", "CONFIRMATION"]
    question: str = Field(min_length=1, max_length=1000)
    required: bool = True
    options: list[dict[str, Any]] | None = Field(default=None, max_length=100)


class ClarificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[ClarificationItem] = Field(min_length=1, max_length=100)


class ReportIRRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_ir: dict[str, Any]


class ReportIRValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 不带 report_ir 时校验已落库的草稿（save_report_ir_draft 写入）。
    report_ir: dict[str, Any] | None = None


class ReportIRSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coverage: dict[str, Any]
    # 兼容旧契约：不带 report_ir 时提交已落库的草稿。
    report_ir: dict[str, Any] | None = None


def _answer_content_hash(answer: dict[str, Any] | None) -> str | None:
    if answer is None:
        return None
    encoded = json.dumps(
        answer.get("value"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_STRUCTURE_HINT_KEYS = (
    "chunk_role",
    "element_types",
    "split_strategy",
    "oversized",
    "oversized_reason",
)
_TABLE_HINT_KEYS = ("title", "row_count", "column_count", "header_row_count", "source_pages")

# 单次校验回传给模型的最大错误/警告条数。
MAX_VALIDATION_ITEMS = 40


def _trim_validation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """限制回传给模型的校验错误条数。

    一次严重不完整的 ReportIR 可能触发成百上千条错误，逐条回传会把这些文本
    永久留在模型上下文里（并挤掉后续修复所需的空间）。
    """
    trimmed = dict(payload)
    for key in ("errors", "warnings"):
        items = payload.get(key) or []
        if len(items) > MAX_VALIDATION_ITEMS:
            trimmed[key] = [
                *items[:MAX_VALIDATION_ITEMS],
                f"（其余 {len(items) - MAX_VALIDATION_ITEMS} 条同类信息已省略）",
            ]
    return trimmed


def _require_report_ir(payload_ir: dict[str, Any] | None, run: ReportRun) -> dict[str, Any]:
    """取本次要校验/提交的 ReportIR：请求体优先，否则用已落库的草稿。"""
    candidate = payload_ir if payload_ir is not None else run.report_ir_draft
    if not isinstance(candidate, dict):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REPORT_IR_DRAFT_MISSING",
                "message": "尚未落库候选 ReportIR，请先调用 save_report_ir_draft",
            },
        )
    return candidate


def _structure_hint(raw: Any) -> dict[str, Any]:
    """把分片的结构元数据裁剪成「轻量提示」再交给报告 Agent。

    原始结构元数据整篇可达正文的 6 倍以上（表格的 cells/text_matrix/cell_reference_matrix
    逐格序列化，单条上限 4 万字符），而这些内容在分片正文里已经存在：原样下发会让
    一次 read_document_chunks 就返回几十万字符，把模型上下文窗口吃满。这里只保留
    定位与类型提示，表格只留行列数与标题。
    """
    if not isinstance(raw, dict):
        return {}
    hint = {key: raw[key] for key in _STRUCTURE_HINT_KEYS if raw.get(key) is not None}
    trail = raw.get("heading_trail")
    if isinstance(trail, list) and trail:
        hint["heading_trail"] = trail[-2:]
    table = raw.get("table_structure")
    if isinstance(table, dict):
        hint["table"] = {key: table[key] for key in _TABLE_HINT_KEYS if table.get(key) is not None}
    return hint


def _chunk_page(rows: list[Any], *, page_limit: int) -> tuple[list[Any], bool]:
    """按字符预算截取一页分片，返回 (本页分片, 是否还有后续)。

    分片正文长度差异极大（实测单条最长 1.7 万字符），只按条数分页会让一次返回达到
    几十万字符、把模型上下文一次性吃满。至少返回 1 条，保证游标一定能前进。
    """
    budget = settings.REPORT_AGENT_CHUNK_PAGE_MAX_CHARS
    selected: list[Any] = []
    used = 0
    for row in rows[:page_limit]:
        cost = len(row.content or "") + STRUCTURE_HINT_CHARS_PER_CHUNK
        if selected and used + cost > budget:
            break
        selected.append(row)
        used += cost
    return selected, len(rows) > len(selected)


def _verify_service_token(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = settings.REPORT_AGENT_INTERNAL_TOKEN
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization[7:].strip()
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail={"code": "REPORT_AGENT_UNAUTHORIZED"})


async def _authorized_run(
    run_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _service: Annotated[None, Depends(_verify_service_token)],
    run_token: Annotated[str | None, Header(alias="X-Report-Run-Token")] = None,
) -> ReportRun:
    try:
        claims = verify_report_agent_token(
            run_token or "",
            run_id=run_id,
            secret=settings.REPORT_AGENT_RUN_TOKEN_SECRET,
        )
    except ReportAgentTokenError as exc:
        raise HTTPException(
            status_code=401, detail={"code": "REPORT_AGENT_RUN_TOKEN_INVALID"}
        ) from exc
    run = await db.scalar(
        select(ReportRun).where(
            ReportRun.id == run_id,
            ReportRun.state == "PROCESSING",
            ReportRun.lease_token == claims.lease_token,
            ReportRun.lease_expires_at > utc_now(),
        )
    )
    if run is None:
        raise HTTPException(status_code=409, detail={"code": "REPORT_AGENT_RUN_LEASE_LOST"})
    return run


def _template_for_run(run: ReportRun) -> ReportTemplate:
    template = ReportTemplate.from_snapshot(run.template_snapshot)
    if template.template_id != run.template_id or template.version != run.template_version:
        raise HTTPException(status_code=409, detail={"code": "REPORT_AGENT_TEMPLATE_CHANGED"})
    return template


@router.get("/readiness", dependencies=[Depends(_verify_service_token)])
async def report_agent_readiness() -> dict[str, Any]:
    tokens_ready = all(
        len(value) >= 32
        for value in (
            settings.REPORT_AGENT_INTERNAL_TOKEN,
            settings.REPORT_AGENT_RUN_TOKEN_SECRET,
        )
    )
    templates_ready = len(report_template_registry.list()) == 7
    return {
        "ready": tokens_ready and templates_ready,
        "templates_ready": templates_ready,
        "tokens_ready": tokens_ready,
    }


@router.get("/runs/{run_id}/context")
async def get_analysis_context(run: Annotated[ReportRun, Depends(_authorized_run)]) -> dict:
    return {
        "run_id": run.id,
        "user_id": run.user_id,
        "dataset_id": run.dataset_id,
        "document_id": run.document_id,
        "document_version": run.document_version,
        "report_type": run.report_type,
        "template_id": run.template_id,
        "template_version": run.template_version,
        "language": run.language,
        "reporting_year": run.reporting_year,
        "user_instructions": run.user_instructions,
        "llm_config_id": run.llm_config_id,
        "llm_snapshot_version": run.llm_snapshot_version,
        "document_manifest": run.document_manifest,
        "custom_template": run.custom_template_manifest,
        "budgets": {
            "max_tool_calls": settings.REPORT_AGENT_MAX_TOOL_CALLS,
            "max_chunk_page_size": 50,
        },
    }


@router.get("/runs/{run_id}/clarifications")
async def get_run_clarifications(
    run: Annotated[ReportRun, Depends(_authorized_run)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    questions = (
        await db.scalars(
            select(ReportQuestion)
            .where(ReportQuestion.run_id == run.id)
            .order_by(ReportQuestion.id)
        )
    ).all()
    return {
        "items": [
            {
                "question_id": int(question.id),
                "field_id": question.field_id,
                "question_type": question.question_type,
                "question": question.question,
                "required": bool(question.required),
                "status": question.status,
                "answer": question.answer,
                "answer_content_hash": _answer_content_hash(question.answer),
                "answered_by": question.answered_by,
                "answered_at": (
                    question.answered_at.isoformat() if question.answered_at is not None else None
                ),
                "evidence_source_type": "USER_INPUT" if question.answer is not None else None,
            }
            for question in questions
        ],
        "answered_count": sum(question.status == "ANSWERED" for question in questions),
        "open_count": sum(question.status == "OPEN" for question in questions),
    }


@router.get("/runs/{run_id}/template")
async def get_template_definition(
    run: Annotated[ReportRun, Depends(_authorized_run)],
) -> dict:
    template = _template_for_run(run)
    return {
        "report_type": template.report_type,
        "template_id": template.template_id,
        "template_version": template.version,
        "definition": template.definition,
        # 与校验器同源的 ReportIR JSON Schema：Agent 必须按其中的字段名/枚举构造 IR。
        "ir_schema": report_ir_contract_schema(),
        "common_skill": template.common_skill,
        "report_skill": template.report_skill,
    }


@router.post("/runs/{run_id}/document-chunks")
async def read_document_chunks(
    payload: ChunkPageRequest,
    run: Annotated[ReportRun, Depends(_authorized_run)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    offset = int(payload.cursor or 0)
    rows = (
        await db.scalars(
            select(ChunkRecordDB)
            .where(
                ChunkRecordDB.doc_id == run.document_id,
                ChunkRecordDB.document_version == run.document_version,
                ChunkRecordDB.user_id == run.user_id,
                ChunkRecordDB.set_id == run.dataset_id,
            )
            .order_by(ChunkRecordDB.chunk_index, ChunkRecordDB.id)
            .offset(offset)
            .limit(payload.limit + 1)
        )
    ).all()
    items, has_more = _chunk_page(rows, page_limit=payload.limit)
    return {
        "items": [
            {
                "chunk_id": row.chunk_id,
                "chunk_index": row.chunk_index,
                "chunk_type": row.chunk_type,
                "content": row.content,
                "content_hash": row.content_hash,
                "start_page": row.start_page,
                "end_page": row.end_page,
                "start_line": row.start_line,
                "end_line": row.end_line,
                "structure": _structure_hint(row.structure_metadata),
            }
            for row in items
        ],
        "next_cursor": str(offset + len(items)) if has_more else None,
        "complete": not has_more,
        "manifest_hash": run.document_manifest["chunk_manifest_sha256"],
        "total_chunks": run.document_manifest["chunk_count"],
    }


@router.post("/runs/{run_id}/custom-template-chunks")
async def read_custom_template_chunks(
    payload: ChunkPageRequest,
    run: Annotated[ReportRun, Depends(_authorized_run)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    manifest = run.custom_template_manifest
    if (
        not run.custom_template_document_id
        or not run.custom_template_document_version
        or not manifest
    ):
        return {"items": [], "next_cursor": None, "complete": True, "available": False}
    offset = int(payload.cursor or 0)
    rows = (
        await db.scalars(
            select(ChunkRecordDB)
            .where(
                ChunkRecordDB.doc_id == run.custom_template_document_id,
                ChunkRecordDB.document_version == run.custom_template_document_version,
                ChunkRecordDB.user_id == run.user_id,
                ChunkRecordDB.set_id == run.dataset_id,
            )
            .order_by(ChunkRecordDB.chunk_index, ChunkRecordDB.id)
            .offset(offset)
            .limit(payload.limit + 1)
        )
    ).all()
    items, has_more = _chunk_page(rows, page_limit=payload.limit)
    return {
        "available": True,
        "filename": manifest.get("filename"),
        "items": [
            {
                "chunk_id": row.chunk_id,
                "chunk_index": row.chunk_index,
                "content": row.content,
                "content_hash": row.content_hash,
                "structure": _structure_hint(row.structure_metadata),
            }
            for row in items
        ],
        "next_cursor": str(offset + len(items)) if has_more else None,
        "complete": not has_more,
        "manifest_hash": manifest["chunk_manifest_sha256"],
        "total_chunks": manifest["chunk_count"],
    }


@router.post("/runs/{run_id}/references/search")
async def search_reference_knowledge(
    payload: ReferenceSearchRequest,
    _run: Annotated[ReportRun, Depends(_authorized_run)],
) -> dict:
    return {
        "query": payload.query,
        "hits": [],
        "available": False,
        "warning": "专用规范知识库尚未配置；不得据此编造法规、因子或强制要求。",
    }


@router.post("/runs/{run_id}/calculate")
async def calculate_report_metrics(
    payload: CalculationRequest,
    run: Annotated[ReportRun, Depends(_authorized_run)],
) -> dict:
    template = _template_for_run(run)
    formula = next(
        (
            item
            for item in template.definition.get("calculations") or []
            if item.get("formula_id") == payload.formula_id
        ),
        None,
    )
    if formula is None:
        raise HTTPException(status_code=422, detail={"code": "REPORT_FORMULA_NOT_REGISTERED"})
    try:
        value = execute_registered_formula(
            formula,
            inputs=payload.inputs,
            parameters=payload.parameters,
        )
    except ReportCalculationError as exc:
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    output_field = next(
        field
        for field in template.definition["fields"]
        if field["id"] == formula["output_field_id"]
    )
    return {
        "formula_id": formula["formula_id"],
        "formula_version": formula["version"],
        "output_field_id": formula["output_field_id"],
        "operator": formula["operator"],
        "input_field_ids": list(formula["input_field_ids"]),
        "parameter_ids": list(formula.get("parameter_ids") or []),
        "parameters": payload.parameters,
        "parameter_evidence_ids": payload.parameter_evidence_ids,
        "value": float(value),
        "unit": output_field.get("unit"),
        "precision": 6,
    }


@router.post("/runs/{run_id}/checkpoint")
async def save_analysis_checkpoint(
    payload: CheckpointRequest,
    run: Annotated[ReportRun, Depends(_authorized_run)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    run.checkpoint = payload.model_dump(mode="json")
    run.stage = payload.stage
    await db.commit()
    return {"saved": True, "stage": run.stage}


@router.post("/runs/{run_id}/clarifications")
async def request_clarification(
    payload: ClarificationRequest,
    run: Annotated[ReportRun, Depends(_authorized_run)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    template = _template_for_run(run)
    fields = {field["id"]: field for field in template.definition["fields"]}
    requested_ids = [question.field_id for question in payload.questions]
    if len(requested_ids) != len(set(requested_ids)):
        raise HTTPException(status_code=422, detail={"code": "REPORT_QUESTION_DUPLICATE"})
    if any(field_id not in fields for field_id in requested_ids):
        raise HTTPException(status_code=422, detail={"code": "REPORT_QUESTION_FIELD_UNKNOWN"})
    if any(
        "USER_INPUT" not in fields[field_id].get("source_types", []) for field_id in requested_ids
    ):
        raise HTTPException(
            status_code=422,
            detail={"code": "REPORT_QUESTION_FIELD_NOT_USER_SUPPLIABLE"},
        )
    existing = set(
        (
            await db.execute(
                select(ReportQuestion.field_id, ReportQuestion.question_type).where(
                    ReportQuestion.run_id == run.id
                )
            )
        ).all()
    )
    created_count = 0
    for question in payload.questions:
        if (question.field_id, question.question_type) in existing:
            continue
        field = fields[question.field_id]
        enum_values = (field.get("validation") or {}).get("enum") or []
        options = (
            [{"label": value, "value": value} for value in enum_values]
            if field.get("type") == "enum"
            else question.options
        )
        db.add(
            ReportQuestion(
                run_id=run.id,
                field_id=question.field_id,
                question_type=question.question_type,
                question=question.question,
                options=options,
                required=question.required,
                status="OPEN",
            )
        )
        created_count += 1
    await db.commit()
    open_count = int(
        await db.scalar(
            select(func.count(ReportQuestion.id)).where(
                ReportQuestion.run_id == run.id,
                ReportQuestion.status == "OPEN",
            )
        )
        or 0
    )
    return {
        "accepted": True,
        "question_count": len(payload.questions),
        "created_count": created_count,
        "open_count": open_count,
        "waiting_for_input": open_count > 0,
    }


@router.post("/runs/{run_id}/ir-draft")
async def save_report_ir_draft(
    payload: ReportIRRequest,
    run: Annotated[ReportRun, Depends(_authorized_run)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """把候选 ReportIR 落库，只回一份轻量回执。

    整份 IR 只在这一次调用里进入模型上下文；后续 validate / submit 直接引用落库版本，
    不再重复回传同一份大对象——这是报告会话上下文里最大的可省项。
    """
    report_ir = payload.report_ir
    encoded = json.dumps(
        report_ir, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    run.report_ir_draft = report_ir
    await db.commit()
    ledger = report_ir.get("field_ledger")
    evidence = report_ir.get("evidence")
    sections = report_ir.get("sections")
    return {
        "saved": True,
        "revision": hashlib.sha256(encoded).hexdigest()[:12],
        "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
        "field_ledger_count": len(ledger) if isinstance(ledger, list) else 0,
        "section_count": len(sections) if isinstance(sections, list) else 0,
    }


@router.post("/runs/{run_id}/validate")
async def validate_candidate_report(
    payload: ReportIRValidateRequest,
    run: Annotated[ReportRun, Depends(_authorized_run)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    evidence_context, _manifest = await load_report_source_context(db, run=run)
    validation = validate_report_ir(
        _require_report_ir(payload.report_ir, run),
        run=run,
        template=_template_for_run(run),
        evidence_context=evidence_context,
    )
    return _trim_validation_payload(validation.to_dict())


@router.post("/runs/{run_id}/submit")
async def submit_candidate_report(
    payload: ReportIRSubmitRequest,
    run: Annotated[ReportRun, Depends(_authorized_run)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    evidence_context, chunk_manifest = await load_report_source_context(db, run=run)
    report_ir = _require_report_ir(payload.report_ir, run)
    validation = validate_report_ir(
        report_ir,
        run=run,
        template=_template_for_run(run),
        evidence_context=evidence_context,
    )
    coverage_errors = validate_chunk_coverage(payload.coverage, chunk_manifest)
    if not validation.schema_valid or not validation.publishable or coverage_errors:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "REPORT_IR_VALIDATION_FAILED",
                **_trim_validation_payload(validation.to_dict()),
                "coverage_errors": coverage_errors[:MAX_VALIDATION_ITEMS],
            },
        )
    encoded = json.dumps(
        report_ir, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    manifest = {
        "run_id": run.id,
        "report_ir_sha256": hashlib.sha256(encoded).hexdigest(),
        "template_id": run.template_id,
        "template_version": run.template_version,
        "document_version": run.document_version,
        "generator": "pi-agent",
        "chunk_manifest_sha256": chunk_manifest.content_hash,
        "chunk_count": len(chunk_manifest.items),
    }
    # 不回显 report_ir / coverage：模型刚提交过它们，回显只是把同一份大对象再占用
    # 一次上下文（coverage 在大文档上可达数万 token）。pi 侧用自己提交的对象继续。
    return {
        "accepted": True,
        "validation_report": _trim_validation_payload(validation.to_dict()),
        "manifest": manifest,
    }
