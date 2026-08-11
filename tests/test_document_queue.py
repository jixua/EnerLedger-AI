from __future__ import annotations

from collections import deque
from datetime import timedelta

import pytest

from app.domain.models import Document
from app.services.document_queue import (
    DOCUMENT_STATUS_FAILED,
    DOCUMENT_STATUS_PROCESSING,
    DOCUMENT_STATUS_QUEUED,
    DocumentClaim,
    DocumentQueueService,
    utc_now,
)


class _ExecuteResult:
    def __init__(self, rowcount: int = 1):
        self.rowcount = rowcount


class _FakeSession:
    def __init__(self, scalar_values=(), *, rowcounts=()):
        self.scalar_values = deque(scalar_values)
        self.rowcounts = deque(rowcounts)
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.scalar_values.popleft() if self.scalar_values else None

    async def execute(self, statement):
        self.statements.append(statement)
        return _ExecuteResult(self.rowcounts.popleft() if self.rowcounts else 1)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _document(*, status: str, attempts: int = 0) -> Document:
    now = utc_now()
    return Document(
        id=7,
        dataset_id=9,
        user_id=11,
        filename="report.pdf",
        file_type="pdf",
        file_size=10,
        raw_bucket="raw",
        raw_object_key="raw/report.pdf",
        status=status,
        version=1,
        attempt_count=attempts,
        available_at=now - timedelta(seconds=1),
        queued_at=now - timedelta(seconds=2),
        created_at=now - timedelta(seconds=3),
        reparse_requested=False,
    )


@pytest.mark.asyncio
async def test_pushed_claim_recovers_expired_processing_lease() -> None:
    now = utc_now()
    document = _document(status=DOCUMENT_STATUS_PROCESSING, attempts=1)
    document.lease_token = "old-token"
    document.lease_expires_at = now - timedelta(seconds=1)
    db = _FakeSession([document])
    queue = DocumentQueueService(lease_seconds=60, max_attempts=3, retry_delays=(1,))

    claim = await queue.claim_document(
        db,
        document_id=7,
        worker_id="worker-a",
        now=now,
    )

    assert claim is not None
    assert claim.recovered_expired_lease is True
    assert claim.attempt_count == 2
    assert document.status == DOCUMENT_STATUS_PROCESSING
    assert document.lease_token == claim.lease_token != "old-token"
    assert document.lease_owner == "worker-a"
    assert document.lease_expires_at == now + timedelta(seconds=60)
    assert db.statements[0]._for_update_arg.skip_locked is False
    assert db.commits == 1


@pytest.mark.asyncio
async def test_pushed_claim_targets_only_the_message_document() -> None:
    now = utc_now()
    document = _document(status=DOCUMENT_STATUS_QUEUED)
    db = _FakeSession([document])
    queue = DocumentQueueService(lease_seconds=60, max_attempts=3, retry_delays=(1,))

    claim = await queue.claim_document(
        db,
        document_id=7,
        worker_id="rabbit-worker",
        now=now,
    )

    assert claim is not None
    assert claim.document_id == 7
    statement = db.statements[0]
    assert statement._limit_clause is None
    assert statement._for_update_arg is not None
    assert statement._for_update_arg.skip_locked is False
    assert "document.id" in str(statement.whereclause)
    assert document.status == DOCUMENT_STATUS_PROCESSING


@pytest.mark.asyncio
async def test_exhausted_pushed_task_is_failed_instead_of_staying_processing() -> None:
    now = utc_now()
    exhausted = _document(status=DOCUMENT_STATUS_PROCESSING, attempts=3)
    exhausted.lease_expires_at = now - timedelta(seconds=1)
    exhausted.lease_token = "expired"
    db = _FakeSession([exhausted])
    queue = DocumentQueueService(lease_seconds=60, max_attempts=3, retry_delays=(1,))

    assert (
        await queue.claim_document(
            db,
            document_id=7,
            worker_id="worker-a",
            now=now,
        )
        is None
    )
    assert exhausted.status == DOCUMENT_STATUS_FAILED
    assert exhausted.error_code == "MAX_ATTEMPTS_EXCEEDED"
    assert exhausted.lease_token is None
    assert db.commits == 1


@pytest.mark.asyncio
async def test_failure_requeues_with_backoff_then_finalizes_at_attempt_limit() -> None:
    now = utc_now()
    queue = DocumentQueueService(lease_seconds=60, max_attempts=3, retry_delays=(10, 20))
    claim = DocumentClaim(7, "token", 1, False, False)
    retry_db = _FakeSession(rowcounts=[1])

    target = await queue.fail_or_retry(retry_db, claim, RuntimeError("temporary"), now=now)

    assert target == DOCUMENT_STATUS_QUEUED
    params = retry_db.statements[0].compile().params
    assert params["status"] == DOCUMENT_STATUS_QUEUED
    assert params["available_at"] == now + timedelta(seconds=10)
    assert params["lease_token"] is None
    assert retry_db.commits == 1

    final_claim = DocumentClaim(7, "token", 3, False, False)
    final_db = _FakeSession(rowcounts=[1])
    target = await queue.fail_or_retry(final_db, final_claim, RuntimeError("still bad"), now=now)
    assert target == DOCUMENT_STATUS_FAILED
    assert final_db.statements[0].compile().params["status"] == DOCUMENT_STATUS_FAILED


@pytest.mark.asyncio
async def test_lost_lease_cannot_renew_or_write_failure_state() -> None:
    queue = DocumentQueueService(lease_seconds=60, max_attempts=3, retry_delays=(1,))
    claim = DocumentClaim(7, "stale-token", 1, False, False)

    renew_db = _FakeSession(rowcounts=[0])
    assert await queue.renew_lease(renew_db, claim) is False
    assert renew_db.commits == 0
    assert renew_db.rollbacks == 1

    failure_db = _FakeSession(rowcounts=[0])
    assert await queue.fail_or_retry(failure_db, claim, RuntimeError("late")) is None
    assert failure_db.commits == 0
    assert failure_db.rollbacks == 1
