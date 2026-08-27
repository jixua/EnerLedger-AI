"""FastAPI 到独立 Pi Agent 服务的受保护 HTTP 客户端。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from app.rag.config import settings


class PiAgentUnavailableError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.PI_SERVICE_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


async def stream_pi_agent(payload: dict) -> AsyncIterator[str]:
    timeout = httpx.Timeout(
        connect=settings.AGENT_TOOL_TIMEOUT_SECONDS,
        read=None,
        write=settings.AGENT_TOOL_TIMEOUT_SECONDS,
        pool=settings.AGENT_TOOL_TIMEOUT_SECONDS,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{settings.PI_SERVICE_BASE_URL.rstrip('/')}/internal/agent/runs",
                headers=_headers(),
                json=payload,
            ) as response:
                if response.status_code != 200:
                    raise PiAgentUnavailableError(f"Pi service returned {response.status_code}")
                async for chunk in response.aiter_text():
                    if chunk:
                        yield chunk
    except httpx.HTTPError as exc:
        raise PiAgentUnavailableError("Pi service is unavailable") from exc


async def pi_agent_readiness() -> bool:
    try:
        async with httpx.AsyncClient(timeout=settings.AGENT_TOOL_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{settings.PI_SERVICE_BASE_URL.rstrip('/')}/internal/agent/readiness",
                headers=_headers(),
            )
        return response.status_code == 200 and response.json().get("ready") is True
    except (httpx.HTTPError, ValueError):
        return False
