from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.rag.core.pipeline.ltr import LambdaMartRanker, LambdaMartRankingRequiredError
from app.rag.core.pipeline.ltr.ranker import RankerMonitor


def _fake_ranker(*, confidence: float) -> LambdaMartRanker:
    ranker = object.__new__(LambdaMartRanker)
    ranker.manifest = {
        "model_version": "test-lambdamart",
        "timeout_ms": 1_000,
        "latency_budget_ms": 1_000,
    }
    ranker.short_fallback = {"max_query_chars": 15, "confidence_threshold": 0.1}
    ranker.monitor = RankerMonitor()
    ranker._inference_slots = asyncio.Semaphore(1)
    ranker._inference_executor = ThreadPoolExecutor(max_workers=1)
    ranker._inference_running = 0
    ranker._closed = False
    ranker._predict = lambda *_args: (["chunk-1"], ["chunk-1"], ["chunk-1"], confidence)
    return ranker


@pytest.mark.asyncio
async def test_required_lambdamart_rejects_short_query_weighted_fallback() -> None:
    ranker = _fake_ranker(confidence=0.0)
    try:
        with pytest.raises(LambdaMartRankingRequiredError):
            await ranker.rank(
                query="短问题",
                routes={},
                candidate_contents={"chunk-1": "候选正文"},
                allow_fallback=False,
            )
    finally:
        ranker.close()


@pytest.mark.asyncio
async def test_required_lambdamart_returns_only_model_ranking_mode() -> None:
    ranker = _fake_ranker(confidence=0.5)
    try:
        result = await ranker.rank(
            query="这是一个足够长的 LambdaMART 模型重排问题",
            routes={},
            candidate_contents={"chunk-1": "候选正文"},
            allow_fallback=False,
        )
    finally:
        ranker.close()

    assert result.mode == "ltr"
    assert result.ranked_chunk_ids == ["chunk-1"]
