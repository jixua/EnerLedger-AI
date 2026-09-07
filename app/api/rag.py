"""三路召回后直接进行 LLM 流式生成的最小 SSE 入口。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import get_shared_owner_user_id
from app.domain.models import Dataset
from app.rag.application.recall_pipeline_provider import (
    aresolve_recall_execution,
    build_recall_request_from_config,
    get_recall_pipeline,
)
from app.rag.application.recall_serialization import serialize_hits
from app.rag.config import settings
from app.rag.core.llm.exceptions import (
    DatasetModelBindingRequiredError,
    LLMConfigResolutionError,
)
from app.rag.core.llm.provider_lifecycle import aclose_dataset_execution_contexts
from app.rag.core.llm.response import UsageInfo
from app.rag.core.llm.user_model_resolver import aresolve_model
from app.rag.core.pipeline.chunk_content import fetch_chunk_sources
from app.rag.core.pipeline.recall import RecallError, RecallFatalError, RecallValidationError
from app.rag.core.pipeline.recall.generation import assemble_context
from app.rag.core.prompts import RAG_GENERATION_SYSTEM_PROMPT, build_rag_user_prompt
from app.rag.database import get_db, get_db_context
from app.services.structured_query import StructuredQueryService

router = APIRouter(prefix="/api/v1/rag", tags=["对话"])

NO_CONTEXT_ANSWER = "根据当前已解析资料无法回答该问题。"


class RagStreamBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=8000)
    dataset_ids: list[int] = Field(min_length=1)
    doc_ids: list[int] | None = None
    llm_config_id: int | None = Field(default=None, gt=0)

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


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _structured_lookup(recall_request) -> list[dict]:
    query_text = recall_request.query.lower()
    structured_markers = (
        "排放因子",
        "排放系数",
        "gwp",
        "温室效应潜能",
        "二氧化碳",
        "甲烷",
        "氧化亚氮",
        "co2",
        "ch4",
        "n2o",
    )
    if not any(marker in query_text for marker in structured_markers):
        return []
    try:
        async with get_db_context() as db:
            return await StructuredQueryService().query_natural_language(
                db,
                user_id=recall_request.user_id,
                dataset_ids=list(recall_request.dataset_ids),
                text=recall_request.query,
            )
    except LookupError:
        return []
    except Exception as exc:  # noqa: BLE001 - structured sidecar must not break document RAG
        logger.bind(error=str(exc)).warning("structured lookup degraded")
        return []


def _structured_context(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = ["[结构化数据查询结果：数值、单位和版本以本区块为准]"]
    for index, row in enumerate(rows, start=1):
        lines.append(
            " | ".join(
                [
                    f"S{index}",
                    f"年份={row.get('edition_year')}",
                    f"表={row.get('table_code')}",
                    f"活动={row.get('activity_name')}",
                    f"气体={row.get('gas')}",
                    f"数值={row.get('factor_value')}",
                    f"单位={row.get('numerator_unit')}/{row.get('denominator_unit')}",
                    f"来源={row.get('source_file')}#{row.get('source_sheet')}:{row.get('source_row')}",
                ]
            )
        )
    return "\n".join(lines)


async def _owned_datasets(
    db: AsyncSession,
    *,
    dataset_ids: list[int],
    user_id: int,
) -> dict[int, Dataset]:
    rows = (
        await db.scalars(
            select(Dataset).where(
                Dataset.id.in_(dataset_ids),
                Dataset.user_id == user_id,
                Dataset.status == "ACTIVE",
            )
        )
    ).all()
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(dataset_ids):
        raise HTTPException(status_code=404, detail="数据集不存在或无权访问")
    return by_id


async def _event_stream(
    *,
    request_id: str,
    recall_request,
    resolved_chat=None,
    chat_config_id: int | None = None,
) -> AsyncGenerator[str, None]:
    yield _sse("stream_started", {"request_id": request_id})
    structured_task = asyncio.create_task(_structured_lookup(recall_request))
    try:
        response = await asyncio.wait_for(
            get_recall_pipeline().execute(recall_request),
            timeout=settings.RECALL_STREAM_TIMEOUT_MS / 1000,
        )
        sources = await fetch_chunk_sources(
            [hit.chunk_id for hit in response.hits], recall_request.user_id
        )
        contents = {chunk_id: source.content for chunk_id, source in sources.items()}
        context = assemble_context(
            response.hits,
            contents,
            settings.RECALL_GENERATION_CONTEXT_TOKEN_BUDGET,
        )
        citation_indexes = {
            block.chunk_id: index for index, block in enumerate(context.blocks, start=1)
        }
        serialized_hits = serialize_hits(
            response,
            sources=sources,
            citation_indexes=citation_indexes,
            include_content=True,
        )
        yield _sse(
            "recall_done",
            {
                "request_id": request_id,
                "hits": serialized_hits,
                "failed_sources": response.failed_sources,
            },
        )
        structured_rows = await structured_task
        if structured_rows:
            yield _sse(
                "structured_data_done",
                {"request_id": request_id, "rows": structured_rows},
            )

        if not context.blocks and not structured_rows:
            yield _sse("answer_delta", {"text": NO_CONTEXT_ANSWER})
            yield _sse(
                "answer_done",
                {
                    "request_id": request_id,
                    "answer": NO_CONTEXT_ANSWER,
                    "usage": UsageInfo().model_dump(),
                    "hits": serialized_hits,
                    "failed_sources": response.failed_sources,
                    "elapsed_ms": response.elapsed_ms,
                },
            )
            return

        if resolved_chat is None:
            if chat_config_id is None:
                yield _sse(
                    "error",
                    {
                        "code": "CHAT_CONFIG_REQUIRED",
                        "message": "检索到可用资料，但数据集未绑定对话模型",
                    },
                )
                return
            try:
                async with get_db_context() as model_db:
                    resolved_chat = await aresolve_model(
                        user_id=recall_request.user_id,
                        config_id=chat_config_id,
                        capability="CHAT",
                        db=model_db,
                    )
            except LLMConfigResolutionError as exc:
                yield _sse(
                    "error",
                    {"code": "MODEL_CONFIG_UNAVAILABLE", "message": str(exc)},
                )
                return

        combined_context = "\n\n".join(
            part for part in (context.context_text, _structured_context(structured_rows)) if part
        )
        prompt = build_rag_user_prompt(recall_request.query, combined_context)
        answer_parts: list[str] = []
        usage = UsageInfo()
        terminal_seen = False
        try:
            async with asyncio.timeout(settings.RECALL_GENERATION_TIMEOUT_MS / 1000):
                async for chunk in resolved_chat.provider.stream(
                    prompt=prompt,
                    system_prompt=RAG_GENERATION_SYSTEM_PROMPT,
                ):
                    if chunk.delta:
                        answer_parts.append(chunk.delta)
                        yield _sse("answer_delta", {"text": chunk.delta})
                    if chunk.usage is not None:
                        usage = chunk.usage
                    if chunk.is_end:
                        terminal_seen = True
        except TimeoutError:
            yield _sse(
                "error",
                {"code": "GENERATION_TIMEOUT", "message": "LLM 流式生成超时"},
            )
            return

        if not terminal_seen:
            yield _sse(
                "error",
                {
                    "code": "GENERATION_INCOMPLETE",
                    "message": "LLM 流式响应未正常结束",
                },
            )
            return

        answer_payload = {
            "request_id": request_id,
            "answer": "".join(answer_parts),
            "usage": usage.model_dump(),
            "hits": serialized_hits,
            "failed_sources": response.failed_sources,
            "elapsed_ms": response.elapsed_ms,
        }
        if structured_rows:
            answer_payload["structured_data"] = structured_rows
        yield _sse("answer_done", answer_payload)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        yield _sse("error", {"code": "RECALL_TIMEOUT", "message": "三路召回执行超时"})
    except RecallFatalError:
        yield _sse(
            "error",
            {"code": "MODEL_CONFIG_UNAVAILABLE", "message": "数据集模型配置不可用"},
        )
    except RecallValidationError as exc:
        yield _sse("error", {"code": "INVALID_REQUEST", "message": str(exc)})
    except RecallError:
        yield _sse("error", {"code": "RECALL_FAILED", "message": "三路召回执行失败"})
    except Exception:
        yield _sse("error", {"code": "GENERATION_FAILED", "message": "LLM 流式生成失败"})
    finally:
        if not structured_task.done():
            structured_task.cancel()
        contexts = (getattr(recall_request, "dataset_contexts", None) or {}).values()
        await aclose_dataset_execution_contexts(
            contexts,
            extra_models=[resolved_chat] if resolved_chat is not None else [],
        )


@router.post("/stream")
async def rag_stream(
    body: RagStreamBody,
    request: Request,
    user_id: int = Depends(get_shared_owner_user_id),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """执行 BM25/Sparse/Dense 融合，并将命中上下文交给 LLM 流式作答。"""
    datasets = await _owned_datasets(db, dataset_ids=body.dataset_ids, user_id=user_id)
    first_dataset = datasets[body.dataset_ids[0]]
    chat_config_id = body.llm_config_id or first_dataset.chat_config_id
    contexts = {}
    try:
        recall_config, contexts = await aresolve_recall_execution(user_id, body.dataset_ids)
        recall_request = build_recall_request_from_config(
            query=body.query,
            user_id=user_id,
            dataset_ids=body.dataset_ids,
            doc_ids=body.doc_ids,
            recall_cfg=recall_config,
            dataset_contexts=contexts,
            apply_ltr_serving_contract=False,
        )
    except LLMConfigResolutionError as exc:
        await aclose_dataset_execution_contexts(
            contexts.values(),
        )
        raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc
    except DatasetModelBindingRequiredError as exc:
        await aclose_dataset_execution_contexts(
            contexts.values(),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BaseException:
        await aclose_dataset_execution_contexts(
            contexts.values(),
        )
        raise

    request_id = request.headers.get("X-Request-Id") or uuid4().hex
    return StreamingResponse(
        _event_stream(
            request_id=request_id,
            recall_request=recall_request,
            chat_config_id=chat_config_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
