from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.arxiv_query_optimizer import (
    ArxivQueryOptimizationError,
    build_arxiv_search_query,
    optimize_arxiv_query_with_ai,
    rule_based_arxiv_query,
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
    assert calls[0]["max_tokens"] == 256


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
