from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf

from app.rag.core.parser.pdf.content_validation import (
    PdfSourcePageStructure,
    RegexMarkdownContentSignalDetector,
    formula_signature,
    normalize_table_matrix,
)
from app.rag.core.parser.pdf.quality import PdfQualityReport


@dataclass(frozen=True, slots=True)
class PdfSourceStructureInspection:
    pages: tuple[PdfSourcePageStructure, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "warnings": list(self.warnings),
            "pages": [
                {
                    "page_number": page.page_number,
                    "table_count": page.table_count,
                    "image_count": page.image_count,
                    "image_coverage_ratio": page.image_coverage_ratio,
                    "formula_count": page.formula_count,
                    "odl_formula_count": page.odl_formula_count,
                    "table_matrices": [
                        [list(row) for row in matrix] for matrix in page.table_matrices
                    ],
                    "formula_signatures": [
                        list(signature) for signature in page.formula_signatures
                    ],
                }
                for page in self.pages
            ],
        }


class PdfSourceStructureInspector:
    """Collect conservative source evidence used by the final content validator."""

    _PAGE_MARKER_RE = re.compile(
        r"^[\t ]*<!--[\t ]*ODL_PAGE:(\d+)[\t ]*-->[\t ]*\r?$",
        flags=re.MULTILINE,
    )
    _FORMULA_LINE_RE = re.compile(r"[=≈≠≤≥±×÷∑∫√]")

    def __init__(self) -> None:
        self._detector = RegexMarkdownContentSignalDetector()

    def inspect(
        self,
        pdf_path: str | Path,
        odl_markdown: str,
        quality_report: PdfQualityReport,
    ) -> PdfSourceStructureInspection:
        sections = self._split_pages(odl_markdown)
        quality_by_page = {page.page_number: page for page in quality_report.per_page}
        warnings: list[str] = []
        pages: list[PdfSourcePageStructure] = []

        document = pymupdf.open(filename=str(pdf_path))
        try:
            for page_index in range(document.page_count):
                page_number = page_index + 1
                page = document.load_page(page_index)
                section = sections.get(page_number, "")
                odl_signals = self._detector.detect(section)
                quality = quality_by_page.get(page_number)
                source_is_scan = bool(getattr(quality, "is_image_only", False))
                table_count = odl_signals.table_count
                table_matrices = []
                try:
                    finder = getattr(page, "find_tables", None)
                    if callable(finder) and not source_is_scan:
                        found_tables = list(finder().tables)
                        table_count = max(table_count, len(found_tables))
                        for table_index, table in enumerate(found_tables, start=1):
                            matrix = normalize_table_matrix(table.extract() or ())
                            if matrix:
                                table_matrices.append(matrix)
                            else:
                                warnings.append(
                                    "TABLE_MATRIX_UNAVAILABLE:"
                                    f"page={page_number},table={table_index}"
                                )
                except Exception as exc:
                    warnings.append(
                        f"TABLE_INSPECTION_FAILED:page={page_number},type={type(exc).__name__}"
                    )

                image_count = int(getattr(quality, "image_count", 0) or 0)
                image_count = max(
                    image_count,
                    int(getattr(quality, "markdown_image_reference_count", 0) or 0),
                )
                image_coverage_ratio = float(
                    getattr(quality, "image_coverage_ratio", 0.0) or 0.0
                )
                # Hidden text layers on raster pages are not reliable source truth;
                # OCR/vision output for those pages is assessed by confidence and the
                # offline gold suite instead of exact born-digital signatures.
                source_text = "" if source_is_scan else (page.get_text("text") or "")
                formula_count = sum(
                    1
                    for line in source_text.splitlines()
                    if self._looks_like_formula(line)
                )
                formula_signatures = tuple(
                    signature
                    for line in source_text.splitlines()
                    if self._looks_like_formula(line)
                    if (signature := formula_signature(line))
                    if self._signature_is_verifiable(signature)
                )
                pages.append(
                    PdfSourcePageStructure(
                        page_number=page_number,
                        table_count=table_count,
                        image_count=image_count,
                        image_coverage_ratio=image_coverage_ratio,
                        formula_count=formula_count,
                        odl_formula_count=odl_signals.formula_count,
                        table_matrices=tuple(table_matrices),
                        formula_signatures=formula_signatures,
                    )
                )
        finally:
            document.close()

        return PdfSourceStructureInspection(
            pages=tuple(pages),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    @classmethod
    def _looks_like_formula(cls, line: str) -> bool:
        normalized = line.strip()
        if (
            len(normalized) < 3
            or len(normalized) > 200
            or "://" in normalized
            or not cls._FORMULA_LINE_RE.search(normalized)
        ):
            return False
        if "=" in normalized:
            return RegexMarkdownContentSignalDetector._looks_like_plain_equation(
                normalized
            )
        return bool(
            re.search(r"[≈≠≤≥±×÷∑∫√]", normalized)
            and any(character.isalnum() for character in normalized)
        )

    @staticmethod
    def _signature_is_verifiable(signature: tuple[str, ...]) -> bool:
        operators = {"=", "≈", "≠", "≤", "≥", "±", "×", "÷", "+", "-", "^", "_"}
        return bool(
            any(token in operators for token in signature)
            and any(any(character.isalnum() for character in token) for token in signature)
        )

    @classmethod
    def _split_pages(cls, markdown: str) -> dict[int, str]:
        matches = list(cls._PAGE_MARKER_RE.finditer(markdown or ""))
        sections: dict[int, str] = {}
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
            sections[int(match.group(1))] = markdown[match.end() : end]
        return sections


__all__ = [
    "PdfSourceStructureInspection",
    "PdfSourceStructureInspector",
]
