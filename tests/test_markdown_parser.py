from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.documents import _validate_markdown_file_encoding
from app.rag.core.parser.exceptions import ParseBaseException
from app.rag.core.parser.factory import ParserFactory
from app.rag.core.parser.providers.markdown_parser import MarkdownFileParser


def test_markdown_parser_preserves_utf8_source_and_reports_passthrough_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "核算说明.md"
    source.write_text(
        "# 核算说明\n\n| 项目 | 数值 |\n| --- | --- |\n| 电力 | 12 |\n",
        encoding="utf-8",
    )

    parser = ParserFactory.get_parser("md")
    markdown = parser.parse(source)

    assert isinstance(parser, MarkdownFileParser)
    assert markdown.startswith("# 核算说明")
    assert "| 电力 | 12 |" in markdown
    assert parser.extract_metadata() == {
        "parser_backend": "markdown_passthrough",
        "pages_or_length": 5,
        "source_encoding": "utf-8",
    }


def test_markdown_parser_accepts_markdown_suffix_and_utf8_bom(tmp_path: Path) -> None:
    source = tmp_path / "report.markdown"
    source.write_bytes(b"\xef\xbb\xbf# Report\n")

    parser = ParserFactory.get_parser("MARKDOWN")

    assert parser.parse(source) == "# Report\n"


def test_markdown_parser_rejects_non_utf8_input(tmp_path: Path) -> None:
    source = tmp_path / "legacy.md"
    source.write_bytes("碳排放".encode("gb18030"))

    with pytest.raises(ParseBaseException, match="UTF-8"):
        ParserFactory.get_parser("md").parse(source)

    with pytest.raises(HTTPException) as exc_info:
        _validate_markdown_file_encoding(source, "md")

    assert exc_info.value.status_code == 422
    assert "UTF-8" in str(exc_info.value.detail)
