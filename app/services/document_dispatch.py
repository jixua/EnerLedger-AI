"""Recoverable MySQL outbox state for RabbitMQ document commands."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Document
from app.domain.time import utc_now
from app.rag.config import settings
from app.rag.core.mq.messages import DocumentIngestionMessage
from app.rag.database import get_db_context
from app.rag.observability.logging import logger
from app.rag.services.mq_service import MQService

DISPATCH_PENDING = "PENDING"
DISPATCHING = "DISPATCHING"
DISPATCH_PUBLISHED = "PUBLISHED"


class MessagePublisher(Protocol):
    async def send(self, message: DocumentIngestionMessage) -> None: ...


@dataclass(frozen=True, slots=True)
class DispatchClaim:
    document_id: int
    user_id: int
    dataset_id: int
    document_version: int
    lease_token: str


def mark_document_dispatch_pending(document: Document, *, now: datetime | None = None) -> None:
    pending_at = now or utc_now()
    document.dispatch_status = DISPATCH_PENDING
    document.dispatch_attempt_count = 0
    document.dispatch_available_at = pending_at
    document.dispatch_lease_token = None
    document.dispatch_lease_expires_at = None
    document.dispatch_error = None


class DocumentParseDispatcher:
    """Publish at least once and retain retry state in the authoritative document row."""

    def __init__(
        self,
        publisher: MessagePublisher | None = None,
        *,
        lease_seconds: int | None = None,
    ) -> None:
        self._direct_publish = publisher is not None
        self._publisher = publisher or MQService()
        self._lease_seconds = int(
            settings.DOCUMENT_DISPATCH_LEASE_SECONDS
            if lease_seconds is None
            else lease_seconds
        )

    async def dispatch(self, document: Document) -> bool:
        """Try immediately; failure remains durable for the background reconciler."""

        # Unit callers may inject a publisher and pass a transient model. Production rows
        # always have an outbox state and use the database claim path below.
        if self._direct_publish or document.id is None:
            await self._send(document)
            return True
        async with get_db_context() as db:
            claim = await self._claim_specific(
                db,
                document_id=int(document.id),
                document_version=int(document.version),
            )
        if claim is None:
            return document.dispatch_status == DISPATCH_PUBLISHED
        return await self._publish_claim(claim)

    async def dispatch_pending_batch(self) -> int:
        claims: list[DispatchClaim] = []
        for _ in range(settings.DOCUMENT_DISPATCH_BATCH_SIZE):
            async with get_db_context() as db:
                claim = await self._claim_next(db)
            if claim is None:
                break
            claims.append(claim)
        if claims:
            await asyncio.gather(*(self._publish_claim(claim) for claim in claims))
        return len(claims)

    async def _claim_specific(
        self,
        db: AsyncSession,
        *,
        document_id: int,
        document_version: int,
    ) -> DispatchClaim | None:
        now = utc_now()
        document = await db.scalar(
            select(Document)
            .where(Document.id == document_id, Document.version == document_version)
            .with_for_update()
        )
        if document is None or not self._is_claimable(document, now):
            return None
        return await self._claim_locked(db, document, now)

    async def _claim_next(self, db: AsyncSession) -> DispatchClaim | None:
        now = utc_now()
        document = await db.scalar(
            select(Document)
            .where(
                or_(
                    (Document.dispatch_status == DISPATCH_PENDING)
                    & or_(
                        Document.dispatch_available_at.is_(None),
                        Document.dispatch_available_at <= now,
                    ),
                    (Document.dispatch_status == DISPATCHING)
                    & or_(
                        Document.dispatch_lease_expires_at.is_(None),
                        Document.dispatch_lease_expires_at <= now,
                    ),
                )
            )
            .order_by(Document.dispatch_available_at, Document.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if document is None:
            return None
        return await self._claim_locked(db, document, now)

    @staticmethod
    def _is_claimable(document: Document, now: datetime) -> bool:
        if document.dispatch_status == DISPATCH_PENDING:
            return document.dispatch_available_at is None or document.dispatch_available_at <= now
        return document.dispatch_status == DISPATCHING and (
            document.dispatch_lease_expires_at is None
            or document.dispatch_lease_expires_at <= now
        )

    async def _claim_locked(
        self,
        db: AsyncSession,
        document: Document,
        now: datetime,
    ) -> DispatchClaim:
        token = uuid4().hex
        document.dispatch_status = DISPATCHING
        document.dispatch_attempt_count = int(document.dispatch_attempt_count or 0) + 1
        document.dispatch_lease_token = token
        document.dispatch_lease_expires_at = now + timedelta(seconds=self._lease_seconds)
        document.dispatch_error = None
        await db.commit()
        return DispatchClaim(
            document_id=int(document.id),
            user_id=int(document.user_id),
            dataset_id=int(document.dataset_id),
            document_version=int(document.version),
            lease_token=token,
        )

    async def _send(self, document: Document) -> None:
        await self._publisher.send(
            DocumentIngestionMessage.build(
                document_id=int(document.id),
                user_id=int(document.user_id),
                dataset_id=int(document.dataset_id),
                document_version=int(document.version),
            )
        )

    async def _publish_claim(self, claim: DispatchClaim) -> bool:
        try:
            await self._publisher.send(
                DocumentIngestionMessage.build(
                    document_id=claim.document_id,
                    user_id=claim.user_id,
                    dataset_id=claim.dataset_id,
                    document_version=claim.document_version,
                )
            )
        except Exception as exc:
            await self._release_for_retry(claim, exc)
            logger.bind(
                event="document_dispatch_deferred",
                document_id=claim.document_id,
                document_version=claim.document_version,
                error_type=type(exc).__name__,
            ).warning("RabbitMQ 投递失败，已保留数据库 outbox 状态等待自动重试")
            return False

        async with get_db_context() as db:
            result = await db.execute(
                update(Document)
                .where(
                    Document.id == claim.document_id,
                    Document.version == claim.document_version,
                    Document.dispatch_status == DISPATCHING,
                    Document.dispatch_lease_token == claim.lease_token,
                )
                .values(
                    dispatch_status=DISPATCH_PUBLISHED,
                    dispatch_available_at=None,
                    dispatch_lease_token=None,
                    dispatch_lease_expires_at=None,
                    dispatch_error=None,
                )
                .execution_options(synchronize_session=False)
            )
            if int(result.rowcount or 0) == 1:
                await db.commit()
                return True
            await db.rollback()
        return False

    async def _release_for_retry(self, claim: DispatchClaim, error: Exception) -> None:
        now = utc_now()
        async with get_db_context() as db:
            await db.execute(
                update(Document)
                .where(
                    Document.id == claim.document_id,
                    Document.version == claim.document_version,
                    Document.dispatch_status == DISPATCHING,
                    Document.dispatch_lease_token == claim.lease_token,
                )
                .values(
                    dispatch_status=DISPATCH_PENDING,
                    dispatch_available_at=now
                    + timedelta(seconds=settings.DOCUMENT_DISPATCH_RETRY_SECONDS),
                    dispatch_lease_token=None,
                    dispatch_lease_expires_at=None,
                    dispatch_error=f"{type(error).__name__}: {error}"[:1000],
                )
                .execution_options(synchronize_session=False)
            )
            await db.commit()


async def run_document_dispatch_reconciler(stop_event: asyncio.Event) -> None:
    dispatcher = DocumentParseDispatcher()
    while not stop_event.is_set():
        try:
            dispatched = await dispatcher.dispatch_pending_batch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.bind(
                event="document_dispatch_reconciler_failed",
                error_type=type(exc).__name__,
            ).error("文档 outbox 补偿扫描失败")
            dispatched = 0
        delay = 0.01 if dispatched else settings.DOCUMENT_DISPATCH_POLL_SECONDS
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            pass
