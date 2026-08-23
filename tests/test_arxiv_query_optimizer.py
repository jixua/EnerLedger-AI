from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.arxiv_query_optimizer import (
    ArxivQueryOptimizationError,
    build_arxiv_search_query,
    optimize_arxiv_query_with_ai,
    rule_based_arxiv_query,
    rule_based_fallback_warning,
)


def test_rule_based_query_combines_words_instead_of_exact_full_phrase() -> None:
    result = rule_based_arxiv_query("lithium battery carbon footprint")

    assert result.optimized_query == "lithium AND battery AND carbon AND footprint"
    assert result.search_query == (
        'all:"lithium" AND all:"battery" AND all:"carbon" AND all:"footprint"'
    )
    assert result.mode == "RULES"


def test_search_query_preserves_common_academic_phrases_as_independent_terms() -> None:
    assert build_arxiv_search_query(("lithium-ion battery", "carbon footprint")) == (
        'all:"lithium-ion battery" AND all:"carbon footprint"'
    )


@pytest.mark.asyncio
async def test_ai_optimizer_translates_chinese_topic_and_returns_explainable_terms() -> None:
    calls: list[dict] = []

    class FakeProvider:
        async def generate(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content='{"terms":["lithium-ion battery","carbon emissions"]}'
            )

    result = await optimize_arxiv_query_with_ai(
        "动力电池碳排",
        provider=FakeProvider(),
        model_name="test-chat",
    )

    assert result.optimized_query == "lithium-ion battery AND carbon emissions"
    assert result.search_query == (
        'all:"lithium-ion battery" AND all:"carbon emissions"'
    )
    assert result.mode == "AI"
    assert result.model_name == "test-chat"
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["max_tokens"] == 512


@pytest.mark.asyncio
async def test_ai_optimizer_disables_thinking_and_requests_json_for_deepseek_v4() -> None:
    calls: list[dict] = []

    class FakeProvider:
        api_base_url = "https://api.deepseek.com/chat/completions"
        model_name = "deepseek-v4-flash"

        async def generate(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content='{"terms":["carbon footprint"]}',
                finish_reason="stop",
            )

    await optimize_arxiv_query_with_ai(
        "碳足迹",
        provider=FakeProvider(),
        model_name="deepseek-v4-flash",
    )

    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_ai_optimizer_reports_truncated_empty_output() -> None:
    class FakeProvider:
        async def generate(self, **kwargs):
            return SimpleNamespace(content="", finish_reason="length")

    with pytest.raises(ArxivQueryOptimizationError, match="token 上限"):
        await optimize_arxiv_query_with_ai(
            "碳足迹",
            provider=FakeProvider(),
            model_name="thinking-model",
        )


def test_chinese_rule_fallback_warning_does_not_claim_english_tokenization() -> None:
    warning = rule_based_fallback_warning("碳足迹", reason="AI 检索词优化失败")

    assert warning == "AI 检索词优化失败，已回退为原始主题检索（中文匹配结果可能有限）"


@pytest.mark.asyncio
async def test_ai_optimizer_rejects_non_json_output() -> None:
    class FakeProvider:
        async def generate(self, **kwargs):
            return SimpleNamespace(content="battery emissions")

    with pytest.raises(ArxivQueryOptimizationError, match="有效的检索词 JSON"):
        await optimize_arxiv_query_with_ai(
            "电池碳排",
            provider=FakeProvider(),
            model_name="test-chat",
        )
