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
async def test_required_lambdamart_keeps_model_order_for_short_query() -> None:
    ranker = _fake_ranker(confidence=0.0)
    ranker._predict = lambda *_args: (
        ["chunk-1", "chunk-2"],
        ["chunk-2", "chunk-1"],
        ["chunk-1", "chunk-2"],
        0.0,
    )
    try:
        result = await ranker.rank(
            query="产品碳足迹怎么做",
            routes={},
            candidate_contents={"chunk-1": "候选一", "chunk-2": "候选二"},
            allow_fallback=False,
        )
    finally:
        ranker.close()

    assert result.mode == "ltr_short_low_confidence"
    assert result.reason == "low_confidence_short_query"
    assert result.ranked_chunk_ids == ["chunk-2", "chunk-1"]


@pytest.mark.asyncio
async def test_optional_lambdamart_keeps_calibrated_short_query_fallback() -> None:
    ranker = _fake_ranker(confidence=0.0)
    ranker._predict = lambda *_args: (
        ["chunk-1", "chunk-2"],
        ["chunk-2", "chunk-1"],
        ["chunk-1", "chunk-2"],
        0.0,
    )
    try:
        result = await ranker.rank(
            query="产品碳足迹怎么做",
            routes={},
            candidate_contents={"chunk-1": "候选一", "chunk-2": "候选二"},
        )
    finally:
        ranker.close()

    assert result.mode == "hybrid_short_low_confidence"
    assert result.ranked_chunk_ids == ["chunk-1", "chunk-2"]


@pytest.mark.asyncio
async def test_short_query_policy_does_not_mask_inference_failures() -> None:
    ranker = _fake_ranker(confidence=0.0)
    ranker._predict = lambda *_args: (_ for _ in ()).throw(RuntimeError("broken model"))
    try:
        with pytest.raises(LambdaMartRankingRequiredError, match="inference failed"):
            await ranker.rank(
                query="产品碳足迹怎么做",
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
