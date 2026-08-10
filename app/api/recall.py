"""当前项目自己的三路召回 HTTP 入口。

该路由直接调用已迁入 ``app.rag`` 的 BM25 / Sparse / Dense 召回与融合管线，
不依赖外部 LinkRag 包，也不转发到 LinkRag 服务。
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.auth import get_user_id
from app.rag.application.recall_pipeline_provider import (
    aresolve_recall_execution,
    build_recall_request_from_config,
    get_recall_pipeline,
)
from app.rag.application.recall_serialization import (
    serialize_hits,
    serialize_recall_diagnostics,
)
from app.rag.config import settings
from app.rag.core.llm.exceptions import (
    DatasetModelBindingRequiredError,
    LLMConfigResolutionError,
)
from app.rag.core.llm.provider_lifecycle import aclose_dataset_execution_contexts
from app.rag.core.pipeline.chunk_content import fetch_chunk_sources
from app.rag.core.pipeline.recall import (
    RecallError,
    RecallFatalError,
    RecallValidationError,
)

router = APIRouter(prefix="/api/v1/recall", tags=["混合检索"])


class RecallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=8000)
    dataset_ids: list[int] = Field(min_length=1)
    doc_ids: list[int] | None = None
    include_content: bool = True

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能为空")
        return normalized

    @field_validator("dataset_ids", "doc_ids")
    @classmethod
    def validate_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        normalized = list(dict.fromkeys(value))
        if any(item <= 0 for item in normalized):
            raise ValueError("ID 必须为正整数")
        return normalized


@router.post("")
async def recall(
    body: RecallBody,
    request: Request,
    user_id: int = Depends(get_user_id),
) -> dict:
    """执行 BM25、Sparse、Dense 三路检索与加权融合。"""
    dataset_contexts = {}
    try:
        try:
            recall_config, dataset_contexts = await aresolve_recall_execution(
                user_id,
                body.dataset_ids,
            )
            recall_request = build_recall_request_from_config(
                query=body.query,
                user_id=user_id,
                dataset_ids=body.dataset_ids,
                doc_ids=body.doc_ids,
                recall_cfg=recall_config,
                dataset_contexts=dataset_contexts,
                apply_ltr_serving_contract=False,
            )
            response = await asyncio.wait_for(
                get_recall_pipeline().execute(recall_request),
                timeout=settings.RECALL_STREAM_TIMEOUT_MS / 1000,
            )
        except DatasetModelBindingRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LLMConfigResolutionError as exc:
            raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc
        except RecallValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RecallFatalError as exc:
            raise HTTPException(status_code=422, detail="召回所需模型配置不可用") from exc
        except RecallError as exc:
            raise HTTPException(status_code=503, detail="三路召回均执行失败") from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="召回执行超时") from exc

        sources = await fetch_chunk_sources(
            [str(hit.chunk_id) for hit in response.hits],
            user_id,
        )
        hits = serialize_hits(
            response,
            sources=sources,
            include_content=body.include_content,
        )

        payload: dict = {
            "request_id": request.headers.get("X-Request-Id") or uuid4().hex,
            "query": body.query,
            "hits": hits,
            "per_source_counts": response.per_source_counts,
            "failed_sources": response.failed_sources,
            "elapsed_ms": response.elapsed_ms,
        }
        if response.recall_diagnostics is not None:
            payload["recall_diagnostics"] = serialize_recall_diagnostics(
                response.recall_diagnostics
            )
        return payload
    finally:
        await aclose_dataset_execution_contexts(dataset_contexts.values())
