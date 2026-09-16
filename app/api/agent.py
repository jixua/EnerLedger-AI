"""Pi Agent 对话入口与内部知识库工具。"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import AsyncGenerator
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.reports import ReportCreateRequest, create_report
from app.domain.auth import get_actor_user_id, get_shared_owner_user_id
from app.domain.models import Dataset, Document
from app.rag.application.recall_pipeline_provider import (
    aresolve_recall_execution,
    build_recall_request_from_config,
    get_recall_pipeline,
)
from app.rag.application.recall_serialization import serialize_hits
from app.rag.config import settings
from app.rag.core.dataset_config.execution_context import (
    DatasetExecutionContextLoader,
    DatasetExecutionPurpose,
)
from app.rag.core.dataset_config.models import RecallConfig
from app.rag.core.llm.encryption import decrypt_api_key
from app.rag.core.llm.exceptions import ConfigurationException, LLMConfigResolutionError
from app.rag.core.llm.provider_lifecycle import aclose_dataset_execution_contexts
from app.rag.core.llm.runtime_repository import RuntimeConfigRepository
from app.rag.core.llm.user_model_resolver import aresolve_model
from app.rag.core.pipeline.chunk_content import fetch_chunk_sources
from app.rag.core.pipeline.recall.generation import assemble_context
from app.rag.core.prompts import (
    REPORT_CLARIFICATION_FALLBACK,
    REPORT_CLARIFICATION_MAX_OUTPUT_TOKENS,
    REPORT_CLARIFICATION_SYSTEM_PROMPT,
    REPORT_CLARIFICATION_TIMEOUT_SECONDS,
    build_report_clarification_user_prompt,
    clean_report_clarification,
)
from app.rag.database import get_db, get_db_context
from app.rag.models.chunk_record import ChunkRecordDB
from app.services.agent_conversations import finish_turn, start_turn
from app.services.agent_runs import agent_run_registry
from app.services.document_queue import DOCUMENT_STATUS_READY
from app.services.pi_agent_client import (
    PiAgentUnavailableError,
    pi_agent_readiness,
    stream_pi_agent,
)
from app.services.report_template_classifier import Classification, classify_document

router = APIRouter(tags=["Pi Agent"])
logger = logging.getLogger(__name__)


def _sse_error(code: str, message: str) -> str:
    payload = json.dumps({"code": code, "message": message}, ensure_ascii=False)
    return f"event: error\ndata: {payload}\n\n"


def _sse_event(name: str, payload: dict) -> str:
    # payload 可能携带原始 datetime（如报告对象字段），统一降级为字符串保证事件可序列化。
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


async def _report_clarification_reply(
    *,
    db: AsyncSession,
    user_id: int,
    config_id: int,
    filename: str,
    candidates: list[dict],
) -> str:
    """报告类型不明确时，用本轮对话模型生成确认说明；任何失败回落固定文案。"""

    try:
        resolved = await aresolve_model(
            user_id=user_id, config_id=config_id, capability="CHAT", db=db
        )
    except LLMConfigResolutionError:
        return REPORT_CLARIFICATION_FALLBACK
    try:
        result = await asyncio.wait_for(
            resolved.provider.generate(
                prompt=build_report_clarification_user_prompt(
                    filename=filename, candidates=candidates
                ),
                system_prompt=REPORT_CLARIFICATION_SYSTEM_PROMPT,
                temperature=0.2,
                max_tokens=REPORT_CLARIFICATION_MAX_OUTPUT_TOKENS,
            ),
            timeout=REPORT_CLARIFICATION_TIMEOUT_SECONDS,
        )
        return clean_report_clarification(result.content) or REPORT_CLARIFICATION_FALLBACK
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 确认说明为增强项，失败回落固定文案
        logger.bind(
            event="report_clarification_generation_failed",
            outcome="degraded",
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        ).warning("[agent] report clarification generation failed")
        return REPORT_CLARIFICATION_FALLBACK
    finally:
        await aclose_dataset_execution_contexts([], extra_models=[resolved])


class AgentAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: int = Field(gt=0)
    role: Literal["SOURCE", "TEMPLATE"]


class AgentHistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=16_000)


class AgentStreamBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=8_000)
    dataset_ids: list[int] | None = Field(default=None, max_length=20)
    doc_ids: list[int] | None = Field(default=None, max_length=100)
    llm_config_id: int | None = Field(default=None, gt=0)
    history: list[AgentHistoryMessage] = Field(default_factory=list, max_length=10)
    conversation_id: str | None = Field(default=None, max_length=36)
    attachments: list[AgentAttachment] = Field(default_factory=list, max_length=2)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query 不能为空")
        return value

    @field_validator("dataset_ids", "doc_ids")
    @classmethod
    def normalize_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        normalized = list(dict.fromkeys(value))
        if any(item <= 0 for item in normalized):
            raise ValueError("ID 必须为正整数")
        return normalized


class AgentRecallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=8_000)
    intent: str = Field(default="fact_lookup", max_length=64)
    knowledge_base_refs: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query 不能为空")
        return value

    @field_validator("knowledge_base_refs")
    @classmethod
    def normalize_refs(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(ref.strip() for ref in value if ref.strip()))
        if any(len(ref) > 80 for ref in normalized):
            raise ValueError("知识库引用无效")
        return normalized


class AgentScopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", max_length=8_000)
    requested_names: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("requested_names")
    @classmethod
    def normalize_names(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(name.strip() for name in value if name.strip()))


class AgentExpandEvidenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=96)
    before: int = Field(default=1, ge=0, le=3)
    after: int = Field(default=1, ge=0, le=3)


class AgentDocumentOutlineBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str | None = Field(default=None, min_length=1, max_length=96)
    document_ref: str | None = Field(default=None, min_length=1, max_length=96)

    @field_validator("document_ref")
    @classmethod
    def validate_reference_choice(cls, value: str | None, info):
        evidence_id = info.data.get("evidence_id")
        if bool(evidence_id) == bool(value):
            raise ValueError("evidence_id 与 document_ref 必须且只能提供一个")
        return value


class AgentReadSectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_ref: str = Field(min_length=1, max_length=96)
    include_descendants: bool = True
    cursor: str | None = Field(default=None, min_length=1, max_length=128)


def _internal_authorized(authorization: str | None) -> bool:
    if not authorization or not authorization.startswith("Bearer "):
        return False
    supplied = authorization.removeprefix("Bearer ").strip()
    expected = settings.ENERLEDGER_INTERNAL_AGENT_TOKEN
    return bool(expected) and hmac.compare_digest(supplied, expected)


async def _agent_context(run_id: str, authorization: str | None):
    if not _internal_authorized(authorization):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    context = await agent_run_registry.get(
        run_id, max_age_seconds=settings.AGENT_RUN_TIMEOUT_SECONDS + 30
    )
    if context is None:
        raise HTTPException(status_code=404, detail={"code": "AGENT_RUN_NOT_FOUND"})
    return context


def _source_payload(*, evidence, chunk_id: str, source, relation: str | None = None) -> dict:
    payload = {
        "evidence_id": evidence.evidence_id,
        "citation_index": evidence.citation_index,
        "chunk_id": chunk_id,
        "filename": source.filename,
        "page": source.page,
        "page_range": source.page_range,
        "content": source.content,
        "selected_for_context": True,
    }
    if relation:
        payload["relation"] = relation
    return payload


def _find_heading(nodes: list[dict], heading_key: str) -> dict | None:
    for node in nodes:
        if node.get("heading_key") == heading_key:
            return node
        found = _find_heading(node.get("children", []), heading_key)
        if found is not None:
            return found
    return None


def _collect_section_chunk_ids(node: dict, *, include_descendants: bool) -> list[str]:
    chunk_ids = [str(value) for value in node.get("direct_chunk_ids", [])]
    if include_descendants:
        for child in node.get("children", []):
            chunk_ids.extend(_collect_section_chunk_ids(child, include_descendants=True))
    return list(dict.fromkeys(chunk_ids))


async def _outline_nodes(run_id: str, doc_id: int, nodes: list[dict]) -> list[dict]:
    result = []
    for node in nodes:
        section_ref = await agent_run_registry.register_section_ref(
            run_id, doc_id, node["heading_key"]
        )
        result.append(
            {
                "section_ref": section_ref,
                "title": node["title"],
                "level": node["heading_level"],
                "direct_chunk_count": len(node.get("direct_chunk_ids", [])),
                "children": await _outline_nodes(run_id, doc_id, node.get("children", [])),
            }
        )
    return result


def _chunk_heading_trails(chunk: ChunkRecordDB) -> list[list[str]]:
    metadata = chunk.structure_metadata or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    raw_trails = metadata.get("heading_trails")
    if not raw_trails and metadata.get("heading_trail"):
        raw_trails = [metadata["heading_trail"]]
    trails = []
    for raw_trail in raw_trails or []:
        trail = [str(title).strip() for title in raw_trail if str(title).strip()]
        if trail and trail not in trails:
            trails.append(trail)
    return trails


async def _agent_document_tree(db: AsyncSession, context, doc_id: int) -> dict:
    document = await db.scalar(
        select(Document)
        .join(Dataset, Dataset.id == Document.dataset_id)
        .where(
            Document.id == doc_id,
            Document.user_id == context.user_id,
            Document.dataset_id.in_(context.dataset_ids),
            Document.status == "READY",
            Dataset.user_id == context.user_id,
            Dataset.status == "ACTIVE",
        )
    )
    if document is None:
        raise HTTPException(status_code=404, detail={"code": "DOCUMENT_NOT_FOUND"})
    chunks = (
        await db.scalars(
            select(ChunkRecordDB)
            .where(
                ChunkRecordDB.user_id == context.user_id,
                ChunkRecordDB.set_id == document.dataset_id,
                ChunkRecordDB.doc_id == document.id,
                ChunkRecordDB.document_version == document.version,
            )
            .order_by(ChunkRecordDB.chunk_index)
        )
    ).all()

    root: dict = {"children_by_title": {}, "direct_chunk_ids": []}
    for chunk in chunks:
        trails = _chunk_heading_trails(chunk)
        if not trails:
            root["direct_chunk_ids"].append(str(chunk.chunk_id))
            continue
        for trail in trails:
            current = root
            path: list[str] = []
            for level, title in enumerate(trail, start=1):
                path.append(title)
                current = current["children_by_title"].setdefault(
                    title,
                    {
                        "heading_key": json.dumps(path, ensure_ascii=False),
                        "title": title,
                        "heading_level": level,
                        "direct_chunk_ids": [],
                        "children_by_title": {},
                    },
                )
            current["direct_chunk_ids"].append(str(chunk.chunk_id))

    def serialize(nodes: dict[str, dict]) -> list[dict]:
        return [
            {
                "heading_key": node["heading_key"],
                "title": node["title"],
                "heading_level": node["heading_level"],
                "direct_chunk_ids": list(dict.fromkeys(node["direct_chunk_ids"])),
                "children": serialize(node["children_by_title"]),
            }
            for node in nodes.values()
        ]

    return {
        "doc_id": int(document.id),
        "dataset_id": int(document.dataset_id),
        "original_filename": document.filename,
        "headings": serialize(root["children_by_title"]),
        "root_chunk_ids": list(dict.fromkeys(root["direct_chunk_ids"])),
    }


async def _owned_datasets(
    db: AsyncSession, *, user_id: int, dataset_ids: list[int] | None
) -> dict[int, Dataset]:
    statement = select(Dataset).where(
        Dataset.user_id == user_id,
        Dataset.status == "ACTIVE",
    )
    if dataset_ids:
        statement = statement.where(Dataset.id.in_(dataset_ids))
    rows = (await db.scalars(statement.order_by(Dataset.id))).all()
    by_id = {row.id: row for row in rows}
    if dataset_ids and len(by_id) != len(dataset_ids):
        raise HTTPException(status_code=404, detail="数据集不存在或无权访问")
    if not by_id:
        raise HTTPException(status_code=409, detail={"code": "KNOWLEDGE_SCOPE_EMPTY"})
    return by_id


def _chat_config_id(datasets: dict[int, Dataset], requested_id: int | None) -> int:
    if requested_id is not None:
        return requested_id
    bindings = {dataset.chat_config_id for dataset in datasets.values()}
    if None in bindings or len(bindings) != 1:
        raise HTTPException(status_code=409, detail={"code": "CHAT_CONFIG_REQUIRED"})
    return next(iter(bindings))  # type: ignore[return-value]


def _knowledge_base_payload(context, datasets: dict[int, Dataset]) -> list[dict]:
    return [
        {
            "knowledge_base_ref": ref,
            "name": datasets[dataset_id].name,
            "description": datasets[dataset_id].description,
        }
        for ref, dataset_id in context.knowledge_base_refs
        if dataset_id in datasets
    ]


async def _resolve_agent_recall_execution(
    user_id: int, dataset_ids: list[int]
) -> tuple[RecallConfig, dict, list[int]]:
    """多库模式按库解析执行上下文，配置坏库降级；单库仍严格失败。"""
    if len(dataset_ids) <= 1:
        recall_config, contexts = await aresolve_recall_execution(user_id, dataset_ids)
        return recall_config, contexts, []

    contexts = {}
    failed_dataset_ids: list[int] = []
    async with get_db_context() as db:
        loader = DatasetExecutionContextLoader(db)
        for dataset_id in dataset_ids:
            try:
                contexts[dataset_id] = await loader.load(
                    user_id, dataset_id, DatasetExecutionPurpose.RECALL
                )
            except ConfigurationException:
                failed_dataset_ids.append(dataset_id)
    if not contexts:
        raise HTTPException(status_code=503, detail={"code": "RECALL_UNAVAILABLE"})
    return RecallConfig.from_settings(), contexts, failed_dataset_ids


def _model_payload(runtime_config) -> dict[str, str]:
    return {
        "protocol": runtime_config.protocol.lower(),
        "provider": runtime_config.provider_type.lower(),
        "id": runtime_config.model_name,
        "name": runtime_config.model_name,
        "apiKey": decrypt_api_key(runtime_config.api_key_ciphertext),
        "baseUrl": runtime_config.api_base_url,
    }


@router.get("/api/v1/agent/readiness")
async def agent_readiness() -> dict[str, bool]:
    if not settings.AGENT_ENABLED or not await pi_agent_readiness():
        raise HTTPException(status_code=503, detail={"code": "AGENT_NOT_READY"})
    return {"ready": True}


@router.get("/internal/agent/readiness", include_in_schema=False)
async def internal_agent_readiness(
    authorization: str | None = Header(default=None),
) -> dict[str, bool]:
    if not _internal_authorized(authorization):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    return {"ready": True}


@router.post("/internal/agent/runs/{run_id}/scope", include_in_schema=False)
async def internal_agent_scope(
    run_id: str,
    body: AgentScopeBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not _internal_authorized(authorization):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    context = await agent_run_registry.get(
        run_id, max_age_seconds=settings.AGENT_RUN_TIMEOUT_SECONDS + 30
    )
    if context is None:
        raise HTTPException(status_code=404, detail={"code": "AGENT_RUN_NOT_FOUND"})
    datasets = await _owned_datasets(
        db, user_id=context.user_id, dataset_ids=list(context.dataset_ids)
    )
    available = _knowledge_base_payload(context, datasets)
    if not body.requested_names:
        return {
            "scope_mode": context.scope_mode,
            "knowledge_bases": available,
            "unresolved_names": [],
        }

    selected: list[dict] = []
    unresolved: list[str] = []
    for requested_name in body.requested_names:
        lowered = requested_name.casefold()
        matches = [
            item
            for item in available
            if lowered == item["name"].casefold() or lowered in item["name"].casefold()
        ]
        if len(matches) == 1 and matches[0] not in selected:
            selected.append(matches[0])
        elif len(matches) != 1:
            unresolved.append(requested_name)
    return {
        "scope_mode": "selected" if selected else context.scope_mode,
        "knowledge_bases": selected,
        "unresolved_names": unresolved,
    }


@router.post("/internal/agent/runs/{run_id}/recall", include_in_schema=False)
async def internal_agent_recall(
    run_id: str,
    body: AgentRecallBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not _internal_authorized(authorization):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    context = await agent_run_registry.get(
        run_id, max_age_seconds=settings.AGENT_RUN_TIMEOUT_SECONDS + 30
    )
    if context is None:
        raise HTTPException(status_code=404, detail={"code": "AGENT_RUN_NOT_FOUND"})

    if body.knowledge_base_refs:
        resolved_ids = context.resolve_knowledge_base_refs(body.knowledge_base_refs)
        if resolved_ids is None:
            raise HTTPException(status_code=403, detail={"code": "KNOWLEDGE_BASE_FORBIDDEN"})
        dataset_ids = list(resolved_ids)
    else:
        dataset_ids = list(context.dataset_ids)
    datasets = await _owned_datasets(db, user_id=context.user_id, dataset_ids=dataset_ids)

    execution_contexts = {}
    try:
        (
            recall_config,
            execution_contexts,
            failed_dataset_ids,
        ) = await _resolve_agent_recall_execution(context.user_id, dataset_ids)
        usable_dataset_ids = [
            dataset_id for dataset_id in dataset_ids if dataset_id in execution_contexts
        ]
        recall_request = build_recall_request_from_config(
            query=body.query,
            user_id=context.user_id,
            dataset_ids=usable_dataset_ids,
            doc_ids=list(context.doc_ids) if context.doc_ids else None,
            recall_cfg=recall_config,
            dataset_contexts=execution_contexts,
            apply_ltr_serving_contract=False,
        )
        response = await asyncio.wait_for(
            get_recall_pipeline().execute(recall_request),
            timeout=settings.RECALL_STREAM_TIMEOUT_MS / 1000,
        )
        sources = await fetch_chunk_sources(
            [hit.chunk_id for hit in response.hits], context.user_id
        )
        assembled = assemble_context(
            response.hits,
            {chunk_id: source.content for chunk_id, source in sources.items()},
            settings.RECALL_GENERATION_CONTEXT_TOKEN_BUDGET,
        )
        selected_chunk_ids = {block.chunk_id for block in assembled.blocks}
        evidence_by_chunk = {}
        for hit in response.hits:
            source = sources.get(hit.chunk_id)
            if source is None:
                continue
            evidence = await agent_run_registry.register_evidence(
                run_id,
                chunk_id=str(hit.chunk_id),
                dataset_id=hit.dataset_id,
                doc_id=hit.doc_id,
                document_version=str(source.document_version),
                selected_for_context=hit.chunk_id in selected_chunk_ids,
            )
            if evidence is not None:
                evidence_by_chunk[str(hit.chunk_id)] = evidence

        citation_indexes = {
            chunk_id: evidence.citation_index
            for chunk_id, evidence in evidence_by_chunk.items()
            if evidence.citation_index is not None
        }
        hits = serialize_hits(
            response,
            sources=sources,
            citation_indexes=citation_indexes,
            include_content=True,
            include_score_explanations=True,
        )
        for hit in hits:
            evidence = evidence_by_chunk.get(str(hit["chunk_id"]))
            dataset = datasets.get(hit["dataset_id"])
            hit["evidence_id"] = evidence.evidence_id if evidence else None
            hit["selected_for_context"] = str(hit["chunk_id"]) in selected_chunk_ids
            hit["knowledge_base_ref"] = context.knowledge_base_ref_for(hit["dataset_id"])
            hit["knowledge_base_name"] = dataset.name if dataset else None

        hits_by_id = {str(hit["chunk_id"]): hit for hit in hits}
        evidence_blocks = []
        for block in assembled.blocks:
            serialized = hits_by_id.get(str(block.chunk_id))
            if serialized is None:
                continue
            evidence_blocks.append(
                {
                    "evidence_id": serialized["evidence_id"],
                    "citation_index": serialized["citation_index"],
                    "knowledge_base_name": serialized["knowledge_base_name"],
                    "filename": serialized["filename"],
                    "page": serialized["page"],
                    "page_range": serialized["page_range"],
                    "content": block.content,
                }
            )

        per_knowledge_base_counts = []
        for dataset_id, dataset in datasets.items():
            dataset_hits = [hit for hit in hits if hit["dataset_id"] == dataset_id]
            per_knowledge_base_counts.append(
                {
                    "knowledge_base_ref": context.knowledge_base_ref_for(dataset_id),
                    "name": dataset.name,
                    "candidate_count": len(dataset_hits),
                    "context_count": sum(1 for hit in dataset_hits if hit["selected_for_context"]),
                }
            )
        diagnostics = response.recall_diagnostics
        return {
            "query": body.query,
            "intent": body.intent,
            "scope": {
                "mode": context.scope_mode if not body.knowledge_base_refs else "selected",
                "knowledge_base_count": len(datasets),
                "policy": "system_cross_kb_v1" if len(datasets) > 1 else "dataset",
            },
            "retrieval": {
                "strategy": "bm25_sparse_dense",
                "active_sources": list(response.per_source_counts),
                "weights": response.fusion_weights,
                "per_source_counts": response.per_source_counts,
                "candidate_count": len(hits),
                "context_count": len(evidence_blocks),
                "rerank_applied": False,
                "degraded": bool(response.failed_sources)
                or bool(failed_dataset_ids)
                or bool(diagnostics and diagnostics.degraded),
                "failed_sources": response.failed_sources,
                "failed_knowledge_bases": [
                    {
                        "knowledge_base_ref": context.knowledge_base_ref_for(dataset_id),
                        "name": datasets[dataset_id].name,
                    }
                    for dataset_id in failed_dataset_ids
                ],
                "elapsed_ms": response.elapsed_ms,
            },
            "per_knowledge_base_counts": per_knowledge_base_counts,
            "ranked_hits": hits,
            "evidence_blocks": evidence_blocks,
            # 兼容现有前端和 SSE 消费方。
            "hits": hits,
            "failed_sources": response.failed_sources,
            "elapsed_ms": response.elapsed_ms,
        }
    finally:
        await aclose_dataset_execution_contexts(execution_contexts.values())


@router.post("/internal/agent/runs/{run_id}/evidence/expand", include_in_schema=False)
async def internal_agent_expand_evidence(
    run_id: str,
    body: AgentExpandEvidenceBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """沿已召回证据读取同文档、同版本的相邻 Chunk。"""

    context = await _agent_context(run_id, authorization)
    evidence = await agent_run_registry.get_evidence(run_id, body.evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail={"code": "EVIDENCE_NOT_FOUND"})

    current_stmt = select(ChunkRecordDB).where(
        ChunkRecordDB.chunk_id == evidence.chunk_id,
        ChunkRecordDB.user_id == context.user_id,
        ChunkRecordDB.set_id == evidence.dataset_id,
        ChunkRecordDB.doc_id == evidence.doc_id,
        ChunkRecordDB.document_version == int(evidence.document_version or 1),
    )
    current = await db.scalar(current_stmt)
    if current is None:
        raise HTTPException(status_code=404, detail={"code": "EVIDENCE_NOT_FOUND"})

    start_index = max(0, current.chunk_index - body.before)
    end_index = current.chunk_index + body.after
    neighbors_stmt = (
        select(ChunkRecordDB.chunk_id)
        .join(
            Document,
            (Document.id == ChunkRecordDB.doc_id)
            & (Document.dataset_id == ChunkRecordDB.set_id)
            & (Document.user_id == ChunkRecordDB.user_id),
        )
        .where(
            ChunkRecordDB.user_id == context.user_id,
            ChunkRecordDB.set_id == evidence.dataset_id,
            ChunkRecordDB.doc_id == evidence.doc_id,
            ChunkRecordDB.document_version == current.document_version,
            ChunkRecordDB.chunk_index.between(start_index, end_index),
            Document.status == "READY",
        )
        .order_by(ChunkRecordDB.chunk_index)
    )
    chunk_ids = [str(value) for value in (await db.scalars(neighbors_stmt)).all()]
    sources = await fetch_chunk_sources(chunk_ids, context.user_id)
    chunks = []
    for chunk_id in chunk_ids:
        source = sources.get(chunk_id)
        if source is None:
            continue
        registered = await agent_run_registry.register_evidence(
            run_id,
            chunk_id=chunk_id,
            dataset_id=evidence.dataset_id,
            doc_id=evidence.doc_id,
            document_version=str(source.document_version),
            selected_for_context=True,
        )
        if registered is None:
            continue
        relation = (
            "current"
            if source.chunk_index == current.chunk_index
            else "before"
            if source.chunk_index < current.chunk_index
            else "after"
        )
        chunks.append(
            _source_payload(
                evidence=registered,
                chunk_id=chunk_id,
                source=source,
                relation=relation,
            )
        )
    return {
        "source_evidence_id": evidence.evidence_id,
        "chunks": chunks,
        "coverage": {
            "requested_before": body.before,
            "requested_after": body.after,
            "returned_chunk_count": len(chunks),
        },
    }


@router.post("/internal/agent/runs/{run_id}/documents/outline", include_in_schema=False)
async def internal_agent_document_outline(
    run_id: str,
    body: AgentDocumentOutlineBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """从既有证据或运行期文档引用读取授权文档目录。"""

    context = await _agent_context(run_id, authorization)
    if bool(body.evidence_id) == bool(body.document_ref):
        raise HTTPException(status_code=422, detail={"code": "REFERENCE_REQUIRED"})
    if body.evidence_id:
        evidence = await agent_run_registry.get_evidence(run_id, body.evidence_id)
        doc_id = evidence.doc_id if evidence else None
    else:
        doc_id = await agent_run_registry.resolve_document_ref(run_id, body.document_ref or "")
    if doc_id is None:
        raise HTTPException(status_code=404, detail={"code": "DOCUMENT_NOT_FOUND"})

    tree = await _agent_document_tree(db, context, doc_id)
    document_ref = await agent_run_registry.register_document_ref(run_id, doc_id)
    root_section_ref = await agent_run_registry.register_section_ref(run_id, doc_id, None)
    outline = await _outline_nodes(run_id, doc_id, tree.get("headings", []))
    return {
        "document_ref": document_ref,
        "filename": tree["original_filename"],
        "root_section_ref": root_section_ref,
        "root_chunk_count": len(tree.get("root_chunk_ids", [])),
        "outline": outline,
        "reading_policy": {
            "page_size_chunks": 8,
            "cursor_required_for_more": True,
        },
    }


@router.post("/internal/agent/runs/{run_id}/documents/sections/read", include_in_schema=False)
async def internal_agent_read_section(
    run_id: str,
    body: AgentReadSectionBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按不透明章节引用分页读取正文，并把返回 Chunk 纳入证据账本。"""

    context = await _agent_context(run_id, authorization)
    resolved = await agent_run_registry.resolve_section_ref(run_id, body.section_ref)
    if resolved is None:
        raise HTTPException(status_code=404, detail={"code": "SECTION_NOT_FOUND"})
    doc_id, heading_key = resolved
    offset = 0
    if body.cursor:
        cursor_state = await agent_run_registry.resolve_section_cursor(run_id, body.cursor)
        if cursor_state is None or cursor_state[0] != body.section_ref:
            raise HTTPException(status_code=422, detail={"code": "SECTION_CURSOR_INVALID"})
        offset = cursor_state[1]

    tree = await _agent_document_tree(db, context, doc_id)
    if heading_key is None:
        title = "文档根内容"
        chunk_ids = [str(value) for value in tree.get("root_chunk_ids", [])]
        if body.include_descendants:
            for node in tree.get("headings", []):
                chunk_ids.extend(_collect_section_chunk_ids(node, include_descendants=True))
    else:
        node = _find_heading(tree.get("headings", []), heading_key)
        if node is None:
            raise HTTPException(status_code=404, detail={"code": "SECTION_NOT_FOUND"})
        title = node["title"]
        chunk_ids = _collect_section_chunk_ids(node, include_descendants=body.include_descendants)
    chunk_ids = list(dict.fromkeys(chunk_ids))
    page_ids = chunk_ids[offset : offset + 8]
    sources = await fetch_chunk_sources(page_ids, context.user_id)
    chunks = []
    for chunk_id in page_ids:
        source = sources.get(chunk_id)
        if source is None:
            continue
        evidence = await agent_run_registry.register_evidence(
            run_id,
            chunk_id=chunk_id,
            dataset_id=int(tree["dataset_id"]),
            doc_id=doc_id,
            document_version=str(source.document_version),
            selected_for_context=True,
        )
        if evidence is not None:
            chunks.append(_source_payload(evidence=evidence, chunk_id=chunk_id, source=source))
    next_offset = offset + len(page_ids)
    has_more = next_offset < len(chunk_ids)
    next_cursor = (
        await agent_run_registry.register_section_cursor(run_id, body.section_ref, next_offset)
        if has_more
        else None
    )
    return {
        "document_ref": await agent_run_registry.register_document_ref(run_id, doc_id),
        "section_ref": body.section_ref,
        "section_title": title,
        "chunks": chunks,
        "coverage": {
            "total_chunk_count": len(chunk_ids),
            "returned_chunk_count": len(chunks),
            "offset": offset,
            "has_more": has_more,
            "next_cursor": next_cursor,
        },
    }


