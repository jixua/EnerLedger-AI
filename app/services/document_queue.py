"""Document parsing state transitions and lease fencing stored in MySQL.

RabbitMQ is responsible for active delivery. MySQL remains the authoritative state and
idempotency boundary; workers claim the specific document ID carried by each message.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Document
from app.domain.time import utc_now
from app.rag.config import settings

DOCUMENT_STATUS_QUEUED = "QUEUED"
DOCUMENT_STATUS_PROCESSING = "PROCESSING"
DOCUMENT_STATUS_READY = "READY"
DOCUMENT_STATUS_FAILED = "FAILED"


def reset_document_for_queue(document: Document, *, reparse: bool) -> None:
    """把非活跃文档重置为可消费任务；重新解析会创建新的来源版本。"""

    now = utc_now()
    document.status = DOCUMENT_STATUS_QUEUED
    document.attempt_count = 0
    document.available_at = now
    document.queued_at = now
    document.processing_started_at = None
    document.finished_at = None
    document.lease_token = None
    document.lease_owner = None
    document.lease_expires_at = None
    document.error_code = None
    document.error_message = None
    document.parse_quality_status = None
    document.parse_quality = None
    if reparse:
        document.version = int(document.version or 1) + 1
        document.reparse_requested = True


def configured_retry_delays() -> tuple[int, ...]:
    return tuple(
        int(item.strip())
        for item in settings.DOCUMENT_QUEUE_RETRY_DELAYS_SECONDS.split(",")
        if item.strip()
    )


@dataclass(frozen=True, slots=True)
class DocumentClaim:
    document_id: int
    lease_token: str
    attempt_count: int
    recovered_expired_lease: bool
    reparse_requested: bool


@dataclass(frozen=True, slots=True)
class DocumentQueueSnapshot:
    document_id: int
    user_id: int
    dataset_id: int
    version: int
    status: str
    available_at: datetime | None
    lease_expires_at: datetime | None


class DocumentQueueService:
    """Atomic state transitions used by the RabbitMQ-driven worker."""

    def __init__(
        self,
        *,
        lease_seconds: int | None = None,
        max_attempts: int | None = None,
        retry_delays: tuple[int, ...] | None = None,
    ) -> None:
        self.lease_seconds = int(
            settings.DOCUMENT_QUEUE_LEASE_SECONDS if lease_seconds is None else lease_seconds
        )
        self.max_attempts = int(
            settings.DOCUMENT_QUEUE_MAX_ATTEMPTS if max_attempts is None else max_attempts
        )
        self.retry_delays = configured_retry_delays() if retry_delays is None else retry_delays
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正整数")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts 必须为正整数")
        if not self.retry_delays or any(delay < 0 for delay in self.retry_delays):
            raise ValueError("retry_delays 必须包含非负整数")

    async def claim_document(
        self,
        db: AsyncSession,
        *,
        document_id: int,
        worker_id: str,
        now: datetime | None = None,
    ) -> DocumentClaim | None:
        """Claim one explicitly dispatched document; never scan the queue table."""

        claimed_at = now or utc_now()
        document = await db.scalar(
            select(Document)
            .where(Document.id == document_id)
            .with_for_update()
        )
        if document is None:
            return None
        queued_due = document.status == DOCUMENT_STATUS_QUEUED and (
            document.available_at is None or document.available_at <= claimed_at
        )
        expired_processing = (
            document.status == DOCUMENT_STATUS_PROCESSING
            and (
                document.lease_expires_at is None
                or document.lease_expires_at <= claimed_at
            )
        )
        if not queued_due and not expired_processing:
            return None
        if int(document.attempt_count or 0) >= self.max_attempts:
            document.status = DOCUMENT_STATUS_FAILED
            document.error_code = "MAX_ATTEMPTS_EXCEEDED"
            document.error_message = (
                document.error_message or "解析任务超过最大尝试次数"
            )[:1000]
            document.finished_at = claimed_at
            self._clear_lease(document)
            await db.commit()
            return None

        recovered = document.status == DOCUMENT_STATUS_PROCESSING
        token = uuid4().hex
        document.status = DOCUMENT_STATUS_PROCESSING
        document.attempt_count = int(document.attempt_count or 0) + 1
        document.lease_token = token
        document.lease_owner = worker_id[:128]
        document.lease_expires_at = claimed_at + timedelta(seconds=self.lease_seconds)
        document.processing_started_at = claimed_at
        document.finished_at = None
        document.error_code = None
        document.error_message = None
        await db.commit()
        return DocumentClaim(
            document_id=int(document.id),
            lease_token=token,
            attempt_count=int(document.attempt_count),
            recovered_expired_lease=recovered,
            reparse_requested=bool(document.reparse_requested),
        )

    async def snapshot(
        self,
        db: AsyncSession,
        *,
        document_id: int,
    ) -> DocumentQueueSnapshot | None:
        row = (
            await db.execute(
                select(
                    Document.id,
                    Document.user_id,
                    Document.dataset_id,
                    Document.version,
                    Document.status,
                    Document.available_at,
                    Document.lease_expires_at,
                ).where(Document.id == document_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return DocumentQueueSnapshot(
            document_id=int(row.id),
            user_id=int(row.user_id),
            dataset_id=int(row.dataset_id),
            version=int(row.version),
            status=str(row.status),
            available_at=row.available_at,
            lease_expires_at=row.lease_expires_at,
        )

    async def renew_lease(
        self,
        db: AsyncSession,
        claim: DocumentClaim,
        *,
        now: datetime | None = None,
    ) -> bool:
        renewed_at = now or utc_now()
        statement = (
            update(Document)
            .where(
                Document.id == claim.document_id,
                Document.status == DOCUMENT_STATUS_PROCESSING,
                Document.lease_token == claim.lease_token,
                Document.lease_expires_at > renewed_at,
            )
            .values(
                lease_expires_at=renewed_at + timedelta(seconds=self.lease_seconds),
                updated_at=renewed_at,
            )
            .execution_options(synchronize_session=False)
        )
        result = await db.execute(statement)
        if int(result.rowcount or 0) != 1:
            await db.rollback()
            return False
        await db.commit()
        return True

    async def owns_lease(
        self,
        db: AsyncSession,
        claim: DocumentClaim,
        *,
        now: datetime | None = None,
    ) -> bool:
        checked_at = now or utc_now()
        value = await db.scalar(
            select(Document.id).where(
                Document.id == claim.document_id,
                Document.status == DOCUMENT_STATUS_PROCESSING,
                Document.lease_token == claim.lease_token,
                Document.lease_expires_at > checked_at,
            )
        )
        return value is not None

    async def fail_or_retry(
        self,
        db: AsyncSession,
        claim: DocumentClaim,
        error: BaseException,
        *,
        retryable: bool = True,
        error_code: str | None = None,
        parse_quality_status: str | None = None,
        parse_quality: dict[str, object] | None = None,
        now: datetime | None = None,
    ) -> str | None:
        """由当前 lease 的 owner 记录失败；失去 lease 时返回 ``None``。"""

        failed_at = now or utc_now()
        should_retry = retryable and claim.attempt_count < self.max_attempts
        message = f"{type(error).__name__}: {error}"[:1000]
        code = (error_code or type(error).__name__).upper()[:64]
        values: dict[str, object] = {
            "lease_token": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "error_code": code,
            "error_message": message,
            "updated_at": failed_at,
        }
        if parse_quality_status is not None:
            values["parse_quality_status"] = parse_quality_status
        if parse_quality is not None:
            values["parse_quality"] = parse_quality
        if should_retry:
            delay = self.retry_delays[min(claim.attempt_count - 1, len(self.retry_delays) - 1)]
            target_status = DOCUMENT_STATUS_QUEUED
            values.update(
                status=target_status,
                available_at=failed_at + timedelta(seconds=delay),
                queued_at=failed_at,
                finished_at=None,
            )
        else:
            target_status = DOCUMENT_STATUS_FAILED
            values.update(
                status=target_status,
                available_at=None,
                finished_at=failed_at,
            )

        statement = (
            update(Document)
            .where(
                Document.id == claim.document_id,
                Document.status == DOCUMENT_STATUS_PROCESSING,
                Document.lease_token == claim.lease_token,
                Document.lease_expires_at > failed_at,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        result = await db.execute(statement)
        if int(result.rowcount or 0) != 1:
            await db.rollback()
            return None
        await db.commit()
        return target_status

    @staticmethod
    def _clear_lease(document: Document) -> None:
        document.lease_token = None
        document.lease_owner = None
        document.lease_expires_at = None
