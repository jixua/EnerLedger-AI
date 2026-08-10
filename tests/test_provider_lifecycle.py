from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.rag.core.llm.base_provider import BaseProvider
from app.rag.core.llm.provider_lifecycle import aclose_resolved_models
from app.rag.core.llm.response import GenerateResult, StreamChunk


class _ClosableHttpClient:
    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class _WrappedClient:
    def __init__(self, client: _ClosableHttpClient) -> None:
        self._http_client = client


class _Provider(BaseProvider):
    def __init__(self, client: _ClosableHttpClient) -> None:
        super().__init__(provider_type="fake", provider_name="fake", api_key="secret")
        self._client = _WrappedClient(client)

    async def generate(self, *args, **kwargs) -> GenerateResult:
        raise NotImplementedError

    async def stream(self, *args, **kwargs) -> AsyncIterator[StreamChunk]:
        if False:
            yield StreamChunk(delta="")


class _Resolved:
    def __init__(self, provider: BaseProvider) -> None:
        self.provider = provider


@pytest.mark.asyncio
async def test_base_provider_closes_nested_http_client() -> None:
    client = _ClosableHttpClient()
    provider = _Provider(client)

    await provider.aclose()

    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_resolved_model_close_deduplicates_provider_identity() -> None:
    client = _ClosableHttpClient()
    provider = _Provider(client)

    await aclose_resolved_models([_Resolved(provider), _Resolved(provider)])

    assert client.close_calls == 1
