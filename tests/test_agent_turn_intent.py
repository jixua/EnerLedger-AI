"""带附件的对话回合：意图判定（模型判断 + 回落现状）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import app.services.agent_turn_intent as intent_service
from app.rag.core.prompts import build_turn_intent_user_prompt, parse_turn_intent_reply
from app.services.agent_turn_intent import (
    QUESTION_INTENT,
    REPORT_INTENT,
    TurnIntent,
    decide_turn_intent,
)


class _Provider:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def generate(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(content=self._reply)


def _stub_model(monkeypatch, reply: str | None, *, raises: Exception | None = None) -> list[Any]:
    """把模型解析替换成桩，返回记录释放调用的列表。"""

    async def _resolve(**_kwargs: Any) -> Any:
        if raises is not None:
            raise raises
        return SimpleNamespace(provider=_Provider(reply or ""))

    closed: list[Any] = []

    async def _close(*_args: Any, **kwargs: Any) -> None:
        closed.append(kwargs.get("extra_models"))

    monkeypatch.setattr(intent_service, "aresolve_model", _resolve)
    monkeypatch.setattr(intent_service, "aclose_dataset_execution_contexts", _close)
    return closed


# --- 解析模型回复 -----------------------------------------------------------


def test_parse_accepts_plain_json() -> None:
    assert parse_turn_intent_reply('{"intent": "QUESTION"}') == QUESTION_INTENT


def test_parse_accepts_fenced_json_and_normalizes_case() -> None:
    assert parse_turn_intent_reply('```json\n{"intent":"report"}\n```') == REPORT_INTENT


def test_parse_rejects_unknown_intent_and_garbage() -> None:
    assert parse_turn_intent_reply('{"intent": "SUMMARIZE"}') is None
    assert parse_turn_intent_reply("用户想提问") is None
    assert parse_turn_intent_reply("") is None


def test_prompt_carries_the_question_and_filenames() -> None:
    prompt = build_turn_intent_user_prompt(
        query="这两份材料分别是什么", filenames=["材料.pdf", "模板.docx"]
    )
    assert "这两份材料分别是什么" in prompt
    assert "材料.pdf" in prompt and "模板.docx" in prompt


# --- 判定 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_question_intent_routes_to_qa(monkeypatch) -> None:
    _stub_model(monkeypatch, '{"intent": "QUESTION"}')

    decision = await decide_turn_intent(
        db=None, user_id=1, config_id=1, query="这两份材料分别是什么", filenames=["材料.pdf"]
    )

    assert decision == TurnIntent(intent=QUESTION_INTENT, decided_by="model")


@pytest.mark.asyncio
async def test_report_intent_keeps_report_flow(monkeypatch) -> None:
    _stub_model(monkeypatch, '{"intent": "REPORT"}')

    decision = await decide_turn_intent(
        db=None, user_id=1, config_id=1, query="按这份材料出一份报告", filenames=["材料.pdf"]
    )

    assert decision == TurnIntent(intent=REPORT_INTENT, decided_by="model")


@pytest.mark.asyncio
async def test_provider_is_released(monkeypatch) -> None:
    closed = _stub_model(monkeypatch, '{"intent": "QUESTION"}')

    await decide_turn_intent(
        db=None, user_id=1, config_id=1, query="这份文件讲了什么", filenames=["材料.pdf"]
    )

    assert len(closed) == 1


@pytest.mark.asyncio
async def test_without_attachments_skips_the_model(monkeypatch) -> None:
    async def _unexpected(**_kwargs: Any) -> Any:
        raise AssertionError("没有附件时不应调用模型")

    monkeypatch.setattr(intent_service, "aresolve_model", _unexpected)

    decision = await decide_turn_intent(
        db=None, user_id=1, config_id=1, query="随便问一句", filenames=[]
    )

    assert decision == TurnIntent(intent=REPORT_INTENT, decided_by="fallback")


@pytest.mark.asyncio
async def test_model_unavailable_falls_back_to_report(monkeypatch) -> None:
    """回落方向保持改动前的行为：模型不稳时不要把生成请求答成闲聊。"""

    _stub_model(monkeypatch, None, raises=RuntimeError("provider 不可用"))

    decision = await decide_turn_intent(
        db=None, user_id=1, config_id=1, query="这两份材料分别是什么", filenames=["材料.pdf"]
    )

    assert decision == TurnIntent(intent=REPORT_INTENT, decided_by="fallback")


@pytest.mark.asyncio
async def test_unparseable_reply_falls_back_to_report(monkeypatch) -> None:
    _stub_model(monkeypatch, "我觉得用户在提问")

    decision = await decide_turn_intent(
        db=None, user_id=1, config_id=1, query="这份文件讲了什么", filenames=["材料.pdf"]
    )

    assert decision == TurnIntent(intent=REPORT_INTENT, decided_by="fallback")
