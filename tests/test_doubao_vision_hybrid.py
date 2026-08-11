from __future__ import annotations

import asyncio

import httpx
import pytest

from app.rag.core.llm.providers.doubao_vision import DoubaoVisionProvider


@pytest.mark.asyncio
async def test_hybrid_embedding_keeps_dense_and_sparse_from_one_request_per_text() -> None:
    request_count = 0
    in_flight = 0
    max_in_flight = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count, in_flight, max_in_flight
        request_count += 1
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        position = request_count
        return httpx.Response(
            200,
            json={
                "data": {
                    "embedding": [position * 0.1, position * 0.2],
                    "sparse_embedding": [{"index": position, "value": 0.5}],
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DoubaoVisionProvider(
        api_key="test-key",
        api_base_url="https://ark.example/embeddings/multimodal",
        http_client=client,
        max_concurrency=2,
    )
    try:
        result = await provider.embed_hybrid(["a", "b", "c"])
    finally:
        await provider.aclose()

    assert request_count == 3
    assert max_in_flight == 2
    assert len(result.dense_embeddings) == 3
    assert len(result.sparse_embeddings) == 3
    assert all(len(vector) == 2 for vector in result.dense_embeddings)


@pytest.mark.asyncio
async def test_dense_and_sparse_compatibility_methods_delegate_to_hybrid(monkeypatch) -> None:
    provider = DoubaoVisionProvider(api_key="test-key")
    calls = 0

    async def fake_hybrid(texts, model=None, **kwargs):
        nonlocal calls
        calls += 1
        from app.rag.core.llm.response import (
            HybridEmbeddingResult,
            SparseEmbedding,
            UsageInfo,
        )

        return HybridEmbeddingResult(
            model=model or "test-model",
            dense_embeddings=[[0.1, 0.2]],
            sparse_embeddings=[SparseEmbedding(indices=[1], values=[0.3])],
            usage=UsageInfo(),
        )

    monkeypatch.setattr(provider, "embed_hybrid", fake_hybrid)

    dense = await provider.embed(["text"], model="test-model")
    sparse = await provider.embed_sparse(["text"], model="test-model")

    assert calls == 2
    assert dense.embeddings == [[0.1, 0.2]]
    assert sparse.embeddings[0].indices == [1]
