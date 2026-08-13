from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.api.documents import _document_parse_time_ms


def test_api_duration_reports_complete_processing_attempt() -> None:
    started_at = datetime(2026, 8, 13, 7, 0, 0)
    document = SimpleNamespace(
        processing_started_at=started_at,
        finished_at=started_at + timedelta(minutes=2, seconds=3, milliseconds=456),
        parse_time_ms=1200,
    )

    assert _document_parse_time_ms(document) == 123_456


def test_api_duration_normalizes_timezone_aware_timestamps() -> None:
    document = SimpleNamespace(
        processing_started_at=datetime(2026, 8, 13, 7, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 13, 7, 0, 1, 680000),
        parse_time_ms=1200,
    )

    assert _document_parse_time_ms(document) == 1680


def test_api_duration_falls_back_for_legacy_or_invalid_timestamps() -> None:
    document = SimpleNamespace(
        processing_started_at=None,
        finished_at=None,
        parse_time_ms=1680,
    )
    assert _document_parse_time_ms(document) == 1680

    document.processing_started_at = datetime(2026, 8, 13, 7, 0, 1)
    document.finished_at = datetime(2026, 8, 13, 7, 0, 0)
    assert _document_parse_time_ms(document) == 1680
