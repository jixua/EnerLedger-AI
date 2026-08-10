from __future__ import annotations

from app.rag.api.schemas.mq import SendParseTaskRequest
from app.rag.api.schemas.parse import TaskSubmitRequest
from app.rag.config import Settings
from app.rag.core.mq.messages.parse_task import ParseTaskMessage, ParseTaskPayload
from app.rag.core.parser.pdf.models import PdfParseOptions

REQUIRED_PARSE_FIELDS = {
    "task_id": "task-1",
    "original_file_id": 101,
    "document_parse_task_id": 201,
    "user_id": 301,
    "dataset_id": 401,
    "file_type": "pdf",
    "source_bucket": "raw",
    "source_object_key": "documents/source.pdf",
    "source_filename": "source.pdf",
    "md_bucket": "private",
    "md_object_key": "documents/source.md",
}


def test_pdf_defaults_are_opendataloader_at_every_parse_entry() -> None:
    assert Settings.model_fields["PDF_PARSER_BACKEND"].default == "opendataloader"
    assert PdfParseOptions().backend == "opendataloader"
    assert TaskSubmitRequest(**REQUIRED_PARSE_FIELDS).pdf_parser_backend == "opendataloader"
    assert SendParseTaskRequest(**REQUIRED_PARSE_FIELDS).pdf_parser_backend == "opendataloader"
    assert ParseTaskPayload(**REQUIRED_PARSE_FIELDS).pdf_parser_backend == "opendataloader"
    assert (
        ParseTaskMessage.build(**REQUIRED_PARSE_FIELDS).get_payload().pdf_parser_backend
        == "opendataloader"
    )


def test_ltr_is_disabled_by_default_for_current_scope() -> None:
    assert Settings.model_fields["RECALL_LTR_MODE"].default == "off"
