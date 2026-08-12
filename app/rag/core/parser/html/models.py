from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class HtmlParseOptions:
    """HTML parser runtime options."""

    source_file_url: str | None = None
    image_prefix: str = "html-images"
    mock_minio_base_url: str = "mock-minio://tolink-rag"
    # DOCX 需要把 Mammoth 产生的 table 保留为 HTML，才能让下游
    # 保留 rowspan/colspan 并构建二维单元格元数据。普通网页解析继续使用
    # 原有 Markdown/记录式渲染策略。
    preserve_table_html: bool = False
    # 上游已将图片持久化为最终对象 URL 时，不得再改写成
    # HTML 模拟对象路径。
    preserve_image_urls: bool = False
    # Word 专用的自适应表格协议：简单表格输出 GFM，复杂表格输出
    # ``table-rag-v2`` 文字块。普通 HTML 文档保持原有策略。
    adaptive_word_tables: bool = False
    # Word 保存态分页信息；renderer 会把页码和标题路径写入表格语义上下文。
    page_number: int | None = None
    initial_heading_path: list[str] = field(default_factory=list)
    table_id_start: int = 1


@dataclass(slots=True)
class ImageRewriteResult:
    """Result of normalizing an HTML image reference."""

    markdown: str
    original_url: str
    absolute_url: str
    object_url: str | None
    warning: str | None = None


@dataclass(slots=True)
class TableRenderResult:
    """Rendered Markdown for a single HTML table."""

    markdown: str
    strategy: str
    warning: str | None = None
    image_count: int = 0
    warnings: list[str] = field(default_factory=list)
    table_ir: "TableIR | None" = None
    preview_tables: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class TableCellIR:
    """Word 表格的无损单元格中间表示。"""

    row: int
    column: int
    row_span: int
    column_span: int
    is_header: bool
    text: str
    html: str
    image_sources: list[str] = field(default_factory=list)
    links: list[list[str]] = field(default_factory=list)
    nested_tables: list["TableIR"] = field(default_factory=list)
    block_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "column": self.column,
            "row_span": self.row_span,
            "column_span": self.column_span,
            "is_header": self.is_header,
            "text": self.text,
            "html": self.html,
            "image_sources": list(self.image_sources),
            "links": [list(link) for link in self.links],
            "nested_tables": [table.to_dict() for table in self.nested_tables],
            "block_count": self.block_count,
        }


@dataclass(slots=True)
class TableIR:
    """Word 表格结构、复杂度和预览信息的统一中间表示。"""

    caption: str
    row_count: int
    column_count: int
    cells: list[TableCellIR]
    header_row_count: int = 0
    complexity_reasons: list[str] = field(default_factory=list)

    @property
    def is_complex(self) -> bool:
        return bool(self.complexity_reasons)

    @property
    def image_count(self) -> int:
        return sum(
            len(cell.image_sources)
            + sum(nested_table.image_count for nested_table in cell.nested_tables)
            for cell in self.cells
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "caption": self.caption,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "header_row_count": self.header_row_count,
            "complexity_reasons": list(self.complexity_reasons),
            "cells": [cell.to_dict() for cell in self.cells],
        }


@dataclass(slots=True)
class HtmlParseResult:
    """Structured output before adapting to BaseParser."""

    markdown: str
    metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
