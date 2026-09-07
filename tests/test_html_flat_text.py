from bs4 import BeautifulSoup

from app.rag.core.parser.html.models import HtmlParseOptions
from app.rag.core.parser.html.renderer import HtmlMarkdownRenderer
from app.rag.core.parser.html.service import HtmlParseService


def _render(html: str) -> tuple[str, HtmlMarkdownRenderer]:
    soup = BeautifulSoup(html, "lxml")
    renderer = HtmlMarkdownRenderer(HtmlParseOptions())
    return renderer.render_children(soup.body or soup), renderer


def test_flat_text_html_restores_newline_delimited_blocks() -> None:
    lines = [f"第 {index} 条正文包含足够的可见文本" for index in range(1, 13)]
    body = "\n".join(lines)
    result = HtmlParseService().parse(f"<html><body><p>{body}</p></body></html>")

    assert result.markdown == "\n\n".join(lines)
    assert result.metadata["flat_text_paragraph_count"] == 1
    assert result.metadata["flat_text_block_count"] == len(lines)


def test_short_multiline_paragraph_keeps_original_paragraph_semantics() -> None:
    markdown, renderer = _render("<p>第一行\n第二行\n第三行</p>")

    assert markdown == "第一行\n第二行\n第三行"
    assert renderer.flat_text_paragraph_count == 0
    assert renderer.flat_text_block_count == 0


def test_short_html_document_is_not_rejected_by_character_count() -> None:
    body = "简短但有效的公告正文。"

    result = HtmlParseService().parse(f"<html><body><p>{body}</p></body></html>")

    assert result.markdown == body


def test_flat_text_html_accepts_br_as_the_only_line_separator_tag() -> None:
    lines = [f"第 {index} 行内容" for index in range(1, 10)]
    markdown, renderer = _render(f"<p>{'<br>'.join(lines)}</p>")

    assert markdown == "\n\n".join(lines)
    assert renderer.flat_text_paragraph_count == 1
    assert renderer.flat_text_block_count == len(lines)


def test_paragraph_with_inline_markup_is_not_treated_as_flat_text_export() -> None:
    lines = [f"第 {index} 行" for index in range(1, 10)]
    remainder = "\n".join(lines[1:])
    html = f"<p><strong>{lines[0]}</strong>\n{remainder}</p>"

    markdown, renderer = _render(html)

    assert markdown.startswith(f"**{lines[0]}**\n{lines[1]}")
    assert "\n\n" not in markdown
    assert renderer.flat_text_paragraph_count == 0


def test_standard_semantic_html_keeps_dom_structure_and_skips_flat_fallback() -> None:
    multiline_paragraph = "\n".join(
        f"正文源码第 {index} 行包含标准段落内容。" for index in range(1, 10)
    )
    html = (
        "<article><h1>年度报告</h1>"
        f"<p>{multiline_paragraph}</p>"
        "<h2>数据说明</h2><p>这里是第二个标准段落。</p>"
        "<ul><li>范围一</li><li>范围二</li></ul>"
        "<table><tr><td>指标</td><td>数值</td></tr>"
        "<tr><td>排放量</td><td>100</td></tr></table></article>"
    )

    result = HtmlParseService().parse(html)
    markdown = result.markdown

    assert markdown.startswith("# 年度报告\n\n")
    assert multiline_paragraph in markdown
    assert f"{multiline_paragraph.splitlines()[0]}\n\n" not in markdown
    assert "## 数据说明" in markdown
    assert "- 范围一\n- 范围二" in markdown
    assert "| 指标 | 数值 |" in markdown
    assert result.metadata["flat_text_paragraph_count"] == 0
    assert result.metadata["flat_text_block_count"] == 0
