from pathlib import Path

from ..base import BaseParser
from ..exceptions import ParseBaseException


class MarkdownFileParser(BaseParser):
    """UTF-8 Markdown passthrough parser used by the ingestion pipeline."""

    def parse(self, source: Path | None) -> str:
        if source is None:
            raise ValueError("Markdown 解析需要本地源文件路径")
        self.validate_source(source)

        try:
            markdown = Path(source).read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParseBaseException("Markdown 文件必须使用 UTF-8 编码") from exc

        if not markdown.strip():
            raise ParseBaseException("Markdown 文件正文不能为空")

        self.metadata.update(
            {
                "parser_backend": "markdown_passthrough",
                "pages_or_length": len(markdown.splitlines()),
                "source_encoding": "utf-8",
            }
        )
        return markdown
