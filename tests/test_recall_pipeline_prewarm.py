from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from app.rag.application import recall_pipeline_provider


@pytest.mark.asyncio
async def test_startup_prewarm_builds_pipeline_and_warms_bm25(monkeypatch) -> None:
    pipeline = Mock()
    retriever = Mock()
    retriever.warmup = AsyncMock()
    monkeypatch.setattr(
        recall_pipeline_provider,
        "get_recall_pipeline",
        Mock(return_value=pipeline),
    )
    monkeypatch.setattr(
        recall_pipeline_provider,
        "_enabled_sources",
        lambda: ["bm25", "dense"],
    )
    monkeypatch.setattr(
        recall_pipeline_provider,
        "_get_bm25_retriever",
        Mock(return_value=retriever),
    )
    vector_prewarm = Mock()
    monkeypatch.setattr(
        recall_pipeline_provider,
        "_prewarm_vector_query_runtime",
        vector_prewarm,
    )

    await recall_pipeline_provider.prewarm_recall_pipeline()

    recall_pipeline_provider.get_recall_pipeline.assert_called_once_with()
    vector_prewarm.assert_called_once_with()
    retriever.warmup.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_startup_prewarm_skips_bm25_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(recall_pipeline_provider, "get_recall_pipeline", Mock())
    monkeypatch.setattr(recall_pipeline_provider, "_enabled_sources", lambda: ["dense"])
    vector_prewarm = Mock()
    monkeypatch.setattr(
        recall_pipeline_provider,
        "_prewarm_vector_query_runtime",
        vector_prewarm,
    )
    bm25_factory = Mock()
    monkeypatch.setattr(recall_pipeline_provider, "_get_bm25_retriever", bm25_factory)

    await recall_pipeline_provider.prewarm_recall_pipeline()

    recall_pipeline_provider.get_recall_pipeline.assert_called_once_with()
    vector_prewarm.assert_called_once_with()
    bm25_factory.assert_not_called()


@pytest.mark.asyncio
async def test_startup_prewarm_allows_bm25_timeout_degradation(monkeypatch) -> None:
    retriever = Mock()
    retriever.warmup = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(recall_pipeline_provider, "get_recall_pipeline", Mock())
    monkeypatch.setattr(recall_pipeline_provider, "_enabled_sources", lambda: ["bm25"])
    monkeypatch.setattr(
        recall_pipeline_provider,
        "_prewarm_vector_query_runtime",
        Mock(),
    )
    monkeypatch.setattr(
        recall_pipeline_provider,
        "_get_bm25_retriever",
        Mock(return_value=retriever),
    )

    await recall_pipeline_provider.prewarm_recall_pipeline()

    retriever.warmup.assert_awaited_once_with()
