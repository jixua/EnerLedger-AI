# ruff: noqa: E402
"""能碳会计 AI 智能体的独立 RAG 服务入口。"""

from app.rag.bootstrap import configure_nltk_data_path

configure_nltk_data_path()

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.rag.config import settings
from app.rag.database import close_database, init_database
from app.rag.observability.logging import logger, setup_logger

setup_logger()

from app.api.datasets import router as datasets_router
from app.api.documents import router as documents_router
from app.api.llm import router as llm_router
from app.api.rag import router as rag_router
from app.api.recall import router as recall_router
from app.api.system import router as system_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_database()
    yield
    from app.rag.application.recall_pipeline_provider import (
        close_recall_pipeline_resources,
    )
    from app.rag.services.usage_reporter import drain_usage_reports
    from app.services.document_ingestion import close_ingestion_resources

    await drain_usage_reports()
    await close_recall_pipeline_resources()
    await close_ingestion_resources()
    await close_database()
    await logger.complete()


app = FastAPI(
    title="能碳会计 AI 智能体服务",
    version="0.1.0",
    description=("提供能碳资料管理、文档解析、多路检索与带引用来源的流式对话。"),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Content-Type", "X-Request-Id", "X-User-Id"],
    expose_headers=["Location", "X-Request-Id", "X-Document-Version"],
)
app.include_router(llm_router)
app.include_router(datasets_router)
app.include_router(documents_router)
app.include_router(recall_router)
app.include_router(rag_router)
app.include_router(system_router)


@app.get("/health/live", tags=["系统"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}
