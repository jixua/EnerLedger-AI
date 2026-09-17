"""带附件的对话回合：判定用户是要生成报告，还是就着附件提问。

此前只要附件里存在主体材料，服务端就无条件进入报告分类，用户问「这两份材料分别是
什么」也会被要求先选报告类型。这一步在角色判定之后、创建报告之前做一次意图判定，
把「提问」放回普通问答路径。

模型不可用、超时或产出不可解析时回落到 REPORT——这与改动前的行为一致。报告生成是
这条链路的主要用法，宁可让用户重新措辞，也不要在模型不稳时把生成请求答成一段闲聊。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.core.llm.provider_lifecycle import aclose_dataset_execution_contexts
from app.rag.core.llm.user_model_resolver import aresolve_model
from app.rag.core.prompts import (
    TURN_INTENT_MAX_OUTPUT_TOKENS,
    TURN_INTENT_SYSTEM_PROMPT,
    TURN_INTENT_TIMEOUT_SECONDS,
    build_turn_intent_user_prompt,
    parse_turn_intent_reply,
)
from app.rag.observability.logging import logger

REPORT_INTENT = "REPORT"
QUESTION_INTENT = "QUESTION"
DEFAULT_INTENT = REPORT_INTENT


@dataclass(frozen=True)
class TurnIntent:
    """一轮对话的意图。

    ``decided_by`` 用于诊断与测试：``model`` 模型判定、``fallback`` 回落默认意图、
    ``none`` 没有附件、不由这里决定路由。
    """

    intent: str
    decided_by: str = "fallback"


async def decide_turn_intent(
    *,
    db: AsyncSession,
    user_id: int,
    config_id: int,
    query: str,
    filenames: list[str],
) -> TurnIntent:
    """判定这一轮是生成报告还是提问。任何异常都不外抛。"""

    if not filenames:  # 没有附件时不由这里决定路由
        return TurnIntent(intent=DEFAULT_INTENT, decided_by="fallback")

    resolved = None
    try:
        resolved = await aresolve_model(
            user_id=user_id, config_id=config_id, capability="CHAT", db=db
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 意图判定是路由增强项，取不到模型不阻塞
        logger.bind(
            event="turn_intent_model_resolution_failed",
            outcome="degraded",
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        ).warning("[agent] turn intent model resolution failed")

    if resolved is not None:
        try:
            result = await asyncio.wait_for(
                resolved.provider.generate(
                    prompt=build_turn_intent_user_prompt(query=query, filenames=filenames),
                    system_prompt=TURN_INTENT_SYSTEM_PROMPT,
                    temperature=0.0,
                    max_tokens=TURN_INTENT_MAX_OUTPUT_TOKENS,
                ),
                timeout=TURN_INTENT_TIMEOUT_SECONDS,
            )
            intent = parse_turn_intent_reply(result.content)
            if intent is not None:
                return TurnIntent(intent=intent, decided_by="model")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 判定失败回落默认意图，不阻塞
            logger.bind(
                event="turn_intent_decision_failed",
                outcome="degraded",
                error_type=type(exc).__name__,
                error_message=str(exc)[:200],
            ).warning("[agent] turn intent decision failed")
        finally:
            await aclose_dataset_execution_contexts([], extra_models=[resolved])

    return TurnIntent(intent=DEFAULT_INTENT, decided_by="fallback")
