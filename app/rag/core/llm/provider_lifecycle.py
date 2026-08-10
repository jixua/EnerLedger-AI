"""每请求模型 adapter 的统一资源收敛。"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any

from loguru import logger

_CONTEXT_MODEL_FIELDS = (
    "dense_embedding",
    "sparse_embedding",
    "enhancement_chat",
    "enhancement_vision",
    "rerank",
)


async def aclose_resolved_models(models: Iterable[Any]) -> None:
    """按 provider 对象身份去重关闭；关闭失败只记日志，不掩盖主链路结果。"""

    providers: list[Any] = []
    seen: set[int] = set()
    for model in models:
        if model is None:
            continue
        provider = getattr(model, "provider", None)
        if provider is None or id(provider) in seen:
            continue
        seen.add(id(provider))
        providers.append(provider)

    for provider in providers:
        try:
            closer = getattr(provider, "aclose", None) or getattr(provider, "close", None)
            if closer is None:
                continue
            result = closer()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - 资源关闭不能覆盖业务终态
            logger.bind(
                event="llm_provider_close_failed",
                provider_type=getattr(provider, "provider_type", type(provider).__name__),
                error_type=type(exc).__name__,
            ).warning("模型 Provider 资源关闭失败")


async def aclose_dataset_execution_contexts(
    contexts: Iterable[Any],
    *,
    extra_models: Iterable[Any] = (),
) -> None:
    """关闭 DatasetExecutionContext 中的所有模型以及额外模型。"""

    models = list(extra_models)
    for context in contexts:
        if context is None:
            continue
        models.extend(getattr(context, field, None) for field in _CONTEXT_MODEL_FIELDS)
    await aclose_resolved_models(models)
