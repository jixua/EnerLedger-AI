from __future__ import annotations

import pytest

from app.rag.core.markdown_parser import MarkdownParser
from app.rag.core.parse_task_service import ParseTaskService
from app.rag.core.parser.pdf.models import PdfImageAsset
from app.rag.core.parser.pdf.service import PdfParserService
from app.rag.core.splitter.candidate_boundary_chunker import CandidateBoundaryChunker
from app.rag.core.splitter.stage_models import SplitInput
from app.rag.core.splitter.stage_two_semantic_depth import SemanticDepthWindowStageTwo
from app.rag.core.splitter.validators import CoarseChunkSetValidator


class _LengthTokenizer:
    def count_tokens(self, text: str) -> int:
        return len(text)

    def truncate_text(self, text: str, max_tokens: int) -> tuple[str, int]:
        return text[:max_tokens], max(0, len(text) - max_tokens)


class _UnexpectedEmbedder:
    async def embed(self, **_kwargs):
        pytest.fail("derived chunks and small source chunks must pass through without embedding")


def test_opendataloader_page_markers_become_element_metadata() -> None:
    result = MarkdownParser().parse(
        "<!-- ODL_PAGE:1 -->\n\n第一页正文\n\n"
        "<!-- ODL_PAGE:2 -->\n\n第二页正文"
    )

    assert ParseTaskService._apply_pdf_page_markers(result) == 2
    assert [element.content for element in result.elements] == ["第一页正文", "第二页正文"]
    assert [element.metadata["page_number"] for element in result.elements] == [1, 2]


def test_candidate_chunk_aggregates_real_page_range() -> None:
    result = MarkdownParser().parse("第一页正文\n\n第二页正文")
    result.elements[0].metadata["page_number"] = 3
    result.elements[1].metadata["page_number"] = 4

    split_input = SplitInput(elements=result.elements)
    coarse = CandidateBoundaryChunker(
        tokenizer=_LengthTokenizer(),
        min_candidate_chunk_tokens=10_000,
    ).run(split_input)

    assert len(coarse.chunks) == 1
    assert coarse.chunks[0].metadata["page_numbers"] == [3, 4]
    assert coarse.chunks[0].metadata["start_page"] == 3
    assert coarse.chunks[0].metadata["end_page"] == 4


def test_candidate_chunk_excludes_suppressed_assets_from_indexes_and_views() -> None:
    result = MarkdownParser().parse(
        "可检索正文\n\n![装饰图](https://assets.example/logo.png)"
    )
    result.elements[1].metadata["suppress_retrieval"] = True

    split_input = SplitInput(elements=result.elements)
    coarse = CandidateBoundaryChunker(
        tokenizer=_LengthTokenizer(),
        min_candidate_chunk_tokens=10_000,
    ).run(split_input)

    assert len(coarse.chunks) == 1
    source = coarse.chunks[0]
    assert source.source_element_indexes == [0]
    assert [view.element_index for view in source.element_views] == [0]
    assert source.element_types == ["paragraph"]
    assert "装饰图" not in source.content
    CoarseChunkSetValidator().validate(coarse, split_input)


def test_opendataloader_angle_bracket_image_is_replaced_once_in_place() -> None:
    original = (
        "<!-- ODL_PAGE:2 -->\n\n"
        "图片前正文\n\n"
        "![](<images/page-2-boundary.png>)\n\n"
        "图片后正文"
    )
    asset = PdfImageAsset(
        page_number=2,
        index=1,
        object_key="parsed/2/page-002-image-01.png",
        url="https://assets.example/page-002-image-01.png",
        source_path="images/page-2-boundary.png",
    )

    markdown = PdfParserService()._inject_image_references(
        original,
        "opendataloader",
        [asset],
    )

    expected_reference = "![page-2-image-1](https://assets.example/page-002-image-01.png)"
    assert markdown == (
        "<!-- ODL_PAGE:2 -->\n\n"
        "图片前正文\n\n"
        f"{expected_reference}\n\n"
        "图片后正文"
    )
    assert markdown.count(asset.url) == 1


@pytest.mark.asyncio
async def test_derived_image_and_table_chunks_keep_page_provenance_to_final() -> None:
    result = MarkdownParser().parse(
        "<!-- ODL_PAGE:2 -->\n\n"
        "![排放边界](https://assets.example/boundary.png)\n\n"
        "<!-- ODL_PAGE:3 -->\n\n"
        "| 项目 | 数值 |\n"
        "| --- | --- |\n"
        "| 排放量 | 42 |"
    )
    assert ParseTaskService._apply_pdf_page_markers(result) == 2

    tokenizer = _LengthTokenizer()
    coarse_set = CandidateBoundaryChunker(
        tokenizer=tokenizer,
        min_candidate_chunk_tokens=10_000,
    ).run(SplitInput(elements=result.elements))
    derived = [chunk for chunk in coarse_set.chunks if chunk.role == "derived_element"]

    assert [chunk.metadata["element_type"] for chunk in derived] == ["image", "table"]
    assert [
        {
            key: chunk.metadata[key]
            for key in ("page_number", "page_numbers", "start_page", "end_page")
        }
        for chunk in derived
    ] == [
        {"page_number": 2, "page_numbers": [2], "start_page": 2, "end_page": 2},
        {"page_number": 3, "page_numbers": [3], "start_page": 3, "end_page": 3},
    ]

    final_set = await SemanticDepthWindowStageTwo(
        tokenizer=tokenizer,
        embedder=_UnexpectedEmbedder(),
        max_chunk_tokens=10_000,
        hard_max_tokens=12_000,
        min_chunk_tokens=1,
    ).run(coarse_set)
    final_derived = [chunk for chunk in final_set.chunks if chunk.role == "derived_element"]

    assert [
        {
            key: chunk.metadata[key]
            for key in ("page_number", "page_numbers", "start_page", "end_page")
        }
        for chunk in final_derived
    ] == [
        {"page_number": 2, "page_numbers": [2], "start_page": 2, "end_page": 2},
        {"page_number": 3, "page_numbers": [3], "start_page": 3, "end_page": 3},
    ]
