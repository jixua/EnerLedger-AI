from __future__ import annotations

import pytest

from app.rag.core.markdown_parser import MarkdownParser
from app.rag.core.markdown_parser.heading_hierarchy import HeadingHierarchyGate
from app.rag.core.markdown_parser.structural_classifier import StructuralTextClassifier
from app.rag.core.splitter.candidate_boundary_chunker import CandidateBoundaryChunker
from app.rag.core.splitter.pipeline_chunker import StructuredSemanticChunker
from app.rag.core.splitter.stage_models import SplitInput
from app.rag.core.splitter.stage_two_noop import NoopStageTwoAlgorithm
from app.rag.core.splitter.stage_two_semantic_depth import SemanticDepthWindowStageTwo
from app.rag.core.splitter.structural_boundaries import StructuralBoundaryDetector
from app.rag.core.splitter.validators import FinalChunkSetValidator


class _LengthTokenizer:
    def count_tokens(self, text: str) -> int:
        return len(text)

    def truncate_text(self, text: str, max_tokens: int) -> tuple[str, int]:
        return text[:max_tokens], max(0, len(text) - max_tokens)


def _split_input(markdown: str) -> SplitInput:
    return SplitInput(elements=MarkdownParser().parse(markdown).elements)


def test_chinese_legal_chapters_are_hard_boundaries_and_heading_trails() -> None:
    coarse = CandidateBoundaryChunker(
        tokenizer=_LengthTokenizer(),
        min_candidate_chunk_tokens=128,
    ).run(
        _split_input(
            "# 碳排放权登记管理规则（试行）\n\n"
            "第一章 总 则\n\n第一条 为了规范登记管理。\n\n"
            "第二章 账户管理\n\n第二条 登记主体应当开立账户。"
        )
    )

    mixed = [chunk for chunk in coarse.chunks if chunk.role == "mixed"]
    assert len(mixed) == 2
    assert mixed[0].content.startswith("# 碳排放权登记管理规则")
    assert mixed[1].content.startswith("第二章 账户管理")
    assert mixed[0].heading_trail == ["碳排放权登记管理规则（试行）", "第一章 总 则"]
    assert mixed[1].heading_trail == ["碳排放权登记管理规则（试行）", "第二章 账户管理"]


def test_chinese_legal_articles_are_candidate_boundaries_after_soft_minimum() -> None:
    coarse = CandidateBoundaryChunker(
        tokenizer=_LengthTokenizer(),
        min_candidate_chunk_tokens=20,
    ).run(
        _split_input(
            "第一章 总则\n\n"
            "第一条 短条款。\n\n"
            "第二条 继续聚合到达软下限。\n\n"
            "第三条 达到下限后从本条前切分。"
        )
    )

    mixed = [chunk for chunk in coarse.chunks if chunk.role == "mixed"]
    assert len(mixed) == 2
    assert "第二条" in mixed[0].content
    assert mixed[1].content.startswith("第三条")


def test_decimal_standard_sections_use_document_level_hierarchy() -> None:
    coarse = CandidateBoundaryChunker(
        tokenizer=_LengthTokenizer(),
        min_candidate_chunk_tokens=20,
    ).run(
        _split_input(
            "1 范围\n\n本文件规定了核算要求。\n\n"
            "2 规范性引用文件\n\n下列文件必不可少。\n\n"
            "3 术语和定义\n\n3.1\n温室气体\n\n术语正文。\n\n"
            "3.2 报告主体\n\n术语正文。\n\n4 基本原则\n\n原则正文。"
        )
    )

    mixed = [chunk for chunk in coarse.chunks if chunk.role == "mixed"]
    assert [chunk.content.splitlines()[0] for chunk in mixed] == [
        "1 范围",
        "2 规范性引用文件",
        "3 术语和定义",
        "3.2 报告主体",
        "4 基本原则",
    ]
    assert mixed[2].heading_trail == ["3 术语和定义", "3.1 温室气体"]
    assert mixed[3].heading_trail == ["3 术语和定义", "3.2 报告主体"]


def test_single_numeric_paragraph_is_not_misclassified_as_structure() -> None:
    coarse = CandidateBoundaryChunker(
        tokenizer=_LengthTokenizer(),
        min_candidate_chunk_tokens=10,
    ).run(_split_input("说明\n\n2024 年排放量数据\n\n结束"))

    assert len([chunk for chunk in coarse.chunks if chunk.role == "mixed"]) == 1


def test_page_number_sequence_is_not_structural_boundary() -> None:
    split_input = _split_input(
        "前言\n\n1\n\n第一页正文。\n\n2\n\n第二页正文。\n\n3\n\n第三页正文。"
    )

    assert StructuralBoundaryDetector.detect(split_input.elements) == {}


