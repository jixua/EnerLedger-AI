"""面向前端系统状态页的轻量依赖与解析队列检查。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import get_user_id
from app.domain.models import Document
from app.rag.config import settings
from app.rag.core.parser.pdf.reliability import OpenDataLoaderHealthChecker
from app.rag.database import get_db
from app.services.document_queue import (
    DOCUMENT_STATUS_FAILED,
    DOCUMENT_STATUS_PROCESSING,
    DOCUMENT_STATUS_QUEUED,
    DOCUMENT_STATUS_READY,
)

router = APIRouter(prefix="/api/v1/system", tags=["系统"])


def _component(status: str, message: str, **metrics: int) -> dict:
    payload: dict[str, object] = {"status": status, "message": message}
    if metrics:
        payload["metrics"] = metrics
    return payload


async def _check_http(url: str, *, label: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.get(url)
        if response.status_code >= 500:
            return _component("failed", f"{label} 返回 HTTP {response.status_code}")
        return _component("ready", f"{label} 已响应")
    except Exception as exc:
        return _component("failed", f"{label} 连接失败：{type(exc).__name__}")


async def _check_tcp(host: str, port: int, *, label: str) -> dict:
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=2.5,
        )
        writer.close()
        await writer.wait_closed()
        return _component("ready", f"{label} TCP 端口已响应")
    except Exception as exc:
        return _component("failed", f"{label} 连接失败：{type(exc).__name__}")


def _minio_health_url() -> str:
    endpoint = settings.MINIO_ENDPOINT
    if not endpoint.startswith(("http://", "https://")):
        endpoint = f"{'https' if settings.MINIO_USE_SSL else 'http'}://{endpoint}"
    return f"{endpoint.rstrip('/')}/minio/health/live"


def _qdrant_health_url() -> str:
    if settings.QDRANT_URL:
        return f"{settings.QDRANT_URL.rstrip('/')}/readyz"
    scheme = "https" if settings.QDRANT_HTTPS else "http"
    return f"{scheme}://{settings.QDRANT_HOST}:{settings.QDRANT_PORT}/readyz"


@router.get("/status")
async def get_system_status(
    user_id: int = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """返回依赖连通性、队列计数以及可观察到的 worker 活跃状态。"""

    await db.execute(text("SELECT 1"))
    status_rows = (
        await db.execute(
            select(Document.status, func.count(Document.id))
            .where(Document.user_id == user_id)
            .group_by(Document.status)
        )
    ).all()
    counts = {
        DOCUMENT_STATUS_QUEUED: 0,
        DOCUMENT_STATUS_PROCESSING: 0,
        DOCUMENT_STATUS_READY: 0,
        DOCUMENT_STATUS_FAILED: 0,
    }
    for status_name, count in status_rows:
        counts[str(status_name).upper()] = int(count)

    now = datetime.now(UTC).replace(tzinfo=None)
    active_leases = int(
        await db.scalar(
            select(func.count(Document.id)).where(
                Document.user_id == user_id,
                Document.status == DOCUMENT_STATUS_PROCESSING,
                Document.lease_expires_at.is_not(None),
                Document.lease_expires_at > now,
            )
        )
        or 0
    )

    minio, qdrant, manticore, opendataloader = await asyncio.gather(
        _check_http(_minio_health_url(), label="MinIO"),
        _check_http(_qdrant_health_url(), label="Qdrant"),
        _check_tcp(settings.MANTICORE_HOST, settings.MANTICORE_PORT, label="Manticore"),
        asyncio.to_thread(
            lambda: OpenDataLoaderHealthChecker(
                timeout_seconds=settings.OPENDATALOADER_HEALTHCHECK_TIMEOUT_SECONDS
            ).check().to_dict()
        ),
    )
    queue = _component(
        "ready",
        "MySQL 持久解析队列可查询",
        queued=counts[DOCUMENT_STATUS_QUEUED],
        processing=counts[DOCUMENT_STATUS_PROCESSING],
        ready=counts[DOCUMENT_STATUS_READY],
        failed=counts[DOCUMENT_STATUS_FAILED],
    )
    worker = (
        _component("ready", f"检测到 {active_leases} 个有效处理租约", active_leases=active_leases)
        if active_leases
        else _component("pending", "当前没有可观察到的处理租约", active_leases=0)
    )
    components = {
        "mysql": _component("ready", "数据库查询正常"),
        "minio": minio,
        "qdrant": qdrant,
        "manticore": manticore,
        "opendataloader": opendataloader,
        "queue": queue,
        "worker": worker,
    }
    core_ready = all(
        components[name]["status"] == "ready"
        for name in ("mysql", "minio", "qdrant", "manticore", "opendataloader", "queue")
    )
    return {
        "status": "ok" if core_ready else "degraded",
        "checked_at": datetime.now(UTC).isoformat(),
        "components": components,
        "queue": {"counts": counts, "active_leases": active_leases},
    }
