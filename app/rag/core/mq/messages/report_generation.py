"""Minimal message contract for report generation runs."""

from __future__ import annotations

from pydantic import Field

from app.rag.core.mq.exceptions import MQSerializationError
from app.rag.core.mq.message import AbstractMessage, MessagePayload


class ReportGenerationPayload(MessagePayload):
    run_id: str = Field(min_length=1, max_length=36)
    user_id: int = Field(gt=0)
    # 对话直传的材料没有文档可指，这两项为空。判断来源看 report_run.source_kind，
    # 不要靠它们是否为空反推。
    document_id: int | None = Field(default=None, gt=0)
    document_version: int | None = Field(default=None, gt=0)


class ReportGenerationMessage(AbstractMessage):
    MQ_NAME = "energy_carbon.report.generate"
    MQ_TYPE = "REPORT_GENERATION"

    def __init__(self, payload: ReportGenerationPayload) -> None:
        self._payload = payload

    @classmethod
    def get_mq_name(cls) -> str:
        return cls.MQ_NAME

    @classmethod
    def get_mq_type(cls) -> str:
        return cls.MQ_TYPE

    def get_payload(self) -> ReportGenerationPayload:
        return self._payload

    def get_routing_key(self) -> str:
        return self._payload.run_id

    def get_log_fields(self) -> dict[str, object]:
        return {
            "message_id": self._payload.message_id,
            "run_id": self._payload.run_id,
            "user_id": self._payload.user_id,
            "document_id": self._payload.document_id,
            "document_version": self._payload.document_version,
        }

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        user_id: int,
        document_id: int | None,
        document_version: int | None,
    ) -> ReportGenerationMessage:
        return cls(
            ReportGenerationPayload(
                run_id=run_id,
                user_id=user_id,
                document_id=document_id,
                document_version=document_version,
            )
        )

    @classmethod
    def parse_msg(cls, raw: str) -> ReportGenerationPayload:
        try:
            envelope = cls.deserialize_envelope(raw)
            if envelope.get("mq_type") != cls.MQ_TYPE:
                raise ValueError("unexpected mq_type")
            return ReportGenerationPayload.model_validate(envelope.get("payload"))
        except Exception as exc:
            raise MQSerializationError(f"报告生成消息无效：{type(exc).__name__}") from None
