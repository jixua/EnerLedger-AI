"""出参时间戳必须带时区：库内是无时区 UTC，直接出参会被客户端按本地时间解析。"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from pydantic import BaseModel

import app.domain.schemas as schemas
from app.api.agent_conversations import list_conversations
from app.api.reports import _run_dict
from app.domain.schemas import (
    AuthToken,
    CrawlerSubmissionRead,
    DatasetRead,
    DocumentFolderRead,
    UtcTimestampModel,
)
from app.domain.time import as_utc, utc_now


def _naive() -> datetime:
    return datetime(2026, 9, 17, 8, 46, 54)


# --- as_utc -----------------------------------------------------------------


def test_as_utc_marks_naive_utc_as_utc() -> None:
    marked = as_utc(_naive())
    assert marked == datetime(2026, 9, 17, 8, 46, 54, tzinfo=UTC)
    assert marked.utcoffset() == timedelta(0)


def test_as_utc_converts_other_offsets_to_utc() -> None:
    shanghai = datetime(2026, 9, 17, 16, 46, 54, tzinfo=timezone(timedelta(hours=8)))
    assert as_utc(shanghai) == datetime(2026, 9, 17, 8, 46, 54, tzinfo=UTC)


def test_as_utc_passes_through_none_and_non_datetimes() -> None:
    assert as_utc(None) is None
    assert as_utc("2026-09-17T08:46:54") == "2026-09-17T08:46:54"
    assert as_utc(7) == 7


def test_utc_now_stays_naive_for_mysql_datetime() -> None:
    assert utc_now().tzinfo is None


# --- 出参模型基类 -----------------------------------------------------------


class _SamplePayload(UtcTimestampModel):
    moment: datetime
    maybe: datetime | None
    label: str


def test_utc_base_model_normalizes_timestamps_and_keeps_other_fields() -> None:
    payload = _SamplePayload(moment=_naive(), maybe=None, label="x")

    assert payload.moment == datetime(2026, 9, 17, 8, 46, 54, tzinfo=UTC)
    assert payload.maybe is None
    assert payload.label == "x"
    assert '"moment":"2026-09-17T08:46:54Z"' in payload.model_dump_json()


def test_dataset_read_serializes_created_at_with_utc_marker() -> None:
    payload = DatasetRead(
        id=1,
        name="d",
        description=None,
        status="ACTIVE",
        dense_embedding_config_id=1,
        sparse_embedding_config_id=1,
        chat_config_id=1,
        vision_config_id=None,
        created_at=_naive(),
        updated_at=_naive(),
    )
    dumped = payload.model_dump_json()
    assert '"created_at":"2026-09-17T08:46:54Z"' in dumped
    assert '"updated_at":"2026-09-17T08:46:54Z"' in dumped


def test_folder_read_serializes_timestamps_with_utc_marker() -> None:
    payload = DocumentFolderRead(
        id=1, dataset_id=1, parent_id=None, name="f", created_at=_naive(), updated_at=_naive()
    )
    assert '"created_at":"2026-09-17T08:46:54Z"' in payload.model_dump_json()


def test_auth_token_serializes_expiry_with_utc_marker() -> None:
    payload = AuthToken(access_token="t", expires_at=_naive())
    assert '"expires_at":"2026-09-17T08:46:54Z"' in payload.model_dump_json()


def test_crawler_submission_read_normalizes_timestamps() -> None:
    submission = CrawlerSubmissionRead(
        document_id=1,
        dataset_id=1,
        dataset_name="d",
        filename="a.pdf",
        file_type="pdf",
        file_size=10,
        content_type="application/pdf",
        document_status="PENDING_REVIEW",
        source_type="UPLOAD",
        source_url=None,
        source_title=None,
        source_metadata=None,
        review_status="PENDING",
        review_note=None,
        reviewed_at=_naive(),
        created_at=_naive(),
    )

    assert submission.reviewed_at == datetime(2026, 9, 17, 8, 46, 54, tzinfo=UTC)
    assert submission.created_at.tzinfo is not None


# --- 裸 dict 出参 -----------------------------------------------------------


def _report_run_stub() -> SimpleNamespace:
    return SimpleNamespace(
        id="run-1",
        document_id=1,
        dataset_id=1,
        document_version=1,
        report_type="R1",
        template_snapshot={"name": "产品碳足迹评价报告"},
        template_id="r1",
        template_version="1.0.0",
        custom_template_document_id=None,
        custom_template_document_version=None,
        mode="GENERATE",
        language="zh-CN",
        reporting_year=2025,
        output_formats=["ONLINE"],
        llm_config_id=1,
        llm_snapshot_version=1,
        state="SUCCEEDED",
        stage="COMPLETED",
        error_code=None,
        error_message=None,
        started_at=_naive(),
        finished_at=_naive(),
        created_at=_naive(),
        updated_at=_naive(),
    )


def test_report_run_payload_marks_timestamps_as_utc() -> None:
    payload = _run_dict(_report_run_stub())

    for key in ("started_at", "finished_at", "created_at", "updated_at"):
        assert payload[key].tzinfo is not None, key
    assert payload["created_at"] == datetime(2026, 9, 17, 8, 46, 54, tzinfo=UTC)


def test_report_run_payload_exposes_readable_type_name() -> None:
    payload = _run_dict(_report_run_stub())

    assert payload["report_type_name"] == "产品碳足迹评价报告"
    assert payload["report_type"] == "R1"  # 内部编号仍保留，供接口侧匹配使用


class _FakeScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    def __init__(self, conversations: list[Any], turn_count: int = 2) -> None:
        self._conversations = conversations
        self._turn_count = turn_count

    async def scalars(self, _statement: Any) -> _FakeScalars:
        return _FakeScalars(self._conversations)

    async def scalar(self, _statement: Any) -> int:
        return self._turn_count


@pytest.mark.asyncio
async def test_conversation_payload_marks_timestamps_as_utc() -> None:
    conversation = SimpleNamespace(
        id="c-1", title="t", user_id=1, created_at=_naive(), updated_at=_naive()
    )

    payload = await list_conversations(user_id=1, db=_FakeSession([conversation]), limit=50)

    assert payload[0]["created_at"] == datetime(2026, 9, 17, 8, 46, 54, tzinfo=UTC)
    assert payload[0]["updated_at"].tzinfo is not None


# --- 结构守卫 ---------------------------------------------------------------


def _mentions_datetime(annotation: Any) -> bool:
    if annotation is datetime:
        return True
    return any(_mentions_datetime(argument) for argument in get_args(annotation))


def test_every_schema_with_a_timestamp_inherits_the_utc_base() -> None:
    offenders = [
        name
        for name, member in vars(schemas).items()
        if inspect.isclass(member)
        and member.__module__ == schemas.__name__
        and issubclass(member, BaseModel)
        and any(
            _mentions_datetime(field.annotation) for field in member.model_fields.values()
        )
        and not issubclass(member, UtcTimestampModel)
    ]

    assert not offenders, (
        "以下模型带 datetime 字段但未继承 UtcTimestampModel，出参会被客户端按本地时间解析："
        f"{offenders}"
    )
