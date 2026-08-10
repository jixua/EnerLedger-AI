"""MySQL durable queue 文档解析 worker。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import tempfile
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.bootstrap import configure_nltk_data_path

configure_nltk_data_path()

from app.domain.models import Document  # noqa: E402
from app.rag.config import settings  # noqa: E402
from app.rag.core.parser.pdf.reliability import OpenDataLoaderHealthChecker  # noqa: E402
from app.rag.database import (  # noqa: E402
    close_database,
    get_db_context,
    init_database,
)
from app.rag.observability.logging import logger, setup_logger  # noqa: E402
from app.rag.services.storage.factory import StorageFactory  # noqa: E402
from app.services.document_ingestion import (  # noqa: E402
    DocumentIngestionLeaseLost,
    SimpleDocumentIngestionService,
    close_ingestion_resources,
)
from app.services.document_queue import (  # noqa: E402
    DOCUMENT_STATUS_PROCESSING,
    DocumentClaim,
    DocumentQueueService,
)

SessionContextFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class DocumentParseWorker:
    """一个进程内可配置少量并发的文档解析消费者。"""

    def __init__(
        self,
        *,
        queue: DocumentQueueService | None = None,
        ingestion: SimpleDocumentIngestionService | None = None,
        storage: Any | None = None,
        session_context_factory: SessionContextFactory = get_db_context,
        worker_id: str | None = None,
        poll_interval: float | None = None,
        heartbeat_interval: float | None = None,
        concurrency: int | None = None,
    ) -> None:
        self.queue = queue or DocumentQueueService()
        self.storage = storage or StorageFactory.get_storage()
        self.ingestion = ingestion or SimpleDocumentIngestionService(storage=self.storage)
        self.session_context_factory = session_context_factory
        self.worker_id = worker_id or self._default_worker_id()
        self.poll_interval = float(
            settings.DOCUMENT_QUEUE_POLL_INTERVAL_SECONDS
            if poll_interval is None
            else poll_interval
        )
        self.heartbeat_interval = float(
            settings.DOCUMENT_QUEUE_HEARTBEAT_SECONDS
            if heartbeat_interval is None
            else heartbeat_interval
        )
        self.concurrency = int(
            settings.DOCUMENT_QUEUE_WORKER_CONCURRENCY if concurrency is None else concurrency
        )
        lease_seconds = float(
            getattr(self.queue, "lease_seconds", settings.DOCUMENT_QUEUE_LEASE_SECONDS)
        )
        if self.poll_interval <= 0:
            raise ValueError("poll_interval 必须大于 0")
        if self.heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval 必须大于 0")
        if self.heartbeat_interval >= lease_seconds:
            raise ValueError("heartbeat_interval 必须小于 lease_seconds")
        if self.concurrency < 1:
            raise ValueError("concurrency 必须至少为 1")
        self.stop_event = asyncio.Event()

    async def run(self) -> None:
        health = await asyncio.to_thread(
            OpenDataLoaderHealthChecker(
                timeout_seconds=settings.OPENDATALOADER_HEALTHCHECK_TIMEOUT_SECONDS
            ).check
        )
        health.require_ready()
        Path(settings.PARSE_TEMP_DIR).mkdir(parents=True, exist_ok=True)
        consumers = [
            asyncio.create_task(
                self._consumer_loop(slot),
                name=f"document-parse-consumer-{slot}",
            )
            for slot in range(self.concurrency)
        ]
        try:
            await asyncio.gather(*consumers)
        finally:
            for task in consumers:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)

    def stop(self) -> None:
        """停止领取新任务；当前已领取任务会先完成或安全回到队列。"""

        self.stop_event.set()

    async def _consumer_loop(self, slot: int) -> None:
        worker_slot_id = f"{self.worker_id}:{slot}"
        while not self.stop_event.is_set():
            try:
                async with self.session_context_factory() as db:
                    claim = await self.queue.claim_next(db, worker_id=worker_slot_id)
                if claim is None:
                    await self._wait_for_work()
                    continue
                await self._process_claim(claim)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.bind(
                    event="document_worker_loop_failed",
                    worker_id=worker_slot_id,
                    error_type=type(exc).__name__,
                ).exception("文档解析 worker 循环异常")
                await self._wait_for_work()

    async def _process_claim(self, claim: DocumentClaim) -> None:
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(claim, lease_lost),
            name=f"document-lease-heartbeat-{claim.document_id}",
        )
        try:
            async with self.session_context_factory() as db:
                document = await db.scalar(
                    select(Document).where(
                        Document.id == claim.document_id,
                        Document.status == DOCUMENT_STATUS_PROCESSING,
                        Document.lease_token == claim.lease_token,
                    )
                )
                if document is None:
                    raise DocumentIngestionLeaseLost("领取后未找到当前租约对应的文档")

                with tempfile.TemporaryDirectory(
                    prefix=f"document-{claim.document_id}-",
                    dir=settings.PARSE_TEMP_DIR,
                ) as temp_dir:
                    source_path = Path(temp_dir) / f"source.{document.file_type}"
                    await asyncio.to_thread(
                        self.storage.download_to_path,
                        document.raw_bucket,
                        document.raw_object_key,
                        source_path,
                    )
                    await self.ingestion.ingest(
                        document,
                        source_path,
                        db,
                        replace_existing=claim.reparse_requested,
                        lease_token=claim.lease_token,
                        lease_guard=lambda: self._lease_guard(claim, lease_lost),
                        manage_failure=False,
                    )
            logger.bind(
                event="document_parse_completed",
                document_id=claim.document_id,
                attempt_count=claim.attempt_count,
            ).info("文档解析任务完成")
        except DocumentIngestionLeaseLost:
            logger.bind(
                event="document_parse_lease_lost",
                document_id=claim.document_id,
                attempt_count=claim.attempt_count,
            ).warning("文档解析任务租约失效，旧 worker 放弃终态写入")
        except asyncio.CancelledError:
            # 正常 stop 不会取消正在处理的任务；进程被强制取消时让 lease 自然过期回收。
            raise
        except Exception as exc:
            queue_error = self._queue_failure(exc)
            retryable = bool(getattr(queue_error, "retryable", True))
            error_code = getattr(queue_error, "error_code", None)
            parse_quality_status = getattr(queue_error, "quality_status", None)
            parse_quality = getattr(queue_error, "quality_report", None)
            async with self.session_context_factory() as db:
                target_status = await self.queue.fail_or_retry(
                    db,
                    claim,
                    queue_error,
                    retryable=retryable,
                    error_code=error_code,
                    parse_quality_status=parse_quality_status,
                    parse_quality=parse_quality if isinstance(parse_quality, dict) else None,
                )
            logger.bind(
                event="document_parse_failed",
                document_id=claim.document_id,
                attempt_count=claim.attempt_count,
                target_status=target_status or "LEASE_LOST",
                error_type=type(exc).__name__,
            ).error("文档解析任务失败")
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    @classmethod
    def _queue_failure(cls, error: Exception) -> Exception:
        """Preserve the primary parser error when cleanup also failed.

        ``SimpleDocumentIngestionService`` may raise an ``ExceptionGroup`` containing
        the quality/reliability failure plus rollback or object-cleanup errors.  Queue
        retry semantics and the persisted quality report must come from the primary
        parser failure rather than from the wrapper group.
        """

        if not isinstance(error, BaseExceptionGroup):
            return error
        flattened: list[Exception] = []
        for child in error.exceptions:
            if isinstance(child, Exception):
                selected = cls._queue_failure(child)
                flattened.append(selected)
        if not flattened:
            return error
        return next(
            (item for item in flattened if isinstance(getattr(item, "quality_report", None), dict)),
            next(
                (
                    item
                    for item in flattened
                    if isinstance(getattr(item, "retryable", None), bool)
                ),
                flattened[0],
            ),
        )

    async def _heartbeat(self, claim: DocumentClaim, lease_lost: asyncio.Event) -> None:
        # stop 只停止领取新任务；已领取任务在完成前仍必须续租。
        while not lease_lost.is_set():
            await asyncio.sleep(self.heartbeat_interval)
            async with self.session_context_factory() as db:
                renewed = await self.queue.renew_lease(db, claim)
            if not renewed:
                lease_lost.set()
                return

    async def _lease_guard(
        self,
        claim: DocumentClaim,
        lease_lost: asyncio.Event,
    ) -> bool:
        if lease_lost.is_set():
            return False
        async with self.session_context_factory() as db:
            owned = await self.queue.owns_lease(db, claim)
        if not owned:
            lease_lost.set()
        return owned

    async def _wait_for_work(self) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=self.poll_interval)
        except TimeoutError:
            pass

    @staticmethod
    def _default_worker_id() -> str:
        return f"{socket.gethostname()}-{os.getpid()}"


async def _main() -> None:
    setup_logger()
    await init_database()
    worker = DocumentParseWorker()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, worker.stop)
    try:
        await worker.run()
    finally:
        await close_ingestion_resources()
        await close_database()
        await logger.complete()


if __name__ == "__main__":
    asyncio.run(_main())
