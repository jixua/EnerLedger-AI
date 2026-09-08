# ruff: noqa: E402
"""能碳会计 AI 智能体的独立 RAG 服务入口。"""

from app.rag.bootstrap import configure_nltk_data_path

configure_nltk_data_path()

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.rag.config import settings
from app.rag.database import close_database, init_database
from app.rag.observability.logging import logger, setup_logger
from app.services.arxiv_crawler import arxiv_crawler
from app.services.document_dispatch import run_document_dispatch_reconciler
from app.services.report_dispatch import run_report_dispatch_reconciler

setup_logger()

from app.api.agent import router as agent_router
from app.api.auth import router as auth_router
from app.api.crawler import router as crawler_router
from app.api.datasets import router as datasets_router
from app.api.document_analysis import (
    report_index_router as document_analysis_report_index_router,
)
from app.api.document_analysis import router as document_analysis_router
from app.api.documents import router as documents_router
from app.api.llm import router as llm_router
from app.api.rag import router as rag_router
from app.api.recall import router as recall_router
from app.api.report_agent_internal import router as report_agent_internal_router
from app.api.reports import router as reports_router
from app.api.system import router as system_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_database()
    from app.rag.application.recall_pipeline_provider import (
        prewarm_recall_pipeline,
    )

    await prewarm_recall_pipeline()
    dispatch_stop = asyncio.Event()
    dispatch_task = asyncio.create_task(
        run_document_dispatch_reconciler(dispatch_stop),
        name="document-dispatch-reconciler",
    )
    report_dispatch_stop = asyncio.Event()
    report_dispatch_task = asyncio.create_task(
        run_report_dispatch_reconciler(report_dispatch_stop),
        name="report-dispatch-reconciler",
    )
    yield
    dispatch_stop.set()
    report_dispatch_stop.set()
    await dispatch_task
    await report_dispatch_task
    from app.rag.application.recall_pipeline_provider import (
        close_recall_pipeline_resources,
    )
    from app.rag.services.usage_reporter import drain_usage_reports
    from app.services.document_ingestion import close_ingestion_resources

    await drain_usage_reports()
    await close_recall_pipeline_resources()
    await close_ingestion_resources()
    await arxiv_crawler.close()
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
    allow_headers=[
        "Accept",
        "Authorization",
        "Content-Type",
        "X-Crawler-Api-Key",
        "X-Request-Id",
    ],
    expose_headers=["Location", "X-Request-Id", "X-Document-Version"],
)
app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(crawler_router)
app.include_router(llm_router)
app.include_router(datasets_router)
app.include_router(documents_router)
app.include_router(document_analysis_router)
app.include_router(document_analysis_report_index_router)
app.include_router(recall_router)
app.include_router(reports_router)
app.include_router(rag_router)
app.include_router(report_agent_internal_router)
app.include_router(system_router)


@app.get("/health/live", tags=["系统"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}
