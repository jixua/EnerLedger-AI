"""Publish document parsing commands after the authoritative row is committed."""

from __future__ import annotations

from typing import Protocol

from app.domain.models import Document
from app.rag.core.mq.messages import DocumentIngestionMessage
from app.rag.services.mq_service import MQService


class MessagePublisher(Protocol):
    async def send(self, message: DocumentIngestionMessage) -> None: ...


class DocumentParseDispatcher:
    def __init__(self, publisher: MessagePublisher | None = None) -> None:
        self._publisher = publisher or MQService()

    async def dispatch(self, document: Document) -> None:
        await self._publisher.send(
            DocumentIngestionMessage.build(
                document_id=int(document.id),
                user_id=int(document.user_id),
                dataset_id=int(document.dataset_id),
                document_version=int(document.version),
            )
        )
