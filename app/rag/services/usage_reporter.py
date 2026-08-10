"""模型用量旁路接口。

当前最小版本不建立 ``llm_usage_log`` 表。保留 LinkRag 源码调用所需的函数签名，
但只写结构化调试日志，不访问 MySQL，也不影响解析、召回或生成主链路。
"""

from __future__ import annotations

from loguru import logger


async def report_usage(
    *,
    user_id: int | str,
    provider_type: str,
    model_name: str,
    stage: str,
    operation: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    config_id: int,
    task_id: str | None = None,
    latency_ms: int | None = None,
    status: str = "success",
) -> None:
    logger.bind(
        event="llm_usage_observed",
        user_id=str(user_id),
        provider_type=provider_type,
        model_name=model_name,
        stage=stage,
        operation=operation,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        config_id=config_id,
        task_id=task_id or "",
        latency_ms=latency_ms,
        status=status,
    ).debug("LLM usage observed without database persistence")


def report_usage_nowait(**kwargs) -> None:
    """非阻塞兼容入口；最小版本不调度任何数据库写任务。"""
    logger.bind(event="llm_usage_observed", **kwargs).debug(
        "LLM usage observed without database persistence"
    )


async def drain_usage_reports() -> None:
    """兼容生命周期接口；没有后台持久化任务需要等待。"""
