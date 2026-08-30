"""Recoverable MySQL outbox for report-generation commands."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import ReportRun
from app.domain.time import utc_now
from app.rag.config import settings
from app.rag.core.mq.messages import ReportGenerationMessage
from app.rag.database import get_db_context
from app.rag.observability.logging import logger
from app.rag.services.mq_service import MQService

DISPATCH_PENDING = "PENDING"
DISPATCHING = "DISPATCHING"
DISPATCH_PUBLISHED = "PUBLISHED"


class MessagePublisher(Protocol):
    async def send(self, message: ReportGenerationMessage) -> None: ...


@dataclass(frozen=True, slots=True)
class ReportDispatchClaim:
    run_id: str
    user_id: int
    document_id: int
    document_version: int
    lease_token: str


def mark_report_dispatch_pending(run: ReportRun, *, now: datetime | None = None) -> None:
    pending_at = now or utc_now()
    run.dispatch_status = DISPATCH_PENDING
    run.dispatch_attempt_count = 0
    run.dispatch_available_at = pending_at
    run.dispatch_lease_token = None
    run.dispatch_lease_expires_at = None
    run.dispatch_error = None


class ReportRunDispatcher:
    def __init__(self, publisher: MessagePublisher | None = None) -> None:
        self._publisher = publisher or MQService()
        self._direct_publish = publisher is not None

    async def dispatch(self, run: ReportRun) -> bool:
        if self._direct_publish:
            await self._send(run)
            return True
        async with get_db_context() as db:
            claim = await self._claim_specific(db, run_id=str(run.id))
        if claim is None:
            return run.dispatch_status == DISPATCH_PUBLISHED
        return await self._publish_claim(claim)

    async def dispatch_pending_batch(self) -> int:
        claims: list[ReportDispatchClaim] = []
        for _ in range(settings.REPORT_DISPATCH_BATCH_SIZE):
            async with get_db_context() as db:
                claim = await self._claim_next(db)
            if claim is None:
                break
            claims.append(claim)
        if claims:
            await asyncio.gather(*(self._publish_claim(claim) for claim in claims))
        return len(claims)

    async def _claim_specific(self, db: AsyncSession, *, run_id: str) -> ReportDispatchClaim | None:
        now = utc_now()
        run = await db.scalar(select(ReportRun).where(ReportRun.id == run_id).with_for_update())
        if run is None or not self._claimable(run, now):
            return None
        return await self._claim_locked(db, run, now)

    async def _claim_next(self, db: AsyncSession) -> ReportDispatchClaim | None:
        now = utc_now()
        run = await db.scalar(
            select(ReportRun)
            .where(
                or_(
                    (ReportRun.dispatch_status == DISPATCH_PENDING)
                    & or_(
                        ReportRun.dispatch_available_at.is_(None),
                        ReportRun.dispatch_available_at <= now,
                    ),
                    (ReportRun.dispatch_status == DISPATCHING)
                    & or_(
                        ReportRun.dispatch_lease_expires_at.is_(None),
                        ReportRun.dispatch_lease_expires_at <= now,
                    ),
                )
            )
            .order_by(ReportRun.dispatch_available_at, ReportRun.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if run is None:
            return None
        return await self._claim_locked(db, run, now)

    @staticmethod
    def _claimable(run: ReportRun, now: datetime) -> bool:
        if run.dispatch_status == DISPATCH_PENDING:
            return run.dispatch_available_at is None or run.dispatch_available_at <= now
        return run.dispatch_status == DISPATCHING and (
            run.dispatch_lease_expires_at is None or run.dispatch_lease_expires_at <= now
        )

    @staticmethod
    async def _claim_locked(db: AsyncSession, run: ReportRun, now: datetime) -> ReportDispatchClaim:
        token = uuid4().hex
        run.dispatch_status = DISPATCHING
        run.dispatch_attempt_count = int(run.dispatch_attempt_count or 0) + 1
        run.dispatch_lease_token = token
        run.dispatch_lease_expires_at = now + timedelta(
            seconds=settings.REPORT_DISPATCH_LEASE_SECONDS
        )
        run.dispatch_error = None
        await db.commit()
        return ReportDispatchClaim(
            run_id=str(run.id),
            user_id=int(run.user_id),
            document_id=int(run.document_id),
            document_version=int(run.document_version),
            lease_token=token,
        )

    async def _send(self, run: ReportRun) -> None:
        await self._publisher.send(
            ReportGenerationMessage.build(
                run_id=str(run.id),
                user_id=int(run.user_id),
                document_id=int(run.document_id),
                document_version=int(run.document_version),
            )
        )

    async def _publish_claim(self, claim: ReportDispatchClaim) -> bool:
        try:
            await self._publisher.send(
                ReportGenerationMessage.build(
                    run_id=claim.run_id,
                    user_id=claim.user_id,
                    document_id=claim.document_id,
                    document_version=claim.document_version,
                )
            )
        except Exception as exc:
            await self._release_for_retry(claim, exc)
            logger.bind(
                event="report_dispatch_deferred",
                run_id=claim.run_id,
                error_type=type(exc).__name__,
            ).warning("报告任务投递失败，已保留 outbox 状态等待重试")
            return False

        async with get_db_context() as db:
            result = await db.execute(
                update(ReportRun)
                .where(
                    ReportRun.id == claim.run_id,
                    ReportRun.dispatch_status == DISPATCHING,
                    ReportRun.dispatch_lease_token == claim.lease_token,
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

    @staticmethod
    async def _release_for_retry(claim: ReportDispatchClaim, error: Exception) -> None:
        async with get_db_context() as db:
            await db.execute(
                update(ReportRun)
                .where(
                    ReportRun.id == claim.run_id,
                    ReportRun.dispatch_status == DISPATCHING,
                    ReportRun.dispatch_lease_token == claim.lease_token,
                )
                .values(
                    dispatch_status=DISPATCH_PENDING,
                    dispatch_available_at=utc_now()
                    + timedelta(seconds=settings.REPORT_DISPATCH_RETRY_SECONDS),
                    dispatch_lease_token=None,
                    dispatch_lease_expires_at=None,
                    dispatch_error=f"{type(error).__name__}: {error}"[:1000],
                )
                .execution_options(synchronize_session=False)
            )
            await db.commit()


async def run_report_dispatch_reconciler(stop_event: asyncio.Event) -> None:
    dispatcher = ReportRunDispatcher()
    while not stop_event.is_set():
        try:
            dispatched = await dispatcher.dispatch_pending_batch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.bind(
                event="report_dispatch_reconciler_failed",
                error_type=type(exc).__name__,
            ).error("报告 outbox 补偿扫描失败")
            dispatched = 0
        delay = 0.01 if dispatched else settings.REPORT_DISPATCH_POLL_SECONDS
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            pass
