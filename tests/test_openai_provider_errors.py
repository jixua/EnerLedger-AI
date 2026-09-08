from __future__ import annotations

import httpx
import pytest

from app.rag.core.llm.exceptions import (
    InsufficientBalanceError,
    InvalidResponseError,
    RateLimitError,
    public_llm_error,
)
from app.rag.core.llm.providers.openai import OpenAIClient


def _client(status_code: int, payload: dict) -> OpenAIClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload, request=request)

    client = OpenAIClient(
        api_key="test-key",
        api_base_url="https://example.invalid/chat/completions",
        max_retries=0,
        provider_type="deepseek",
    )
    client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_deepseek_402_is_classified_as_insufficient_balance() -> None:
    client = _client(
        402,
        {"error": {"code": "insufficient_balance", "message": "Insufficient Balance"}},
    )
    try:
        with pytest.raises(InsufficientBalanceError) as raised:
            await client.chat_completions(model="deepseek-chat", messages=[])
    finally:
        await client.close()

    failure = public_llm_error(raised.value)
    assert failure.code == "LLM_INSUFFICIENT_BALANCE"
    assert failure.retryable is False
    assert "DeepSeek" in failure.message
    assert "余额不足" in failure.message


@pytest.mark.asyncio
async def test_insufficient_quota_inside_429_is_not_mislabeled_as_rate_limit() -> None:
    client = _client(
        429,
        {"error": {"code": "insufficient_quota", "message": "quota has been exhausted"}},
    )
    try:
        with pytest.raises(InsufficientBalanceError):
            await client.chat_completions(model="deepseek-chat", messages=[])
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_429_remains_a_retryable_rate_limit() -> None:
    client = _client(429, {"error": {"message": "Rate limit reached"}})
    try:
        with pytest.raises(RateLimitError) as raised:
            await client.chat_completions(model="deepseek-chat", messages=[])
    finally:
        await client.close()

    failure = public_llm_error(raised.value)
    assert failure.code == "LLM_RATE_LIMITED"
    assert failure.retryable is True
    assert "频率或并发数" in failure.message


@pytest.mark.asyncio
async def test_streaming_call_uses_the_same_balance_classification() -> None:
    client = _client(402, {"error": {"message": "Insufficient Balance"}})
    try:
        with pytest.raises(InsufficientBalanceError):
            async for _chunk in client.stream_chat_completions(
                model="deepseek-chat",
                messages=[],
            ):
                pass
    finally:
        await client.close()


def test_rejected_request_detail_redacts_api_credentials() -> None:
    error = InvalidResponseError(
        message="invalid api_key=sk-secretvalue123456 for this model",
        provider_type="deepseek",
    )

    failure = public_llm_error(error)

    assert failure.code == "LLM_REQUEST_REJECTED"
    assert "sk-secretvalue123456" not in failure.message
    assert "[REDACTED]" in failure.message
