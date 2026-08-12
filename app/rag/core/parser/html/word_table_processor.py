import re
from typing import Literal

from bs4 import BeautifulSoup, Tag

from .models import TableCellIR, TableIR, TableRenderResult

TableStrategy = Literal["markdown_table", "rag_text_table", "html_fallback"]
COMPLEX_BLOCK_TAGS = {"p", "pre", "blockquote", "ul", "ol", "table"}
MAX_TABLE_SPAN = 256


class WordTableProcessor:
    """Build a loss-aware Word table IR and emit GFM or ``table-rag-v2``."""

    def render(
        self,
        table: Tag,
        *,
        table_id: str,
        page_number: int | None = None,
        heading_path: list[str] | None = None,
    ) -> TableRenderResult:
        try:
            table_ir = self._build_ir(table)
            if not table_ir.cells:
                raise ValueError("table contains no readable cells")
            if table_ir.is_complex:
                return TableRenderResult(
                    markdown=self._render_rag_text_table(
                        table_ir,
                        table_id,
                        page_number=page_number,
                        heading_path=heading_path or [],
                    ),
                    strategy="rag_text_table",
                    table_ir=table_ir,
                    image_count=table_ir.image_count,
                    preview_tables=self._build_preview_tables(table_ir, table_id),
                )
            return TableRenderResult(
                markdown=self._render_markdown_table_block(
                    table_ir,
                    table_id,
                    page_number=page_number,
                    heading_path=heading_path or [],
                ),
                strategy="markdown_table",
                table_ir=table_ir,
                preview_tables=self._build_preview_tables(table_ir, table_id),
            )
        except Exception as exc:
            return TableRenderResult(
                markdown=self._render_html_fallback(table, table_id),
                strategy="html_fallback",
                warning=str(exc),
                image_count=len(table.find_all("img")),
            )

    def _build_ir(self, table: Tag) -> TableIR:
        rows = self._direct_rows(table)
        cells: list[TableCellIR] = []
        occupied: set[tuple[int, int]] = set()
        max_column = 0
        header_row_indexes: set[int] = set()

        for row_index, row in enumerate(rows):
            row_cells = row.find_all(["th", "td"], recursive=False)
            if row_cells and all(cell.name == "th" for cell in row_cells):
                header_row_indexes.add(row_index)
            column_index = 0
            for cell in row_cells:
                while (row_index, column_index) in occupied:
                    column_index += 1
                row_span = self._parse_span(cell.get("rowspan"))
                column_span = self._parse_span(cell.get("colspan"))
                image_sources = [
                    str(image.get("src", ""))
                    for image in cell.find_all("img")
                    if image.find_parent("table") is table and str(image.get("src", ""))
                ]
                links = [
                    [self._clean_text(link.get_text(" ", strip=True)), str(link.get("href", ""))]
                    for link in cell.find_all("a")
                    if link.find_parent("table") is table and str(link.get("href", ""))
                ]
                nested_tables = [
                    nested
                    for nested in cell.find_all("table")
                    if nested.find_parent("table") is table
                ]
                block_count = len(
                    [
                        child
                        for child in cell.children
                        if isinstance(child, Tag) and child.name in COMPLEX_BLOCK_TAGS
                    ]
                )
                cells.append(
                    TableCellIR(
                        row=row_index,
                        column=column_index,
                        row_span=row_span,
                        column_span=column_span,
                        is_header=cell.name == "th",
                        text=self._cell_text(cell),
                        html="".join(str(child) for child in cell.children).strip(),
                        image_sources=image_sources,
                        links=links,
                        nested_tables=[self._build_ir(nested) for nested in nested_tables],
                        block_count=block_count,
                    )
                )
                for row_offset in range(row_span):
                    for column_offset in range(column_span):
                        occupied.add((row_index + row_offset, column_index + column_offset))
                column_index += column_span
                max_column = max(max_column, column_index)

        row_count = max((row + 1 for row, _ in occupied), default=len(rows))
        header_row_count = 0
        while header_row_count in header_row_indexes:
            header_row_count += 1
        if not header_row_count and rows:
            header_row_count = 1

        reasons: set[str] = set()
        if header_row_count > 1:
            reasons.add("multi_header")
        for cell in cells:
            if cell.row_span > 1:
                reasons.add("rowspan")
            if cell.column_span > 1:
                reasons.add("colspan")
            if cell.nested_tables:
                reasons.add("nested_table")
            if cell.image_sources:
                reasons.add("image_cell")
            if cell.links:
                reasons.add("link_cell")
            if cell.block_count > 1:
                reasons.add("multi_block_cell")

        return TableIR(
            caption=self._table_caption(table),
            row_count=row_count,
            column_count=max_column,
            cells=cells,
            header_row_count=header_row_count,
            complexity_reasons=sorted(reasons),
        )

    def _render_markdown_table_block(
        self,
        table_ir: TableIR,
        table_id: str,
        *,
        page_number: int | None,
        heading_path: list[str],
    ) -> str:
        start_marker = (
            f'<!-- LINKPARSE_TABLE_START id="{table_id}" format="markdown" '
            'schema="gfm-table-v1" -->'
        )
        end_marker = f'<!-- LINKPARSE_TABLE_END id="{table_id}" -->'
        context = self._render_context(
            table_ir,
            page_number=page_number,
            heading_path=heading_path,
        )
        table_markdown = self._render_markdown_table(table_ir)
        return "\n".join([start_marker, *context, table_markdown, end_marker])

    def _render_markdown_table(self, table_ir: TableIR) -> str:
        matrix = [["" for _ in range(table_ir.column_count)] for _ in range(table_ir.row_count)]
        for cell in table_ir.cells:
            matrix[cell.row][cell.column] = cell.text
        if not matrix:
            raise ValueError("table matrix is empty")

        headers = [
            value or f"列{column_index + 1}"
            for column_index, value in enumerate(matrix[0])
        ]
        lines = [
            "| " + " | ".join(self._escape_table_cell(cell) for cell in headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
        ]
        lines.extend(
            "| " + " | ".join(self._escape_table_cell(cell) for cell in row) + " |"
            for row in matrix[1:]
        )
        return "\n".join(lines)

    def _render_rag_text_table(
        self,
        table_ir: TableIR,
        table_id: str,
        *,
        page_number: int | None,
        heading_path: list[str],
        parent: tuple[str, int, int] | None = None,
    ) -> str:
        reasons = ",".join(table_ir.complexity_reasons)
        start_marker = (
            f'<!-- LINKPARSE_TABLE_START id="{table_id}" format="rag_text" '
            f'schema="table-rag-v2" reasons="{reasons}"'
        )
        if parent is not None:
            parent_id, parent_row, parent_column = parent
            start_marker += (
                f' parent_id="{parent_id}" parent_row="{parent_row + 1}" '
                f'parent_column="{parent_column + 1}"'
            )
        start_marker += " -->"
        end_marker = f'<!-- LINKPARSE_TABLE_END id="{table_id}" -->'
        header_paths = self._header_paths(table_ir)
        field_names = self._field_names(header_paths)
        child_refs: dict[tuple[int, int], str] = {}
        child_tables: list[tuple[str, TableIR, int, int]] = []
        child_number = 0
        for cell_index, cell in enumerate(table_ir.cells):
            for nested_index, nested_table in enumerate(cell.nested_tables):
                child_number += 1
                child_id = f"{table_id}-{child_number:03d}"
                child_refs[(cell_index, nested_index)] = child_id
                child_tables.append((child_id, nested_table, cell.row, cell.column))

        lines = [start_marker]
        lines.extend(
            self._render_context(
                table_ir,
                page_number=page_number,
                heading_path=heading_path,
            )
        )
        header_summary = self._render_header_summary(header_paths)
        if header_summary:
            lines.append("表头：")
            lines.extend(f"- {item}" for item in header_summary)

        header_rows = min(table_ir.header_row_count, table_ir.row_count)
        body_line_count = 0
        for row_index in range(header_rows, table_ir.row_count):
            fields: list[str] = []
            for cell_index, cell in enumerate(table_ir.cells):
                if cell.row < header_rows:
                    continue
                if not (cell.row <= row_index < cell.row + cell.row_span):
                    continue
                covered_headers = field_names[
                    cell.column : min(table_ir.column_count, cell.column + cell.column_span)
                ]
                field_name = "、".join(dict.fromkeys(covered_headers)) or f"列{cell.column + 1}"
                child_ids = [
                    child_refs[(cell_index, nested_index)]
                    for nested_index in range(len(cell.nested_tables))
                ]
                value = self._render_cell_value(cell, child_ids)
                if value:
                    fields.append(f"{field_name}：{value}")
            if fields:
                body_line_count += 1
                if body_line_count == 1:
                    lines.append("数据：")
                escaped_fields = [self._escape_rag_field(field) for field in fields]
                lines.append(f"- 行{body_line_count}：" + " | ".join(escaped_fields))

        if body_line_count == 0:
            header_content: list[str] = []
            for cell_index, cell in enumerate(table_ir.cells):
                child_ids = [
                    child_refs[(cell_index, nested_index)]
                    for nested_index in range(len(cell.nested_tables))
                ]
                value = self._render_cell_value(cell, child_ids)
                if value and value not in header_content:
                    header_content.append(value)
            if header_content:
                lines.append("内容：" + "；".join(header_content))

        lines.append(end_marker)
        blocks = ["\n".join(lines)]
        for child_id, nested_table, parent_row, parent_column in child_tables:
            blocks.append(
                self._render_rag_text_table(
                    nested_table,
                    child_id,
                    page_number=page_number,
                    heading_path=heading_path,
                    parent=(table_id, parent_row, parent_column),
                )
            )
        return "\n\n".join(blocks)

    def _header_paths(self, table_ir: TableIR) -> list[list[str]]:
        header_rows = min(table_ir.header_row_count, table_ir.row_count)
        header_matrix = [["" for _ in range(table_ir.column_count)] for _ in range(header_rows)]
        for cell in table_ir.cells:
            if cell.row >= header_rows:
                continue
            row_end = min(header_rows, cell.row + cell.row_span)
            column_end = min(table_ir.column_count, cell.column + cell.column_span)
            for row_index in range(cell.row, row_end):
                for column_index in range(cell.column, column_end):
                    header_matrix[row_index][column_index] = cell.text

        title_row_count = self._title_header_row_count(table_ir)
        headers: list[list[str]] = []
        for column_index in range(table_ir.column_count):
            parts: list[str] = []
            for row_index in range(title_row_count, header_rows):
                part = header_matrix[row_index][column_index]
                if part and (not parts or parts[-1] != part):
                    parts.append(part)
            headers.append(parts or [f"列{column_index + 1}"])
        return headers

    @staticmethod
    def _title_header_row_count(table_ir: TableIR) -> int:
        """Treat a leading full-width merged header as a title, not a field prefix."""
        if table_ir.header_row_count <= 1:
            return 0
        for cell in table_ir.cells:
            if (
                cell.row == 0
                and cell.column == 0
                and cell.column_span >= table_ir.column_count
                and cell.is_header
                and cell.text
            ):
                return 1
        return 0

    def _table_title(self, table_ir: TableIR) -> str:
        if table_ir.caption:
            return table_ir.caption
        if not self._title_header_row_count(table_ir):
            return ""
        return next(
            (
                cell.text
                for cell in table_ir.cells
                if cell.row == 0 and cell.column == 0 and cell.is_header and cell.text
            ),
            "",
        )

    @staticmethod
    def _field_names(header_paths: list[list[str]]) -> list[str]:
        """Use the shortest unique suffix so row data stays compact but unambiguous."""
        names: list[str] = []
        for index, path in enumerate(header_paths):
            selected = path[-1]
            for width in range(1, len(path) + 1):
                candidate = "/".join(path[-width:])
                if sum(
                    1
                    for other in header_paths
                    if "/".join(other[-width:]) == candidate
                ) == 1:
                    selected = candidate
                    break
            if selected in names:
                selected = f"{selected}(列{index + 1})"
            names.append(selected)
        return names

    @staticmethod
    def _render_header_summary(header_paths: list[list[str]]) -> list[str]:
        groups: dict[str, list[str]] = {}
        for path in header_paths:
            root = path[0]
            child = "/".join(path[1:])
            if root not in groups:
                groups[root] = []
            if child and child not in groups[root]:
                groups[root].append(child)
        return [
            f"{root}：{'、'.join(children)}" if children else root
            for root, children in groups.items()
        ]

    @staticmethod
    def _escape_rag_field(field: str) -> str:
        return field.replace(" | ", " \\| ")

    def _render_cell_value(self, cell: TableCellIR, child_ids: list[str]) -> str:
        parts: list[str] = []
        if cell.text:
            parts.append(cell.text)
        for image_index, source in enumerate(cell.image_sources, start=1):
            parts.append(f"![表格图片{image_index}]({source})")
        for label, href in cell.links:
            link_label = label or "链接"
            parts.append(f"链接：[{link_label}]({href})")
        parts.extend(f"嵌套表格：{child_id}" for child_id in child_ids)
        return "；".join(parts)

    def _build_preview_tables(self, table_ir: TableIR, table_id: str) -> list[dict]:
        child_refs: dict[tuple[int, int], str] = {}
        child_tables: list[tuple[str, TableIR]] = []
        child_number = 0
        for cell_index, cell in enumerate(table_ir.cells):
            for nested_index, nested_table in enumerate(cell.nested_tables):
                child_number += 1
                child_id = f"{table_id}-{child_number:03d}"
                child_refs[(cell_index, nested_index)] = child_id
                child_tables.append((child_id, nested_table))

        cells: list[dict] = []
        for cell_index, cell in enumerate(table_ir.cells):
            child_ids = [
                child_refs[(cell_index, nested_index)]
                for nested_index in range(len(cell.nested_tables))
            ]
            cells.append(
                {
                    "row": cell.row,
                    "column": cell.column,
                    "row_span": cell.row_span,
                    "column_span": cell.column_span,
                    "is_header": cell.is_header,
                    "markdown": self._preview_cell_markdown(cell, child_ids),
                }
            )
        preview = {
            "id": table_id,
            "schema": "table-ir-preview-v1",
            "row_count": table_ir.row_count,
            "column_count": table_ir.column_count,
            "header_row_count": table_ir.header_row_count,
            "caption": table_ir.caption,
            "cells": cells,
        }
        previews = [preview]
        for child_id, nested_table in child_tables:
            previews.extend(self._build_preview_tables(nested_table, child_id))
        return previews

    @staticmethod
    def _preview_cell_markdown(cell: TableCellIR, child_ids: list[str]) -> str:
        value = cell.text
        for label, href in cell.links:
            link_label = label or "链接"
            link = f"[{link_label}]({href})"
            if label and label in value:
                value = value.replace(label, link, 1)
            else:
                value = "；".join(part for part in (value, link) if part)
        extras = [
            f"![表格图片{index}]({source})"
            for index, source in enumerate(cell.image_sources, start=1)
        ]
        extras.extend(f"嵌套表格：{child_id}" for child_id in child_ids)
        return "；".join(part for part in [value, *extras] if part)

    def _render_context(
        self,
        table_ir: TableIR,
        *,
        page_number: int | None,
        heading_path: list[str],
    ) -> list[str]:
        context: list[str] = []
        table_title = self._table_title(table_ir)
        if table_title:
            context.append(f"表格：{table_title}")
        if heading_path:
            context.append("章节：" + " / ".join(heading_path))
        if page_number is not None:
            context.append(f"页码：第 {page_number} 页")
        return context

    @staticmethod
    def _render_html_fallback(table: Tag, table_id: str) -> str:
        start_marker = (
            f'<!-- LINKPARSE_TABLE_START id="{table_id}" format="html_fallback" -->'
        )
        end_marker = f'<!-- LINKPARSE_TABLE_END id="{table_id}" -->'
        return f"{start_marker}\n{str(table)}\n{end_marker}"

    @staticmethod
    def _direct_rows(table: Tag) -> list[Tag]:
        rows: list[Tag] = []
        for child in table.children:
            if not isinstance(child, Tag):
                continue
            if child.name == "tr":
                rows.append(child)
            elif child.name in {"thead", "tbody", "tfoot"}:
                rows.extend(child.find_all("tr", recursive=False))
        return rows

    def _cell_text(self, cell: Tag) -> str:
        parsed = BeautifulSoup(str(cell), "lxml")
        copied_cell = parsed.find(["td", "th"])
        if copied_cell is None:
            return self._clean_text(cell.get_text(" ", strip=True))
        for nested_table in copied_cell.find_all("table"):
            nested_table.decompose()
        return self._clean_text(copied_cell.get_text(" ", strip=True))

    def _table_caption(self, table: Tag) -> str:
        caption = table.find("caption", recursive=False)
        return self._clean_text(caption.get_text(" ", strip=True)) if caption else ""

    @staticmethod
    def _parse_span(raw_value: object) -> int:
        try:
            value = max(1, int(str(raw_value or "1")))
        except ValueError:
            return 1
        if value > MAX_TABLE_SPAN:
            raise ValueError(f"table cell span exceeds {MAX_TABLE_SPAN}")
        return value

    def _escape_table_cell(self, text: str) -> str:
        return self._clean_text(text).replace("|", "\\|").replace("\n", " ")

    @staticmethod
    def _clean_text(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()
