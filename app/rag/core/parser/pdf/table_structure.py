"""Structured PDF table extraction and conservative cross-page continuation merging.

The module consumes OpenDataLoader Markdown.  PDF page provenance comes exclusively
from ``ODL_PAGE`` markers supplied by the backend; image/table filenames are never
interpreted as page numbers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from bs4 import BeautifulSoup, Tag


class TableSourceFormat(StrEnum):
    MARKDOWN = "MARKDOWN"
    HTML = "HTML"
    MIXED = "MIXED"


class OdlTableMethod(StrEnum):
    DEFAULT = "default"
    CLUSTER = "cluster"


@dataclass(frozen=True, slots=True)
class StructuredTableCell:
    cell_id: str
    row_index: int
    column_index: int
    text: str
    row_span: int = 1
    column_span: int = 1
    is_header: bool = False
    source_page: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "cell_id": self.cell_id,
            "row_index": self.row_index,
            "column_index": self.column_index,
            "text": self.text,
            "row_span": self.row_span,
            "column_span": self.column_span,
            "is_header": self.is_header,
            "source_page": self.source_page,
        }


@dataclass(frozen=True, slots=True)
class StructuredPdfTable:
    table_id: str
    title: str | None
    source_format: TableSourceFormat
    source_pages: tuple[int, ...]
    row_count: int
    column_count: int
    header_row_count: int
    cells: tuple[StructuredTableCell, ...]
    cell_reference_matrix: tuple[tuple[str | None, ...], ...]
    header_hierarchy: tuple[tuple[str, ...], ...]
    part_table_ids: tuple[str, ...]
    source_offset_ranges: tuple[tuple[int, int], ...] = ()
    source_line_ranges: tuple[tuple[int, int], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def source_page_start(self) -> int | None:
        return min(self.source_pages) if self.source_pages else None

    @property
    def source_page_end(self) -> int | None:
        return max(self.source_pages) if self.source_pages else None

    @property
    def text_matrix(self) -> tuple[tuple[str, ...], ...]:
        cells_by_id = {cell.cell_id: cell for cell in self.cells}
        return tuple(
            tuple(cells_by_id[cell_id].text if cell_id in cells_by_id else "" for cell_id in row)
            for row in self.cell_reference_matrix
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "table_id": self.table_id,
            "title": self.title,
            "source_format": self.source_format.value,
            "source_page_range": {
                "start": self.source_page_start,
                "end": self.source_page_end,
            },
            "source_pages": list(self.source_pages),
            "page_provenance_complete": bool(self.source_pages),
            "row_count": self.row_count,
            "column_count": self.column_count,
            "header_row_count": self.header_row_count,
            "header_hierarchy": [list(levels) for levels in self.header_hierarchy],
            "cells": [cell.to_dict() for cell in self.cells],
            "cell_reference_matrix": [list(row) for row in self.cell_reference_matrix],
            "text_matrix": [list(row) for row in self.text_matrix],
            "part_table_ids": list(self.part_table_ids),
            "source_offset_ranges": [list(item) for item in self.source_offset_ranges],
            "source_line_ranges": [list(item) for item in self.source_line_ranges],
            "warnings": list(self.warnings),
        }

    def to_chunk_structure_metadata(self) -> dict[str, object]:
        """Payload intended for ``document_chunk.structure_metadata``."""

        return {
            "chunk_role": "table",
            "source_page_start": self.source_page_start,
            "source_page_end": self.source_page_end,
            "table_structure": self.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class TableContinuationDecision:
    left_table_id: str
    right_table_id: str
    merged: bool
    score: int
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "left_table_id": self.left_table_id,
            "right_table_id": self.right_table_id,
            "merged": self.merged,
            "score": self.score,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class PdfTableStructureReport:
    tables: tuple[StructuredPdfTable, ...]
    raw_table_count: int
    merged_table_count: int
    warnings: tuple[str, ...]
    continuation_decisions: tuple[TableContinuationDecision, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "raw_table_count": self.raw_table_count,
            "structured_table_count": len(self.tables),
            "merged_table_count": self.merged_table_count,
            "warnings": list(self.warnings),
            "continuation_decisions": [
                decision.to_dict() for decision in self.continuation_decisions
            ],
            "tables": [table.to_dict() for table in self.tables],
        }


@dataclass(frozen=True, slots=True)
class OdlTableStrategyMetrics:
    method: OdlTableMethod
    markdown_with_html: bool
    page_marker_count: int
    raw_table_count: int
    structured_table_count: int
    merged_table_count: int
    cell_count: int
    non_empty_cell_count: int
    row_span_cell_count: int
    column_span_cell_count: int
    warning_count: int
    page_provenance_complete: bool

    @classmethod
    def from_report(
        cls,
        method: OdlTableMethod,
        markdown_with_html: bool,
        report: PdfTableStructureReport,
        *,
        page_marker_count: int,
    ) -> OdlTableStrategyMetrics:
        cells = [cell for table in report.tables for cell in table.cells]
        return cls(
            method=method,
            markdown_with_html=bool(markdown_with_html),
            page_marker_count=page_marker_count,
            raw_table_count=report.raw_table_count,
            structured_table_count=len(report.tables),
            merged_table_count=report.merged_table_count,
            cell_count=len(cells),
            non_empty_cell_count=sum(bool(cell.text.strip()) for cell in cells),
            row_span_cell_count=sum(cell.row_span > 1 for cell in cells),
            column_span_cell_count=sum(cell.column_span > 1 for cell in cells),
            warning_count=len(report.warnings)
            + sum(len(table.warnings) for table in report.tables),
            page_provenance_complete=all(bool(table.source_pages) for table in report.tables),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method.value,
            "markdown_with_html": self.markdown_with_html,
            "page_marker_count": self.page_marker_count,
            "raw_table_count": self.raw_table_count,
            "structured_table_count": self.structured_table_count,
            "merged_table_count": self.merged_table_count,
            "cell_count": self.cell_count,
            "non_empty_cell_count": self.non_empty_cell_count,
            "row_span_cell_count": self.row_span_cell_count,
            "column_span_cell_count": self.column_span_cell_count,
            "warning_count": self.warning_count,
            "page_provenance_complete": self.page_provenance_complete,
        }


@dataclass(frozen=True, slots=True)
class OdlTableAbReport:
    default: OdlTableStrategyMetrics
    cluster: OdlTableStrategyMetrics
    recommendation: OdlTableMethod
    reasons: tuple[str, ...]

    @classmethod
    def compare(
        cls,
        default: OdlTableStrategyMetrics,
        cluster: OdlTableStrategyMetrics,
    ) -> OdlTableAbReport:
        if default.method is not OdlTableMethod.DEFAULT:
            raise ValueError("default metrics must use the default table method")
        if cluster.method is not OdlTableMethod.CLUSTER:
            raise ValueError("cluster metrics must use the cluster table method")

        reasons: list[str] = []
        cluster_gain = cluster.non_empty_cell_count - default.non_empty_cell_count
        warning_delta = cluster.warning_count - default.warning_count
        if cluster_gain > 0:
            reasons.append(f"CLUSTER_NON_EMPTY_CELL_GAIN:{cluster_gain}")
        if warning_delta > 0:
            reasons.append(f"CLUSTER_WARNING_INCREASE:{warning_delta}")
        if cluster.structured_table_count > default.structured_table_count:
            reasons.append(
                "CLUSTER_STRUCTURED_TABLE_GAIN:"
                f"{cluster.structured_table_count - default.structured_table_count}"
            )

        # Prefer the conservative default unless cluster produces measurably more
        # structured content without adding warnings or losing page provenance.
        use_cluster = (
            cluster_gain > 0
            and warning_delta <= 0
            and cluster.page_provenance_complete
        )
        recommendation = OdlTableMethod.CLUSTER if use_cluster else OdlTableMethod.DEFAULT
        if not reasons:
            reasons.append("NO_STRUCTURAL_DIFFERENCE")
        if not use_cluster:
            reasons.append("CONSERVATIVE_DEFAULT_RETAINED")
        return cls(
            default=default,
            cluster=cluster,
            recommendation=recommendation,
            reasons=tuple(reasons),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "default": self.default.to_dict(),
            "cluster": self.cluster.to_dict(),
            "recommendation": self.recommendation.value,
            "reasons": list(self.reasons),
        }


class OdlTableAbEvaluator:
    """Build comparable metrics from two real OpenDataLoader Markdown outputs."""

    _PAGE_MARKER_RE = re.compile(r"<!--\s*ODL_PAGE:(\d+)\s*-->")

    def __init__(self, extractor: PdfTableStructureExtractor | None = None) -> None:
        # The class is defined before the extractor for a compact public API; runtime
        # lookup happens only when an instance is created, after module initialization.
        self._extractor = extractor or PdfTableStructureExtractor()

    def evaluate(
        self,
        *,
        default_markdown: str,
        cluster_markdown: str,
        default_markdown_with_html: bool = False,
        cluster_markdown_with_html: bool = False,
    ) -> OdlTableAbReport:
        default_report = self._extractor.extract(default_markdown)
        cluster_report = self._extractor.extract(cluster_markdown)
        default_metrics = OdlTableStrategyMetrics.from_report(
            OdlTableMethod.DEFAULT,
            default_markdown_with_html,
            default_report,
            page_marker_count=len(self._PAGE_MARKER_RE.findall(default_markdown)),
        )
        cluster_metrics = OdlTableStrategyMetrics.from_report(
            OdlTableMethod.CLUSTER,
            cluster_markdown_with_html,
            cluster_report,
            page_marker_count=len(self._PAGE_MARKER_RE.findall(cluster_markdown)),
        )
        return OdlTableAbReport.compare(default_metrics, cluster_metrics)


class PdfTableStructureExtractor:
    """Parse Markdown and HTML tables while retaining ODL page provenance."""

    _PAGE_MARKER_RE = re.compile(
        r"^[\t ]*<!--[\t ]*ODL_PAGE:(\d+)[\t ]*-->[\t ]*\r?$",
        flags=re.MULTILINE,
    )
    _HTML_TABLE_RE = re.compile(r"<table\b.*?</table\s*>", re.IGNORECASE | re.DOTALL)
    _HTML_TABLE_TAG_RE = re.compile(r"</?table\b[^>]*>", re.IGNORECASE)
    _MARKDOWN_DELIMITER_RE = re.compile(
        r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
    )

    def extract(
        self,
        markdown: str,
        *,
        merge_continuations: bool = True,
    ) -> PdfTableStructureReport:
        warnings: list[str] = []
        sections = self._page_sections(markdown or "", warnings)
        tables: list[StructuredPdfTable] = []
        next_index = 1
        for page_number, section, base_offset, base_line in sections:
            page_tables, next_index = self._extract_page_tables(
                section,
                page_number=page_number,
                base_offset=base_offset,
                base_line=base_line,
                next_index=next_index,
            )
            tables.extend(page_tables)

        raw_count = len(tables)
        decisions: tuple[TableContinuationDecision, ...] = ()
        merged_count = 0
        if merge_continuations and tables:
            tables, decisions = TableContinuationMerger().merge(tables)
            merged_count = raw_count - len(tables)
        return PdfTableStructureReport(
            tables=tuple(tables),
            raw_table_count=raw_count,
            merged_table_count=merged_count,
            warnings=tuple(dict.fromkeys(warnings)),
            continuation_decisions=decisions,
        )

    def _page_sections(
        self,
        markdown: str,
        warnings: list[str],
    ) -> list[tuple[int | None, str, int, int]]:
        matches = list(self._PAGE_MARKER_RE.finditer(markdown))
        if not matches:
            if markdown.strip():
                warnings.append("MISSING_ODL_PAGE_MARKERS")
            return [(None, markdown, 0, 0)]
        if markdown[: matches[0].start()].strip():
            warnings.append("CONTENT_BEFORE_FIRST_ODL_PAGE_MARKER")
        sections: list[tuple[int | None, str, int, int]] = []
        seen: set[int] = set()
        for index, match in enumerate(matches):
            page_number = int(match.group(1))
            if page_number in seen:
                warnings.append(f"DUPLICATE_ODL_PAGE_MARKER:page={page_number}")
            seen.add(page_number)
            end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
            section_start = match.end()
            sections.append(
                (
                    page_number,
                    markdown[section_start:end],
                    section_start,
                    markdown.count("\n", 0, section_start),
                )
            )
        return sections

    def _extract_page_tables(
        self,
        section: str,
        *,
        page_number: int | None,
        base_offset: int,
        base_line: int,
        next_index: int,
    ) -> tuple[list[StructuredPdfTable], int]:
        # Collect both syntaxes first and assign table IDs only after sorting by
        # their original character offset.  Appending all HTML tables before all
        # Markdown tables breaks the ordinal contract used by the Markdown parser
        # whenever both formats occur on one page.
        candidates: list[tuple[int, int, str, object, str | None]] = []
        masked = list(section)
        for start, end in self._iter_top_level_html_tables(section):
            title_hint = self._preceding_title(section[:start])
            soup = BeautifulSoup(section[start:end], "html.parser")
            html_table = soup.find("table")
            if isinstance(html_table, Tag):
                candidates.append(
                    (
                        start,
                        end,
                        "html",
                        html_table,
                        title_hint,
                    )
                )
            for offset in range(start, end):
                if masked[offset] != "\n":
                    masked[offset] = " "

        lines = "".join(masked).splitlines()
        original_lines = section.splitlines()
        line_starts: list[int] = []
        cursor = 0
        for raw_line in section.splitlines(keepends=True):
            line_starts.append(cursor)
            cursor += len(raw_line)
        if len(line_starts) < len(original_lines):
            line_starts.append(cursor)
        line_index = 0
        while line_index + 1 < len(lines):
            if "|" not in lines[line_index] or not self._MARKDOWN_DELIMITER_RE.fullmatch(
                lines[line_index + 1]
            ):
                line_index += 1
                continue
            end = line_index + 2
            while end < len(lines) and lines[end].strip() and "|" in lines[end]:
                end += 1
            start_offset = line_starts[line_index]
            end_offset = (
                line_starts[end]
                if end < len(line_starts)
                else len(section)
            )
            title_hint = self._preceding_title(section[:start_offset])
            candidates.append(
                (
                    start_offset,
                    end_offset,
                    "markdown",
                    original_lines[line_index:end],
                    title_hint,
                )
            )
            line_index = end

        tables: list[StructuredPdfTable] = []
        for start_offset, end_offset, source_format, payload, title_hint in sorted(
            candidates,
            key=lambda item: (item[0], item[1]),
        ):
            table_id = f"table-{next_index:04d}"
            absolute_offsets = ((base_offset + start_offset, base_offset + end_offset),)
            absolute_lines = (
                (
                    base_line + section.count("\n", 0, start_offset),
                    base_line
                    + section.count("\n", 0, max(start_offset, end_offset - 1)),
                ),
            )
            if source_format == "html":
                tables.append(
                    self._parse_html_table(
                        payload,
                        table_id=table_id,
                        page_number=page_number,
                        title_hint=title_hint,
                        source_offset_ranges=absolute_offsets,
                        source_line_ranges=absolute_lines,
                    )
                )
            else:
                tables.append(
                    self._parse_markdown_table(
                        payload,
                        table_id=table_id,
                        page_number=page_number,
                        title_hint=title_hint,
                        source_offset_ranges=absolute_offsets,
                        source_line_ranges=absolute_lines,
                    )
                )
            next_index += 1
        return tables, next_index

    @classmethod
    def _iter_top_level_html_tables(cls, text: str) -> Iterable[tuple[int, int]]:
        """Yield balanced outer ``table`` ranges, including nested tables in full."""

        depth = 0
        start: int | None = None
        for match in cls._HTML_TABLE_TAG_RE.finditer(text or ""):
            closing = match.group(0).lstrip().startswith("</")
            if not closing:
                if depth == 0:
                    start = match.start()
                depth += 1
                continue
            if depth <= 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                yield start, match.end()
                start = None

    def _parse_html_table(
        self,
        table: Tag,
        *,
        table_id: str,
        page_number: int | None,
        title_hint: str | None,
        source_offset_ranges: tuple[tuple[int, int], ...],
        source_line_ranges: tuple[tuple[int, int], ...],
    ) -> StructuredPdfTable:
        rows = self._direct_rows(table)
        cells: list[StructuredTableCell] = []
        occupied: dict[tuple[int, int], str] = {}
        grid: list[list[str | None]] = []
        header_rows: set[int] = set()
        warnings: list[str] = []

        for row_index, row in enumerate(rows):
            self._ensure_grid_row(grid, row_index, 0)
            column_index = 0
            direct_cells = row.find_all(["th", "td"], recursive=False)
            if direct_cells and all(cell.name == "th" for cell in direct_cells):
                header_rows.add(row_index)
            for cell in direct_cells:
                while occupied.get((row_index, column_index)) is not None:
                    self._set_grid_value(
                        grid,
                        row_index,
                        column_index,
                        occupied[(row_index, column_index)],
                    )
                    column_index += 1
                row_span = self._span(cell.get("rowspan"), warnings, "rowspan")
                column_span = self._span(cell.get("colspan"), warnings, "colspan")
                cell_id = f"{table_id}-cell-{len(cells) + 1:04d}"
                is_header = cell.name == "th" or cell.find_parent("thead") is not None
                if is_header:
                    header_rows.add(row_index)
                structured = StructuredTableCell(
                    cell_id=cell_id,
                    row_index=row_index,
                    column_index=column_index,
                    text=self._clean_cell_text(cell),
                    row_span=row_span,
                    column_span=column_span,
                    is_header=is_header,
                    source_page=page_number,
                )
                cells.append(structured)
                for row_offset in range(row_span):
                    for column_offset in range(column_span):
                        target = (row_index + row_offset, column_index + column_offset)
                        if target in occupied:
                            warnings.append(
                                f"OVERLAPPING_HTML_CELL:row={target[0]},column={target[1]}"
                            )
                        occupied[target] = cell_id
                        self._set_grid_value(grid, target[0], target[1], cell_id)
                column_index += column_span

        caption = table.find("caption")
        caption_text = self._clean_text(caption.get_text(" ", strip=True)) if caption else ""
        header_row_count = self._leading_header_row_count(header_rows)
        normalized_grid = self._normalize_grid(grid)
        return self._build_table(
            table_id=table_id,
            title=caption_text or title_hint,
            source_format=TableSourceFormat.HTML,
            source_pages=(page_number,) if page_number is not None else (),
            cells=tuple(cells),
            grid=normalized_grid,
            header_row_count=header_row_count,
            source_offset_ranges=source_offset_ranges,
            source_line_ranges=source_line_ranges,
            warnings=tuple(warnings),
        )

    def _parse_markdown_table(
        self,
        lines: Sequence[str],
        *,
        table_id: str,
        page_number: int | None,
        title_hint: str | None,
        source_offset_ranges: tuple[tuple[int, int], ...],
        source_line_ranges: tuple[tuple[int, int], ...],
    ) -> StructuredPdfTable:
        data_lines = [lines[0], *lines[2:]]
        rows = [self._split_markdown_row(line) for line in data_lines]
        width = max((len(row) for row in rows), default=0)
        cells: list[StructuredTableCell] = []
        grid: list[list[str | None]] = []
        for row_index, row in enumerate(rows):
            output_row: list[str | None] = []
            for column_index in range(width):
                cell_id = f"{table_id}-cell-{len(cells) + 1:04d}"
                cells.append(
                    StructuredTableCell(
                        cell_id=cell_id,
                        row_index=row_index,
                        column_index=column_index,
                        text=row[column_index] if column_index < len(row) else "",
                        is_header=row_index == 0,
                        source_page=page_number,
                    )
                )
                output_row.append(cell_id)
            grid.append(output_row)
        return self._build_table(
            table_id=table_id,
            title=title_hint,
            source_format=TableSourceFormat.MARKDOWN,
            source_pages=(page_number,) if page_number is not None else (),
            cells=tuple(cells),
            grid=self._normalize_grid(grid),
            header_row_count=1 if rows else 0,
            source_offset_ranges=source_offset_ranges,
            source_line_ranges=source_line_ranges,
            warnings=(),
        )

    def _build_table(
        self,
        *,
        table_id: str,
        title: str | None,
        source_format: TableSourceFormat,
        source_pages: tuple[int, ...],
        cells: tuple[StructuredTableCell, ...],
        grid: tuple[tuple[str | None, ...], ...],
        header_row_count: int,
        source_offset_ranges: tuple[tuple[int, int], ...],
        source_line_ranges: tuple[tuple[int, int], ...],
        warnings: tuple[str, ...],
    ) -> StructuredPdfTable:
        width = max((len(row) for row in grid), default=0)
        header_hierarchy = self._header_hierarchy(cells, grid, header_row_count, width)
        local_warnings = list(warnings)
        if not source_pages:
            local_warnings.append("MISSING_PAGE_PROVENANCE")
        if not cells:
            local_warnings.append("EMPTY_TABLE")
        return StructuredPdfTable(
            table_id=table_id,
            title=title,
            source_format=source_format,
            source_pages=source_pages,
            row_count=len(grid),
            column_count=width,
            header_row_count=header_row_count,
            cells=cells,
            cell_reference_matrix=grid,
            header_hierarchy=header_hierarchy,
            part_table_ids=(table_id,),
            source_offset_ranges=source_offset_ranges,
            source_line_ranges=source_line_ranges,
            warnings=tuple(dict.fromkeys(local_warnings)),
        )

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

    @staticmethod
    def _span(raw_value: object, warnings: list[str], field_name: str) -> int:
        try:
            value = int(str(raw_value or "1"))
        except ValueError:
            warnings.append(f"INVALID_{field_name.upper()}:{raw_value}")
            return 1
        if value < 1:
            warnings.append(f"INVALID_{field_name.upper()}:{raw_value}")
            return 1
        return value

    @classmethod
    def _split_markdown_row(cls, line: str) -> list[str]:
        stripped = line.strip()
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|") and not stripped.endswith("\\|"):
            stripped = stripped[:-1]
        cells: list[str] = []
        buffer: list[str] = []
        escaped = False
        for character in stripped:
            if escaped:
                buffer.append(character)
                escaped = False
            elif character == "\\":
                escaped = True
                buffer.append(character)
            elif character == "|":
                cells.append(cls._clean_text("".join(buffer)).replace("\\|", "|"))
                buffer = []
            else:
                buffer.append(character)
        cells.append(cls._clean_text("".join(buffer)).replace("\\|", "|"))
        return cells

    @classmethod
    def _preceding_title(cls, text: str) -> str | None:
        for raw_line in reversed(text.splitlines()):
            line = cls._clean_text(raw_line)
            if not line or line.startswith("<!--") or line.startswith("!["):
                continue
            # A preceding table's closing tag/data row is not the title of the
            # next table in a mixed Markdown/HTML section.
            if re.search(
                r"</?(?:table|thead|tbody|tfoot|tr|th|td|caption)\b",
                line,
                flags=re.IGNORECASE,
            ) or line.count("|") >= 2:
                continue
            line = re.sub(r"^#{1,6}\s*", "", line)
            return line[:500] or None
        return None

    @classmethod
    def _clean_cell_text(cls, cell: Tag) -> str:
        clone = BeautifulSoup(str(cell), "html.parser")
        for nested_table in clone.find_all("table"):
            nested_table.replace_with(cls._clean_text(nested_table.get_text(" ", strip=True)))
        return cls._clean_text(clone.get_text(" ", strip=True))

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    @staticmethod
    def _ensure_grid_row(grid: list[list[str | None]], row_index: int, width: int) -> None:
        while len(grid) <= row_index:
            grid.append([])
        while len(grid[row_index]) < width:
            grid[row_index].append(None)

    @classmethod
    def _set_grid_value(
        cls,
        grid: list[list[str | None]],
        row_index: int,
        column_index: int,
        value: str,
    ) -> None:
        cls._ensure_grid_row(grid, row_index, column_index + 1)
        grid[row_index][column_index] = value

    @staticmethod
    def _normalize_grid(
        grid: Sequence[Sequence[str | None]],
    ) -> tuple[tuple[str | None, ...], ...]:
        width = max((len(row) for row in grid), default=0)
        return tuple(tuple([*row, *([None] * (width - len(row)))]) for row in grid)

    @staticmethod
    def _leading_header_row_count(header_rows: set[int]) -> int:
        count = 0
        while count in header_rows:
            count += 1
        return count

    @staticmethod
    def _header_hierarchy(
        cells: Sequence[StructuredTableCell],
        grid: Sequence[Sequence[str | None]],
        header_row_count: int,
        width: int,
    ) -> tuple[tuple[str, ...], ...]:
        by_id = {cell.cell_id: cell for cell in cells}
        result: list[tuple[str, ...]] = []
        for column_index in range(width):
            levels: list[str] = []
            for row_index in range(min(header_row_count, len(grid))):
                cell_id = grid[row_index][column_index]
                text = by_id[cell_id].text if cell_id in by_id else ""
                if text and text not in levels:
                    levels.append(text)
            result.append(tuple(levels))
        return tuple(result)


class TableContinuationMerger:
    """Merge only evidence-backed table continuations on consecutive PDF pages."""

    _CONTINUATION_RE = re.compile(r"(?:\(续\)|（续）|续表|表续)$")

    def merge(
        self,
        tables: Sequence[StructuredPdfTable],
    ) -> tuple[list[StructuredPdfTable], tuple[TableContinuationDecision, ...]]:
        if not tables:
            return [], ()
        merged: list[StructuredPdfTable] = [tables[0]]
        decisions: list[TableContinuationDecision] = []
        for right in tables[1:]:
            left = merged[-1]
            decision = self._decision(left, right)
            decisions.append(decision)
            if decision.merged:
                merged[-1] = self._merge_pair(left, right, decision)
            else:
                merged.append(right)
        return merged, tuple(decisions)

    def _decision(
        self,
        left: StructuredPdfTable,
        right: StructuredPdfTable,
    ) -> TableContinuationDecision:
        reasons: list[str] = []
        if left.source_page_end is None or right.source_page_start is None:
            reasons.append("MISSING_PAGE_PROVENANCE")
            return self._rejected(left, right, reasons)
        if right.source_page_start != left.source_page_end + 1:
            reasons.append("NON_CONSECUTIVE_PAGES")
            return self._rejected(left, right, reasons)
        reasons.append("CONSECUTIVE_PAGES")
        if left.column_count != right.column_count or left.column_count == 0:
            reasons.append("COLUMN_COUNT_MISMATCH")
            return self._rejected(left, right, reasons)
        reasons.append("COLUMN_COUNT_MATCH")

        score = 0
        left_headers = self._headers(left)
        right_headers = self._headers(right)
        header_match = bool(
            any(any(levels) for levels in left_headers)
            and left_headers == right_headers
        )
        if header_match:
            score += 3
            reasons.append("REPEATED_HEADER_MATCH")
        left_title = self._normalize_title(left.title)
        right_title = self._normalize_title(right.title)
        title_match = bool(left_title and left_title == right_title)
        if title_match:
            score += 2
            reasons.append("TABLE_TITLE_MATCH")
        continuation_title = bool(
            right.title and self._looks_like_continuation_title(right.title)
        )
        if continuation_title:
            score += 2
            reasons.append("CONTINUATION_TITLE")
        # Repeated headers are common across independent tables (for example,
        # multiple ``类型 / 数值`` tables), so they are necessary but never
        # sufficient continuation evidence.  A continuation must additionally
        # retain the same normalized title or carry an explicit continuation label.
        # Consecutive pages and matching column counts have already been enforced
        # above, making all four evidence groups part of the decision.
        merged = header_match and (title_match or continuation_title)
        if not merged:
            reasons.append("INSUFFICIENT_CONTINUATION_EVIDENCE")
        return TableContinuationDecision(
            left_table_id=left.table_id,
            right_table_id=right.table_id,
            merged=merged,
            score=score,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _rejected(
        left: StructuredPdfTable,
        right: StructuredPdfTable,
        reasons: Iterable[str],
    ) -> TableContinuationDecision:
        return TableContinuationDecision(
            left_table_id=left.table_id,
            right_table_id=right.table_id,
            merged=False,
            score=0,
            reasons=tuple(reasons),
        )

    def _merge_pair(
        self,
        left: StructuredPdfTable,
        right: StructuredPdfTable,
        decision: TableContinuationDecision,
    ) -> StructuredPdfTable:
        repeated_header = "REPEATED_HEADER_MATCH" in decision.reasons
        drop_rows = right.header_row_count if repeated_header else 0
        shifted_cells: list[StructuredTableCell] = []
        row_shift = left.row_count - drop_rows
        for cell in right.cells:
            if cell.row_index < drop_rows:
                continue
            shifted_cells.append(
                replace(
                    cell,
                    row_index=cell.row_index + row_shift,
                )
            )
        cells = (*left.cells, *shifted_cells)
        row_count = left.row_count + max(0, right.row_count - drop_rows)
        grid = self._grid_from_cells(cells, row_count, left.column_count)
        source_format = (
            left.source_format
            if left.source_format is right.source_format
            else TableSourceFormat.MIXED
        )
        source_pages = tuple(dict.fromkeys((*left.source_pages, *right.source_pages)))
        title = left.title or right.title
        warnings = tuple(dict.fromkeys((*left.warnings, *right.warnings)))
        return StructuredPdfTable(
            table_id=left.table_id,
            title=title,
            source_format=source_format,
            source_pages=source_pages,
            row_count=row_count,
            column_count=left.column_count,
            header_row_count=left.header_row_count,
            cells=tuple(cells),
            cell_reference_matrix=grid,
            header_hierarchy=left.header_hierarchy or right.header_hierarchy,
            part_table_ids=(*left.part_table_ids, *right.part_table_ids),
            source_offset_ranges=(
                *left.source_offset_ranges,
                *right.source_offset_ranges,
            ),
            source_line_ranges=(*left.source_line_ranges, *right.source_line_ranges),
            warnings=warnings,
        )

    @staticmethod
    def _grid_from_cells(
        cells: Sequence[StructuredTableCell],
        row_count: int,
        column_count: int,
    ) -> tuple[tuple[str | None, ...], ...]:
        grid: list[list[str | None]] = [
            [None for _ in range(column_count)] for _ in range(row_count)
        ]
        for cell in cells:
            for row_offset in range(cell.row_span):
                for column_offset in range(cell.column_span):
                    row_index = cell.row_index + row_offset
                    column_index = cell.column_index + column_offset
                    if row_index < row_count and column_index < column_count:
                        grid[row_index][column_index] = cell.cell_id
        return tuple(tuple(row) for row in grid)

    @staticmethod
    def _headers(table: StructuredPdfTable) -> tuple[tuple[str, ...], ...]:
        return tuple(
            tuple(re.sub(r"\s+", "", value).lower() for value in levels)
            for levels in table.header_hierarchy
        )

    @classmethod
    def _normalize_title(cls, title: str | None) -> str:
        value = re.sub(r"\s+", "", title or "").lower()
        value = cls._CONTINUATION_RE.sub("", value)
        return re.sub(r"[\W_]+", "", value)

    @classmethod
    def _looks_like_continuation_title(cls, title: str) -> bool:
        value = re.sub(r"\s+", "", title)
        return "续表" in value or "（续）" in value or value.endswith("(续)")


__all__ = [
    "OdlTableAbEvaluator",
    "OdlTableAbReport",
    "OdlTableMethod",
    "OdlTableStrategyMetrics",
    "PdfTableStructureExtractor",
    "PdfTableStructureReport",
    "StructuredPdfTable",
    "StructuredTableCell",
    "TableContinuationDecision",
    "TableContinuationMerger",
    "TableSourceFormat",
]
