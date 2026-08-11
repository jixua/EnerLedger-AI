"""Minimal message contract for actively dispatched document parsing."""

from __future__ import annotations

import json

from pydantic import Field

from app.rag.core.mq.exceptions import MQSerializationError
from app.rag.core.mq.message import AbstractMessage, MessagePayload


class DocumentIngestionPayload(MessagePayload):
    """Only carry identity; the consumer reloads authoritative state from MySQL."""

    document_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    dataset_id: int = Field(gt=0)
    document_version: int = Field(gt=0)


class DocumentIngestionMessage(AbstractMessage):
    MQ_NAME = "energy_carbon.document.parse"
    MQ_TYPE = "DOCUMENT_INGESTION"

    def __init__(self, payload: DocumentIngestionPayload) -> None:
        self._payload = payload

    @classmethod
    def get_mq_name(cls) -> str:
        return cls.MQ_NAME

    @classmethod
    def get_mq_type(cls) -> str:
        return cls.MQ_TYPE

    def get_payload(self) -> DocumentIngestionPayload:
        return self._payload

    def get_routing_key(self) -> str:
        return str(self._payload.document_id)

    def get_log_fields(self) -> dict[str, object]:
        return {
            "message_id": self._payload.message_id,
            "document_id": self._payload.document_id,
            "user_id": self._payload.user_id,
            "dataset_id": self._payload.dataset_id,
            "document_version": self._payload.document_version,
        }

    @classmethod
    def build(
        cls,
        *,
        document_id: int,
        user_id: int,
        dataset_id: int,
        document_version: int,
    ) -> DocumentIngestionMessage:
        return cls(
            DocumentIngestionPayload(
                document_id=document_id,
                user_id=user_id,
                dataset_id=dataset_id,
                document_version=document_version,
            )
        )

    @classmethod
    def parse_msg(cls, raw: str) -> DocumentIngestionPayload:
        try:
            envelope = json.loads(raw)
            if not isinstance(envelope, dict):
                raise ValueError("message must be an object")
            if envelope.get("mq_type") not in (None, cls.MQ_TYPE):
                raise ValueError("unexpected mq_type")
            payload = envelope.get("payload", envelope)
            return DocumentIngestionPayload.model_validate(payload)
        except Exception as exc:
            raise MQSerializationError(f"文档解析消息无效: {type(exc).__name__}") from None