@pytest.mark.parametrize("marker", ["{number} ", "{number}. ", "{number}、", "{number}) "])
def test_numbered_steps_without_nested_hierarchy_are_not_sections(marker: str) -> None:
    split_input = _split_input(
        "操作说明\n\n"
        f"{marker.format(number=1)}第一步操作\n\n输入数据。\n\n"
        f"{marker.format(number=2)}第二步操作\n\n检查结果。\n\n"
        f"{marker.format(number=3)}第三步操作\n\n提交。"
    )

    assert StructuralBoundaryDetector.detect(split_input.elements) == {}


def test_split_decimal_number_and_title_elements_form_one_heading_trail() -> None:
    coarse = CandidateBoundaryChunker(
        tokenizer=_LengthTokenizer(),
        min_candidate_chunk_tokens=10,
    ).run(
        _split_input(
            "1 范围\n\n正文。\n\n2 规范性引用文件\n\n正文。\n\n"
            "3 术语和定义\n\n3.1\n\n温室气体\n\n术语正文。\n\n"
            "3.2\n\n报告主体\n\n术语正文。\n\n4 基本原则\n\n正文。"
        )
    )

    mixed = [chunk for chunk in coarse.chunks if chunk.role == "mixed"]
    assert any("3.1 温室气体" in chunk.heading_trail for chunk in mixed)
    assert any("3.2 报告主体" in chunk.heading_trail for chunk in mixed)


def test_heading_gate_and_splitter_share_the_same_structure_classifier() -> None:
    samples = ["第一章 总则", "3.1 温室气体", "一、总体要求", "（一）指导思想"]

    for sample in samples:
        assert StructuralTextClassifier.classify(sample) is not None
        assert HeadingHierarchyGate._looks_like_hierarchy_clue(sample) is True
    assert HeadingHierarchyGate._looks_like_hierarchy_clue("12") is False
    assert HeadingHierarchyGate._looks_like_hierarchy_clue("1. 第一步操作") is False
    assert HeadingHierarchyGate._looks_like_hierarchy_clue("1) 第一步操作") is False


def test_chinese_policy_outline_has_hierarchy_without_treating_body_list_as_hard_boundary() -> None:
    coarse = CandidateBoundaryChunker(
        tokenizer=_LengthTokenizer(),
        min_candidate_chunk_tokens=20,
    ).run(
        _split_input(
            "一、总体要求\n\n（一）指导思想\n\n指导思想正文很长很长。\n\n"
            "（二）工作原则\n\n工作原则正文很长很长。\n\n"
            "二、主要目标\n\n（一）阶段目标\n\n阶段目标正文。"
        )
    )

    mixed = [chunk for chunk in coarse.chunks if chunk.role == "mixed"]
    assert mixed[0].heading_trail == ["一、总体要求", "（一）指导思想"]
    assert any(chunk.content.startswith("二、主要目标") for chunk in mixed)


@pytest.mark.asyncio
async def test_noop_applies_deterministic_size_fallback_and_hard_max() -> None:
    tokenizer = _LengthTokenizer()
    candidate = CandidateBoundaryChunker(
        tokenizer=tokenizer,
        min_candidate_chunk_tokens=20,
    )
    noop = NoopStageTwoAlgorithm(
        tokenizer=tokenizer,
        max_chunk_tokens=30,
        hard_max_tokens=40,
        min_chunk_tokens=10,
    )
    chunker = StructuredSemanticChunker(
        candidate_chunker=candidate,
        stage_two_algorithm_name="noop",
        stage_two_algorithm=noop,
        final_validator=FinalChunkSetValidator(tokenizer=tokenizer, hard_max_tokens=40),
    )

    chunks = await chunker.arun(
        _split_input("这是一个没有任何标题的长段落。" * 20)
    )

    assert len(chunks) > 1
    assert all(tokenizer.count_tokens(chunk.content.strip()) <= 40 for chunk in chunks)
    assert all(chunk.metadata["split_strategy"] == "candidate_boundary + noop" for chunk in chunks)


@pytest.mark.asyncio
async def test_semantic_stage_falls_back_to_deterministic_packing_when_embedding_fails() -> None:
    class _FailingEmbedder:
        async def embed(self, **_kwargs):
            raise RuntimeError("embedding unavailable")

    tokenizer = _LengthTokenizer()
    coarse_set = CandidateBoundaryChunker(
        tokenizer=tokenizer,
        min_candidate_chunk_tokens=20,
    ).run(_split_input("这是一个没有标题的长段落。" * 30))
    final_set = await SemanticDepthWindowStageTwo(
        tokenizer=tokenizer,
        embedder=_FailingEmbedder(),
        max_chunk_tokens=30,
        hard_max_tokens=40,
        min_chunk_tokens=10,
    ).run(coarse_set)

    assert len(final_set.chunks) > 1
    assert all(tokenizer.count_tokens(chunk.content.strip()) <= 40 for chunk in final_set.chunks)
    assert final_set.metadata["semantic_fallback"] is True
    assert final_set.metadata["semantic_fallback_reason"] == "RuntimeError"