@router.post("/api/v1/agent/stream")
async def agent_stream(
    body: AgentStreamBody,
    request: Request,
    user_id: int = Depends(get_shared_owner_user_id),
    actor_user_id: int = Depends(get_actor_user_id),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    if not settings.AGENT_ENABLED:
        raise HTTPException(status_code=503, detail={"code": "AGENT_DISABLED"})

    datasets = await _owned_datasets(db, user_id=user_id, dataset_ids=body.dataset_ids)
    dataset_ids = list(body.dataset_ids) if body.dataset_ids else list(datasets)
    config_id = _chat_config_id(datasets, body.llm_config_id)

    try:
        resolved_model = await aresolve_model(
            user_id=user_id,
            config_id=config_id,
            capability="CHAT",
            db=db,
        )
    except LLMConfigResolutionError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc
    await aclose_dataset_execution_contexts([], extra_models=[resolved_model])
    runtime_config = await RuntimeConfigRepository(db=db).get(config_id)
    if runtime_config is None:  # guarded by aresolve_model; keeps type boundary explicit
        raise HTTPException(status_code=404, detail="模型配置不存在")

    source_documents: list[Document] = []
    template_documents: list[Document] = []
    attachment_snapshots: list[dict] = []
    if body.attachments:
        attachment_ids = [item.document_id for item in body.attachments]
        if len(set(attachment_ids)) != len(attachment_ids):
            raise HTTPException(status_code=422, detail="附件不能重复")
        owned = (
            await db.scalars(
                select(Document).where(
                    Document.id.in_(attachment_ids),
                    Document.user_id == user_id,
                    Document.dataset_id.in_(dataset_ids),
                )
            )
        ).all()
        by_id = {int(document.id): document for document in owned}
        if set(by_id) != set(attachment_ids):
            raise HTTPException(status_code=404, detail="附件不存在或不在当前知识库范围")
        for item in body.attachments:
            document = by_id[item.document_id]
            if document.status != DOCUMENT_STATUS_READY:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "AGENT_ATTACHMENT_NOT_READY",
                        "message": f"{document.filename} 尚未解析完成，请稍后再发送",
                    },
                )
            (source_documents if item.role == "SOURCE" else template_documents).append(document)
            attachment_snapshots.append(
                {
                    "document_id": int(document.id),
                    "document_version": int(document.version),
                    "dataset_id": int(document.dataset_id),
                    "filename": document.filename,
                    "role": item.role,
                }
            )
        if len(source_documents) != 1 or len(template_documents) > 1:
            raise HTTPException(status_code=422, detail="报告对话需要一个源文件和最多一个模板文件")

    conversation, turn, persisted_history = await start_turn(
        db,
        user_id=actor_user_id,
        conversation_id=body.conversation_id,
        user_content=body.query,
        dataset_ids=dataset_ids,
        document_ids=list(body.doc_ids or []),
        llm_config_id=config_id,
        attachments=attachment_snapshots,
    )

    if source_documents:
        try:
            classification = await classify_document(db, document=source_documents[0])
            if template_documents:
                template_classification = await classify_document(
                    db, document=template_documents[0]
                )
        except Exception as exc:
            logger.exception("Report template classification failed", exc_info=exc)
            await finish_turn(
                db,
                turn=turn,
                content="报告类型识别失败，请稍后重试。",
                status="FAILED",
                error_code="REPORT_TEMPLATE_CLASSIFICATION_FAILED",
                error_message="报告类型识别失败",
            )
            raise
        if template_documents:
            source_type = classification.selected_report_type
            template_type = template_classification.selected_report_type
            if template_type and source_type and template_type != source_type:
                merged: dict[str, dict] = {}
                for item in (
                    *template_classification.candidates,
                    *classification.candidates,
                ):
                    previous = merged.get(item["report_type"])
                    if previous is None or item["score"] > previous["score"]:
                        merged[item["report_type"]] = item
                ranked_candidates = sorted(
                    merged.values(),
                    key=lambda item: (-item["score"], item["report_type"]),
                )
                classification = Classification(
                    state="AMBIGUOUS",
                    selected_report_type=None,
                    candidates=tuple(ranked_candidates[:3]),
                )
            elif template_type:
                classification = template_classification
        if classification.state == "CONFIDENT":
            report_type = str(classification.selected_report_type)
            try:
                report = await create_report(
                    document_id=int(source_documents[0].id),
                    payload=ReportCreateRequest(
                        report_type=report_type,
                        llm_config_id=config_id,
                        custom_template_document_id=(
                            int(template_documents[0].id) if template_documents else None
                        ),
                        user_instructions=body.query[:2000],
                    ),
                    user_id=user_id,
                    db=db,
                )
            except Exception as exc:
                logger.exception("Report creation from agent failed", exc_info=exc)
                await finish_turn(
                    db,
                    turn=turn,
                    content="报告任务创建失败，请检查文件状态后重试。",
                    status="FAILED",
                    error_code="REPORT_CREATION_FAILED",
                    error_message="报告任务创建失败",
                )
                raise
            answer = (
                f"已识别为 {report_type}，并创建报告任务 {report['run_id'][:8]}。"
                + ("生成时会参考你上传的模板版式与章节表达。" if template_documents else "")
            )
            await finish_turn(
                db, turn=turn, content=answer, report_run_id=report["run_id"]
            )

            async def report_stream() -> AsyncGenerator[str, None]:
                yield _sse_event(
                    "conversation_started",
                    {"conversation_id": conversation.id, "turn_id": turn.id},
                )
                yield _sse_event(
                    "report_started",
                    {"report_run": report, "classification": classification.to_dict()},
                )
                yield _sse_event("answer_delta", {"text": answer})
                yield _sse_event("answer_done", {"answer": answer, "request_id": turn.id})

            return StreamingResponse(report_stream(), media_type="text/event-stream")

        options = [
            {
                "value": candidate["report_type"],
                "label": f"{candidate['report_type']} · {candidate['name']}",
                "description": (
                    "命中：" + "、".join(candidate["matched_terms"][:4])
                    if candidate["matched_terms"]
                    else "当前材料特征不足，请按报告用途确认。"
                ),
            }
            for candidate in classification.candidates
        ]
        interaction = {
            "type": "TEMPLATE_SELECTION",
            "status": "OPEN",
            "title": "选择报告类型",
            "question": "当前材料可能对应多类报告，请确认要生成哪一种？",
            "options": options,
            "source_document_id": int(source_documents[0].id),
            "template_document_id": int(template_documents[0].id) if template_documents else None,
            "llm_config_id": config_id,
            "user_instructions": body.query[:2000],
            "classification": classification.to_dict(),
        }
        answer = await _report_clarification_reply(
            db=db,
            user_id=user_id,
            config_id=config_id,
            filename=source_documents[0].filename,
            candidates=list(classification.candidates),
        )
        await finish_turn(
            db, turn=turn, content=answer, status="NEEDS_INPUT", interaction=interaction
        )

        async def clarification_stream() -> AsyncGenerator[str, None]:
            yield _sse_event(
                "conversation_started",
                {"conversation_id": conversation.id, "turn_id": turn.id},
            )
            yield _sse_event("answer_delta", {"text": answer})
            yield _sse_event("confirmation_required", {"interaction": interaction})
            yield _sse_event("answer_done", {"answer": answer, "request_id": turn.id})

        return StreamingResponse(clarification_stream(), media_type="text/event-stream")

    run_id = uuid4().hex
    try:
        await agent_run_registry.register(
            run_id=run_id,
            user_id=user_id,
            dataset_ids=dataset_ids,
            doc_ids=body.doc_ids,
            scope_mode="selected" if body.dataset_ids else "all_accessible",
        )
    except Exception as exc:
        logger.exception("Agent run registration failed", exc_info=exc)
        await finish_turn(
            db,
            turn=turn,
            content="Agent 运行初始化失败，请稍后重试。",
            status="FAILED",
            error_code="AGENT_RUN_REGISTRATION_FAILED",
            error_message="Agent 运行初始化失败",
        )
        raise
    payload = {
        "runId": run_id,
        "content": body.query,
        "history": persisted_history,
        "model": _model_payload(runtime_config),
    }

    async def event_stream() -> AsyncGenerator[str, None]:
        answer = ""
        terminal = False
        failed = False
        buffer = ""
        try:
            yield _sse_event(
                "conversation_started",
                {"conversation_id": conversation.id, "turn_id": turn.id},
            )
            async with asyncio.timeout(settings.AGENT_RUN_TIMEOUT_SECONDS):
                async for chunk in stream_pi_agent(payload):
                    buffer += chunk.replace("\r\n", "\n")
                    while "\n\n" in buffer:
                        frame, buffer = buffer.split("\n\n", 1)
                        event_name = "message"
                        data_lines = []
                        for line in frame.splitlines():
                            if line.startswith("event:"):
                                event_name = line[6:].strip()
                            elif line.startswith("data:"):
                                data_lines.append(line[5:].strip())
                        if data_lines:
                            try:
                                event_data = json.loads("\n".join(data_lines))
                            except json.JSONDecodeError:
                                event_data = {}
                            if event_name == "answer_delta":
                                answer += str(event_data.get("text") or "")
                            elif event_name == "answer_done":
                                answer = str(event_data.get("answer") or answer)
                                terminal = True
                            elif event_name == "error":
                                failed = True
                                await finish_turn(
                                    db,
                                    turn=turn,
                                    content=answer,
                                    status="FAILED",
                                    error_code=str(event_data.get("code") or "AGENT_ERROR")[:64],
                                    error_message=str(
                                        event_data.get("message") or "Agent 执行失败"
                                    )[:1000],
                                )
                    yield chunk
            if terminal and not failed:
                await finish_turn(db, turn=turn, content=answer)
            elif not failed:
                await finish_turn(
                    db,
                    turn=turn,
                    content=answer,
                    status="FAILED",
                    error_code="STREAM_INCOMPLETE",
                    error_message="Agent 流在终态事件前结束",
                )
        except TimeoutError:
            await finish_turn(
                db, turn=turn, content=answer, status="FAILED",
                error_code="AGENT_TIMEOUT", error_message="Agent 执行超时",
            )
            yield _sse_error("AGENT_TIMEOUT", "Agent 执行超时")
        except PiAgentUnavailableError:
            await finish_turn(
                db, turn=turn, content=answer, status="FAILED",
                error_code="AGENT_UNAVAILABLE", error_message="Agent 服务暂不可用",
            )
            yield _sse_error("AGENT_UNAVAILABLE", "Agent 服务暂不可用")
        except asyncio.CancelledError:
            await finish_turn(db, turn=turn, content=answer, status="CANCELLED")
            raise
        except Exception as exc:
            logger.exception("Pi Agent stream failed unexpectedly", exc_info=exc)
            await finish_turn(
                db,
                turn=turn,
                content=answer,
                status="FAILED",
                error_code="AGENT_INTERNAL_ERROR",
                error_message="Agent 执行时发生未预期错误",
            )
            yield _sse_error("AGENT_INTERNAL_ERROR", "Agent 执行时发生未预期错误")
        finally:
            await agent_run_registry.release(run_id)

    request_id = request.headers.get("X-Request-Id") or run_id
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Request-Id": request_id,
        },
    )
