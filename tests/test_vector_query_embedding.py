from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app.rag.core.storage.vector.facade import VectorStorageFacade
from app.rag.core.storage.vector.query_embedding import aembed_dense_query


class _Provider:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    async def embed(self, *, texts: list[str], model: str):
        self.calls.append((texts, model))
        return SimpleNamespace(
            embeddings=[[0.25, 0.75]],
            usage=SimpleNamespace(prompt_tokens=4, total_tokens=4),
        )


@pytest.mark.asyncio
async def test_dense_query_embedding_uses_only_resolved_provider() -> None:
    provider = _Provider()
    resolved = SimpleNamespace(provider=provider, model_name="query-model")

    vector, usage = await aembed_dense_query(resolved, "  carbon accounting  ")

    assert vector == [0.25, 0.75]
    assert usage.total_tokens == 4
    assert provider.calls == [(["carbon accounting"], "query-model")]


@pytest.mark.asyncio
async def test_dense_query_embedding_rejects_invalid_vector_count() -> None:
    provider = _Provider()

    async def empty_embed(**_kwargs):
        return SimpleNamespace(embeddings=[], usage=None)

    provider.embed = empty_embed
    resolved = SimpleNamespace(provider=provider, model_name="query-model")

    with pytest.raises(ValueError, match="returned 0 vectors"):
        await aembed_dense_query(resolved, "carbon accounting")


@pytest.mark.asyncio
async def test_facade_dense_query_does_not_import_chunking_factory(monkeypatch) -> None:
    provider = _Provider()
    resolved = SimpleNamespace(
        provider=provider,
        model_name="query-model",
        provider_type="test-provider",
        config_id=17,
    )

    class _Store:
        async def _search_chunks(self, **_kwargs):
            return []

    # Any regression to build_chunk_embedding_pipeline would import this module
    # and fail here before the provider is called.
    monkeypatch.setitem(sys.modules, "app.rag.core.splitter.factory", None)
    facade = VectorStorageFacade(qdrant_store=_Store())

    result = await facade.search_dense_chunks(
        query="carbon accounting",
        user_id=1,
        set_id=17,
        resolved_model=resolved,
    )

    assert result.hits == []
    assert result.model_name == "query-model"
    assert provider.calls == [(["carbon accounting"], "query-model")]
