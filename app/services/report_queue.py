"""Report-run state transitions with lease fencing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import ReportRun
from app.domain.time import utc_now
from app.rag.config import settings

REPORT_TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "STALE_DOCUMENT"}


def _retry_delays() -> tuple[int, ...]:
    return tuple(
        int(item.strip())
        for item in settings.REPORT_QUEUE_RETRY_DELAYS_SECONDS.split(",")
        if item.strip()
    )


@dataclass(frozen=True, slots=True)
class ReportRunClaim:
    run_id: str
    lease_token: str
    attempt_count: int


class ReportRunQueueService:
    def __init__(self) -> None:
        self.lease_seconds = settings.REPORT_QUEUE_LEASE_SECONDS
        self.max_attempts = settings.REPORT_QUEUE_MAX_ATTEMPTS
        self.retry_delays = _retry_delays()

    async def claim(
        self,
        db: AsyncSession,
        *,
        run_id: str,
        worker_id: str,
        now: datetime | None = None,
    ) -> ReportRunClaim | None:
        claimed_at = now or utc_now()
        run = await db.scalar(select(ReportRun).where(ReportRun.id == run_id).with_for_update())
        if run is None or run.state in REPORT_TERMINAL_STATES or run.state == "NEEDS_INPUT":
            return None
        pending_due = run.state == "PENDING" and (
            run.available_at is None or run.available_at <= claimed_at
        )
        expired = run.state == "PROCESSING" and (
            run.lease_expires_at is None or run.lease_expires_at <= claimed_at
        )
        if not pending_due and not expired:
            return None
        if int(run.attempt_count or 0) >= self.max_attempts:
            run.state = "FAILED"
            run.stage = "FAILED"
            run.error_code = "REPORT_MAX_ATTEMPTS_EXCEEDED"
            run.error_message = "报告任务超过最大尝试次数"
            run.finished_at = claimed_at
            self._clear_lease(run)
            await db.commit()
            return None
        token = uuid4().hex
        run.state = "PROCESSING"
        run.stage = "PREPARING"
        run.attempt_count = int(run.attempt_count or 0) + 1
        run.lease_token = token
        run.lease_owner = worker_id[:128]
        run.lease_expires_at = claimed_at + timedelta(seconds=self.lease_seconds)
        run.started_at = run.started_at or claimed_at
        run.finished_at = None
        run.error_code = None
        run.error_message = None
        await db.commit()
        return ReportRunClaim(str(run.id), token, int(run.attempt_count))

    async def complete(
        self,
        db: AsyncSession,
        claim: ReportRunClaim,
        *,
        state: str,
        stage: str,
        report_ir: dict | None,
        validation_report: dict,
        manifest: dict,
        analysis_coverage: dict | None,
        now: datetime | None = None,
    ) -> bool:
        completed_at = now or utc_now()
        if state not in {"SUCCEEDED", "NEEDS_INPUT"}:
            raise ValueError("报告处理器返回了无效完成状态")
        values = {
            "state": state,
            "stage": stage,
            "report_ir": report_ir,
            "validation_report": validation_report,
            "manifest": manifest,
            "analysis_coverage": analysis_coverage,
            "lease_token": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "available_at": None,
            "finished_at": completed_at if state == "SUCCEEDED" else None,
            "updated_at": completed_at,
        }
        result = await db.execute(
            update(ReportRun)
            .where(
                ReportRun.id == claim.run_id,
                ReportRun.state == "PROCESSING",
                ReportRun.lease_token == claim.lease_token,
                ReportRun.lease_expires_at > completed_at,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0) != 1:
            await db.rollback()
            return False
        await db.commit()
        return True

    async def renew(
        self,
        db: AsyncSession,
        claim: ReportRunClaim,
        *,
        now: datetime | None = None,
    ) -> bool:
        renewed_at = now or utc_now()
        result = await db.execute(
            update(ReportRun)
            .where(
                ReportRun.id == claim.run_id,
                ReportRun.state == "PROCESSING",
                ReportRun.lease_token == claim.lease_token,
                ReportRun.lease_expires_at > renewed_at,
            )
            .values(
                lease_expires_at=renewed_at + timedelta(seconds=self.lease_seconds),
                updated_at=renewed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0) != 1:
            await db.rollback()
            return False
        await db.commit()
        return True

    async def fail(
        self,
        db: AsyncSession,
        claim: ReportRunClaim,
        error: Exception,
        *,
        retryable: bool,
        error_code: str,
        now: datetime | None = None,
    ) -> str | None:
        failed_at = now or utc_now()
        should_retry = retryable and claim.attempt_count < self.max_attempts
        values: dict[str, object] = {
            "lease_token": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "error_code": error_code[:64],
            "error_message": f"{type(error).__name__}: {error}"[:1000],
            "updated_at": failed_at,
        }
        if should_retry:
            delay = self.retry_delays[min(claim.attempt_count - 1, len(self.retry_delays) - 1)]
            state = "PENDING"
            values.update(
                state=state,
                stage="RETRY_WAIT",
                available_at=failed_at + timedelta(seconds=delay),
                finished_at=None,
                dispatch_status="PENDING",
                dispatch_attempt_count=0,
                dispatch_available_at=failed_at + timedelta(seconds=delay),
                dispatch_lease_token=None,
                dispatch_lease_expires_at=None,
                dispatch_error=None,
            )
        else:
            state = "FAILED"
            values.update(
                state=state,
                stage="FAILED",
                available_at=None,
                finished_at=failed_at,
            )
        result = await db.execute(
            update(ReportRun)
            .where(
                ReportRun.id == claim.run_id,
                ReportRun.state == "PROCESSING",
                ReportRun.lease_token == claim.lease_token,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0) != 1:
            await db.rollback()
            return None
        await db.commit()
        return state

    @staticmethod
    def _clear_lease(run: ReportRun) -> None:
        run.lease_token = None
        run.lease_owner = None
        run.lease_expires_at = None
