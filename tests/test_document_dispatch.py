from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

import app.services.document_dispatch as dispatch_module
from app.domain.models import Document
from app.rag.core.mq.messages import DocumentIngestionMessage
from app.services.document_dispatch import (
    DISPATCH_PENDING,
    DispatchClaim,
    DocumentParseDispatcher,
    mark_document_dispatch_pending,
)


class _Publisher:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, message) -> None:
        self.messages.append(message)


class _FailingPublisher:
    async def send(self, _message) -> None:
        raise RuntimeError("broker unavailable")


class _Result:
    rowcount = 1


class _Session:
    def __init__(self) -> None:
        self.statements = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


@pytest.mark.asyncio
async def test_dispatch_message_contains_only_document_identity() -> None:
    publisher = _Publisher()
    document = Document(id=7, user_id=11, dataset_id=9, version=3)

    await DocumentParseDispatcher(publisher).dispatch(document)

    assert len(publisher.messages) == 1
    message = publisher.messages[0]
    assert isinstance(message, DocumentIngestionMessage)
    payload = message.get_payload()
    assert payload.document_id == 7
    assert payload.user_id == 11
    assert payload.dataset_id == 9
    assert payload.document_version == 3
    assert "filename" not in message.serialize()


def test_document_dispatch_message_round_trip_and_rejects_wrong_type() -> None:
    message = DocumentIngestionMessage.build(
        document_id=7,
        user_id=11,
        dataset_id=9,
        document_version=3,
    )

    payload = DocumentIngestionMessage.parse_msg(message.serialize())

    assert payload.document_id == 7
    with pytest.raises(Exception, match="\u6587\u6863\u89e3\u6790\u6d88\u606f\u65e0\u6548"):
        DocumentIngestionMessage.parse_msg(
            '{"mq_type":"OTHER","payload":{"document_id":7}}'
        )


def test_resetting_outbox_clears_stale_dispatch_lease() -> None:
    document = Document(
        id=7,
        user_id=11,
        dataset_id=9,
        version=3,
        dispatch_status="DISPATCHING",
        dispatch_attempt_count=4,
        dispatch_lease_token="stale-token",
        dispatch_error="old failure",
    )

    mark_document_dispatch_pending(document)

    assert document.dispatch_status == DISPATCH_PENDING
    assert document.dispatch_attempt_count == 0
    assert document.dispatch_available_at is not None
    assert document.dispatch_lease_token is None
    assert document.dispatch_error is None


@pytest.mark.asyncio
async def test_failed_publish_returns_claim_to_durable_pending_state(monkeypatch) -> None:
    sessions = []

    @asynccontextmanager
    async def session_context():
        session = _Session()
        sessions.append(session)
        yield session

    monkeypatch.setattr(dispatch_module, "get_db_context", session_context)
    dispatcher = DocumentParseDispatcher(_FailingPublisher())
    claim = DispatchClaim(7, 11, 9, 3, "lease-token")

    published = await dispatcher._publish_claim(claim)

    assert published is False
    assert len(sessions) == 1
    assert sessions[0].commits == 1
    values = sessions[0].statements[0].compile().params
    assert values["dispatch_status"] == DISPATCH_PENDING
    assert values["dispatch_lease_token"] is None
    assert "broker unavailable" in values["dispatch_error"]
