from bs4 import BeautifulSoup

from app.rag.core.markdown_parser.models import ElementType
from app.rag.core.markdown_parser.parser import MarkdownParser
from app.rag.core.parser.html.models import HtmlParseOptions
from app.rag.core.parser.html.renderer import HtmlMarkdownRenderer
from app.rag.core.splitter.element_derived_chunker import DerivedElementChunkBuilder


def _render(html: str) -> tuple[str, HtmlMarkdownRenderer]:
    soup = BeautifulSoup(html, "lxml")
    renderer = HtmlMarkdownRenderer(
        HtmlParseOptions(adaptive_word_tables=True, preserve_image_urls=True)
    )
    return renderer.render_children(soup.body or soup), renderer


def test_simple_word_table_uses_one_marked_gfm_table_element() -> None:
    markdown, renderer = _render(
        "<table><tr><td>字段</td><td>说明</td></tr>"
        "<tr><td>name</td><td>名称</td></tr></table>"
    )
    result = MarkdownParser().parse(markdown)

    assert "| 字段 | 说明 |" in markdown
    assert "<table" not in markdown
    assert renderer.markdown_table_count == 1
    assert renderer.rag_text_table_count == 0
    assert len(result.tables) == 1
    assert result.elements[0].type is ElementType.TABLE
    assert result.elements[0].metadata["table_format"] == "markdown"
    assert renderer.table_previews[0]["schema"] == "table-ir-preview-v1"


def test_complex_word_table_uses_rag_text_and_preserves_preview_ir() -> None:
    markdown, renderer = _render(
        "<table><thead>"
        '<tr><th rowspan="2">部门</th><th colspan="2">预算</th></tr>'
        "<tr><th>人力</th><th>设备</th></tr>"
        "</thead><tbody>"
        "<tr><td>研发部</td><td>100 万</td><td>30 万</td></tr>"
        "</tbody></table>"
    )
    result = MarkdownParser().parse(markdown)

    assert "<table" not in markdown
    assert 'format="rag_text" schema="table-rag-v2"' in markdown
    assert "- 预算：人力、设备" in markdown
    assert markdown.count("- 预算：人力、设备") == 1
    assert "- 行1：部门：研发部 | 人力：100 万 | 设备：30 万" in markdown
    assert renderer.rag_text_table_count == 1
    assert len(result.tables) == 1
    assert result.elements[0].metadata["complexity_reasons"] == [
        "colspan",
        "multi_header",
        "rowspan",
    ]
    preview = renderer.table_previews[0]
    assert preview["cells"][0]["row_span"] == 2
    assert preview["cells"][1]["column_span"] == 2


def test_nested_word_table_is_emitted_as_parent_and_child_table_elements() -> None:
    markdown, renderer = _render(
        "<table><tr><td>类型</td><td>内容</td></tr>"
        "<tr><td>组合</td><td><table><tr><td>子项</td></tr></table></td></tr>"
        "</table>"
    )
    result = MarkdownParser().parse(markdown)

    assert len(result.tables) == 2
    assert [preview["id"] for preview in renderer.table_previews] == [
        "table-001",
        "table-001-001",
    ]
    nested = next(element for element in result.elements if element.metadata.get("nested_table"))
    assert nested.metadata["parent_table_id"] == "table-001"


def test_linkparse_markers_are_not_kept_in_table_retrieval_text() -> None:
    markdown, _ = _render(
        "<table><tr><td>字段</td><td>说明</td></tr>"
        "<tr><td>name</td><td>名称</td></tr></table>"
    )
    table = MarkdownParser().parse(markdown).tables[0]

    raw_table = DerivedElementChunkBuilder._extract_raw_table(table.content)

    assert "LINKPARSE_TABLE" not in raw_table
    assert "<table" not in raw_table
    assert raw_table.startswith("| 字段 | 说明 |")
