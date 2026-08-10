"""基于 MySQL 8 document 表的轻量持久解析队列。

这里刻意不引入 Redis、RabbitMQ 或 Kafka。``SELECT ... FOR UPDATE SKIP LOCKED``
负责多 worker 竞争，短事务内签发 lease；心跳续租和 token CAS 防止过期 worker 覆盖
新 worker 的终态。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import case, func, or_, select, update
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


class DocumentQueueService:
    """MySQL durable queue 的原子状态转换。"""

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

    async def claim_next(
        self,
        db: AsyncSession,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> DocumentClaim | None:
        """抢占一个到期任务；锁只覆盖签发 lease 的短事务。

        ``skip_locked`` 让多个 worker 横向扩展时不会互相等待。超过最大尝试次数的
        异常记录在同一循环内收敛为 ``FAILED``，不会永久卡在 ``PROCESSING``。
        """

        while True:
            claimed_at = now or utc_now()
            due_queued = (
                (Document.status == DOCUMENT_STATUS_QUEUED)
                & or_(Document.available_at.is_(None), Document.available_at <= claimed_at)
            )
            expired_processing = (
                (Document.status == DOCUMENT_STATUS_PROCESSING)
                & (Document.lease_expires_at.is_not(None))
                & (Document.lease_expires_at <= claimed_at)
            )
            statement = (
                select(Document)
                .where(or_(due_queued, expired_processing))
                .order_by(
                    case((Document.status == DOCUMENT_STATUS_PROCESSING, 0), else_=1),
                    func.coalesce(Document.available_at, Document.created_at),
                    Document.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            document = await db.scalar(statement)
            if document is None:
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
                # 使用真实当前时间继续找下一条；测试传入的固定 now 仍保持确定性。
                continue

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
