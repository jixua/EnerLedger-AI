from app.rag.core.markdown_parser.models import ElementType
from app.rag.core.markdown_parser.parser import MarkdownParser
from app.rag.core.parser.pdf.table_structure import PdfTableStructureExtractor
from app.rag.core.splitter.element_derived_chunker import DerivedElementChunkBuilder
from app.rag.core.splitter.overlap import ChunkOverlapConfig, ChunkOverlapper


class _CharacterTokenizer:
    def count_tokens(self, text: str) -> int:
        return len(text)

    def truncate_text(self, text: str, max_tokens: int) -> tuple[str, bool]:
        return text[:max_tokens], len(text) > max_tokens


def _builder() -> DerivedElementChunkBuilder:
    tokenizer = _CharacterTokenizer()
    overlapper = ChunkOverlapper(
        tokenizer,
        ChunkOverlapConfig(enabled=False, tokens=0),
    )
    return DerivedElementChunkBuilder(tokenizer, overlapper)


def test_html_table_is_one_structured_table_element() -> None:
    markdown = """<!-- ODL_PAGE:1 -->
正文
<table>
  <tr><th rowspan="2">类别</th><th colspan="2">排放量</th></tr>
  <tr><th>2024</th><th>2025</th></tr>
  <tr><td>范围一</td><td>10</td><td>9</td></tr>
</table>
表后正文
"""

    result = MarkdownParser().parse(markdown, source_file="carbon.pdf")

    tables = [element for element in result.elements if element.type is ElementType.TABLE]
    assert len(tables) == 1
    assert tables[0].metadata == {"table_format": "html"}
    assert 'rowspan="2"' in tables[0].content
    assert 'colspan="2"' in tables[0].content
    assert len(result.tables) == 1
    assert result.tables[0].content == tables[0].content


def test_single_line_html_table_is_detected_between_paragraphs() -> None:
    result = MarkdownParser().parse(
        "前文\n<table><tr><td>1</td></tr></table>\n后文"
    )

    assert [element.type for element in result.elements] == [
        ElementType.PARAGRAPH,
        ElementType.TABLE,
        ElementType.PARAGRAPH,
    ]


def test_unclosed_html_table_does_not_consume_following_document() -> None:
    result = MarkdownParser().parse("<table>\n<tr><td>1</td></tr>\n\n后续正文")

    assert not result.tables
    assert result.elements[-1].content == "后续正文"


def test_html_table_chunk_uses_structured_matrix_and_dimensions() -> None:
    markdown = """<!-- ODL_PAGE:3 -->
<table>
  <caption>能碳排放统计</caption>
  <tr><th rowspan="2">类别</th><th colspan="2">排放量</th></tr>
  <tr><th>2024</th><th>2025</th></tr>
  <tr><td>范围一</td><td>10</td><td>9</td></tr>
</table>
"""
    parse_result = MarkdownParser().parse(markdown, source_file="carbon.pdf")
    table = next(
        element for element in parse_result.elements if element.type is ElementType.TABLE
    )
    structure = PdfTableStructureExtractor().extract(markdown).tables[0].to_dict()
    table.metadata["table_structure"] = structure
    table.metadata["page_number"] = 3

    result = _builder().build([table], [[]])

    assert len(result.derived_chunks) == 1
    derived = result.derived_chunks[0]
    assert "<table>" not in derived.content
    assert "表名：能碳排放统计" in derived.content
    assert "列结构：类别；排放量 > 2024；排放量 > 2025" in derived.content
    assert "数据行1：范围一 | 10 | 9" in derived.content
    assert derived.metadata["table_row_count"] == 3
    assert derived.metadata["table_col_count"] == 3
    assert derived.metadata["table_inline_in_source"] is False
    assert derived.metadata["table_structure"]["cells"][0]["row_span"] == 2
    assert derived.metadata["table_structure"]["cells"][1]["column_span"] == 2


def test_merged_continuation_chunk_contains_every_page_once() -> None:
    markdown = """<!-- ODL_PAGE:1 -->
表 2 企业能源消费量

| 年份 | 能源品种 | 消费量 |
| --- | --- | --- |
| 2024 | 电力 | 120 MWh |

<!-- ODL_PAGE:2 -->
表 2 企业能源消费量（续）

| 年份 | 能源品种 | 消费量 |
| --- | --- | --- |
| 2025 | 天然气 | 80 Nm3 |
"""
    parse_result = MarkdownParser().parse(markdown, source_file="carbon.pdf")
    table_elements = [
        element for element in parse_result.elements if element.type is ElementType.TABLE
    ]
    merged = PdfTableStructureExtractor().extract(markdown).tables[0]
    table_elements[0].metadata.update(
        {
            "table_structure": merged.to_dict(),
            "page_numbers": [1, 2],
            "start_page": 1,
            "end_page": 2,
        }
    )
    table_elements[1].metadata["suppress_retrieval"] = True

    result = _builder().build(table_elements, [[], []])

    assert len(result.derived_chunks) == 1
    derived = result.derived_chunks[0]
    assert "数据行1：2024 | 电力 | 120 MWh" in derived.content
    assert "数据行2：2025 | 天然气 | 80 Nm3" in derived.content
    assert derived.content.count("2025 | 天然气 | 80 Nm3") == 1
    assert derived.metadata["table_row_count"] == 3
    assert derived.metadata["table_col_count"] == 3
    assert derived.metadata["table_inline_in_source"] is False
    assert derived.metadata["start_page"] == 1
    assert derived.metadata["end_page"] == 2
