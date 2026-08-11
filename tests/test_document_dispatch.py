from __future__ import annotations

import pytest

from app.domain.models import Document
from app.rag.core.mq.messages import DocumentIngestionMessage
from app.services.document_dispatch import DocumentParseDispatcher


class _Publisher:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, message) -> None:
        self.messages.append(message)


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
