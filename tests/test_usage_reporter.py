from __future__ import annotations

import pytest

from app.rag.services.usage_reporter import (
    drain_usage_reports,
    report_usage,
    report_usage_nowait,
)


@pytest.mark.asyncio
async def test_usage_reporting_does_not_require_a_database_table() -> None:
    await report_usage(
        user_id=12,
        provider_type="example",
        model_name="embedding-v1",
        stage="parse",
        operation="embed",
        prompt_tokens=34,
        total_tokens=34,
        config_id=56,
        task_id="task-78",
    )
    report_usage_nowait(
        user_id=12,
        provider_type="example",
        model_name="embedding-v1",
        stage="recall",
        operation="embed",
        total_tokens=2,
        config_id=56,
    )
    await drain_usage_reports()
