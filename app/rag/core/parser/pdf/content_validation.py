"""Pure page-level structural validation for parsed PDF Markdown.

The validator intentionally consumes already collected source-page signals instead of
opening a PDF or changing document state.  This keeps the policy reusable from any
ingestion flow while making the validation result deterministic and JSON serializable.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from html import unescape
from typing import Protocol

from bs4 import BeautifulSoup

TableMatrix = tuple[tuple[str, ...], ...]
FormulaSignature = tuple[str, ...]


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", flags=re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MARKDOWN_INLINE_LINK_RE = re.compile(
    r"(?<!!)\[(?P<label>[^\]\r\n]*)\]\(\s*(?:<[^>\r\n]+>|[^)\r\n]+)\s*\)"
)
_FORMULA_NUMBER_RE = re.compile(r"[\(（]\s*(\d+(?:[.\-–]\d+)*)\s*[\)）]")
_FORMULA_TOKEN_RE = re.compile(
    r"\(\d+(?:[.\-]\d+)*\)|"
    r"[A-Za-z]+(?:[_^](?:\{?[A-Za-z0-9]+\}?))?|"
    r"\d+(?:\.\d+)?|"
    r"[=≈≠≤≥±×÷∑∫√+\-*/^_()]"
)


def normalize_table_cell(value: object) -> str:
    """Canonicalize visible cell text without counting markup or link targets."""

    text = "" if value is None else str(value)
    text = _HTML_COMMENT_RE.sub(" ", text)
    text = _MARKDOWN_INLINE_LINK_RE.sub(lambda match: match.group("label"), text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = unescape(text)
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", "", text)


def normalize_table_matrix(rows: Sequence[Sequence[object]]) -> TableMatrix:
    """Return a rectangular normalized matrix, or an empty tuple when unreliable."""

    if not rows:
        return ()
    widths = [len(row) for row in rows]
    if not widths or min(widths) < 1 or len(set(widths)) != 1:
        return ()
    matrix = tuple(tuple(normalize_table_cell(cell) for cell in row) for row in rows)
    if not any(cell for row in matrix for cell in row):
        return ()
    return matrix


def formula_signature(value: str) -> FormulaSignature:
    """Extract ordered critical formula tokens for exact runtime comparison.

    Equivalent display spellings (for example ``\\times`` and ``×``) share one
    canonical token, while variables, numbers, formula identifiers and operator order
    remain exact.  Chinese prose surrounding a formula is deliberately ignored.
    """

    text = unicodedata.normalize("NFKC", unescape(value or ""))
    text = re.sub(r"\\tag\s*\{\s*([^{}]+)\s*\}", r"(\1)", text)
    replacements = {
        r"\times": "×",
        r"\cdot": "×",
        r"\div": "÷",
        r"\leq": "≤",
        r"\le": "≤",
        r"\geq": "≥",
        r"\ge": "≥",
        r"\neq": "≠",
        r"\pm": "±",
        r"\sum": "∑",
        r"\int": "∫",
        r"\sqrt": "√",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"\\(?:mathrm|mathbf|text|operatorname)\s*\{([^{}]*)\}", r"\1", text)
    text = _HTML_TAG_RE.sub("", text)
    text = _FORMULA_NUMBER_RE.sub(lambda match: f"({match.group(1)})", text)
    tokens: list[str] = []
    for match in _FORMULA_TOKEN_RE.finditer(text):
        token = match.group(0).replace("{", "").replace("}", "")
        if token in {"*", "·"}:
            token = "×"
        elif token == "/":
            token = "÷"
        tokens.append(token)
    return tuple(tokens)


def _require_native_non_negative_int(field_name: str, value: object) -> None:
    """Keep report payloads JSON-native instead of accepting bool/NumPy/float values."""

    if type(value) is not int:  # Deliberately stricter than isinstance(value, int).
        raise TypeError(f"{field_name} must be a native int")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")


def _require_native_positive_int(field_name: str, value: object) -> None:
    _require_native_non_negative_int(field_name, value)
    if value < 1:
        raise ValueError(f"{field_name} must be greater than zero")


class PdfContentIssueSeverity(StrEnum):
    """Whether one structural mismatch blocks acceptance or is advisory."""

    BLOCKING = "BLOCKING"
    WARNING = "WARNING"


@dataclass(frozen=True, slots=True)
class PdfSourcePageStructure:
    """Source-side structural evidence for one original PDF page.

    Counts are expected to come from the original PDF page geometry or the structured
    OpenDataLoader result.  ``odl_formula_count`` is deliberately separate because an
    ODL-only formula hint is weaker than a formula confirmed from the source page.
    """

    page_number: int
    table_count: int = 0
    image_count: int = 0
    image_coverage_ratio: float = 0.0
    formula_count: int = 0
    odl_formula_count: int = 0
    table_matrices: tuple[TableMatrix, ...] = ()
    formula_signatures: tuple[FormulaSignature, ...] = ()

    def __post_init__(self) -> None:
        _require_native_positive_int("page_number", self.page_number)
        for field_name in (
            "table_count",
            "image_count",
            "formula_count",
            "odl_formula_count",
        ):
            _require_native_non_negative_int(field_name, getattr(self, field_name))
        if type(self.image_coverage_ratio) not in {int, float} or isinstance(
            self.image_coverage_ratio, bool
        ):
            raise TypeError("image_coverage_ratio must be a native int or float")
        if not math.isfinite(self.image_coverage_ratio) or not (
            0 <= self.image_coverage_ratio <= 1
        ):
            raise ValueError("image_coverage_ratio must be between 0 and 1")
        object.__setattr__(self, "image_coverage_ratio", float(self.image_coverage_ratio))
        self._validate_nested_strings("table_matrices", self.table_matrices, depth=3)
        self._validate_nested_strings("formula_signatures", self.formula_signatures, depth=2)
        normalized_matrices = tuple(
            normalize_table_matrix(matrix) for matrix in self.table_matrices
        )
        if any(not matrix for matrix in normalized_matrices):
            raise ValueError("table_matrices must contain rectangular non-empty matrices")
        object.__setattr__(self, "table_matrices", normalized_matrices)
        if self.table_matrices and self.table_count < len(self.table_matrices):
            object.__setattr__(self, "table_count", len(self.table_matrices))
        if self.formula_signatures and self.formula_count < len(self.formula_signatures):
            object.__setattr__(self, "formula_count", len(self.formula_signatures))

    @staticmethod
    def _validate_nested_strings(field_name: str, value: object, *, depth: int) -> None:
        current = [value]
        for _ in range(depth):
            next_values: list[object] = []
            for item in current:
                if not isinstance(item, tuple):
                    raise TypeError(f"{field_name} must use immutable tuples")
                next_values.extend(item)
            current = next_values
        if any(not isinstance(item, str) for item in current):
            raise TypeError(f"{field_name} leaves must be strings")


@dataclass(frozen=True, slots=True)
class MarkdownPageContentSignals:
    """Structural representations found in final Markdown for one page."""

    table_count: int = 0
    image_reference_count: int = 0
    visual_description_count: int = 0
    formula_count: int = 0
    table_matrices: tuple[TableMatrix, ...] = ()
    formula_signatures: tuple[FormulaSignature, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "table_count",
            "image_reference_count",
            "visual_description_count",
            "formula_count",
        ):
            _require_native_non_negative_int(field_name, getattr(self, field_name))
        PdfSourcePageStructure._validate_nested_strings(
            "table_matrices", self.table_matrices, depth=3
        )
        PdfSourcePageStructure._validate_nested_strings(
            "formula_signatures", self.formula_signatures, depth=2
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "table_count": self.table_count,
            "image_reference_count": self.image_reference_count,
            "visual_description_count": self.visual_description_count,
            "formula_count": self.formula_count,
            "table_matrices": [
                [list(row) for row in matrix] for matrix in self.table_matrices
            ],
            "formula_signatures": [list(signature) for signature in self.formula_signatures],
        }


class MarkdownContentSignalDetector(Protocol):
    """Injectable strategy for detecting final Markdown representations."""

    def detect(self, markdown: str) -> MarkdownPageContentSignals: ...


@dataclass(frozen=True, slots=True)
class PdfContentValidationPolicy:
    """Injectable thresholds and severities for PDF structure validation."""

    high_image_coverage_ratio: float = 0.6
    min_visual_description_chars: int = 8
    missing_table_severity: PdfContentIssueSeverity = PdfContentIssueSeverity.BLOCKING
    missing_image_severity: PdfContentIssueSeverity = PdfContentIssueSeverity.WARNING
    missing_high_coverage_image_severity: PdfContentIssueSeverity = (
        PdfContentIssueSeverity.BLOCKING
    )
    missing_source_formula_severity: PdfContentIssueSeverity = (
        PdfContentIssueSeverity.BLOCKING
    )
    missing_odl_formula_severity: PdfContentIssueSeverity = PdfContentIssueSeverity.WARNING
    allow_empty_source_pages: bool = False
    allow_page_set_mismatch: bool = False

    def __post_init__(self) -> None:
        if type(self.high_image_coverage_ratio) not in {int, float} or isinstance(
            self.high_image_coverage_ratio, bool
        ):
            raise TypeError("high_image_coverage_ratio must be a native int or float")
        if not math.isfinite(self.high_image_coverage_ratio) or not (
            0 <= self.high_image_coverage_ratio <= 1
        ):
            raise ValueError("high_image_coverage_ratio must be between 0 and 1")
        object.__setattr__(
            self,
            "high_image_coverage_ratio",
            float(self.high_image_coverage_ratio),
        )
        _require_native_positive_int(
            "min_visual_description_chars",
            self.min_visual_description_chars,
        )
        for field_name in (
            "missing_table_severity",
            "missing_image_severity",
            "missing_high_coverage_image_severity",
            "missing_source_formula_severity",
            "missing_odl_formula_severity",
        ):
            if not isinstance(getattr(self, field_name), PdfContentIssueSeverity):
                raise TypeError(f"{field_name} must be PdfContentIssueSeverity")
        for field_name in ("allow_empty_source_pages", "allow_page_set_mismatch"):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be bool")

    def to_dict(self) -> dict[str, object]:
        return {
            "high_image_coverage_ratio": self.high_image_coverage_ratio,
            "min_visual_description_chars": self.min_visual_description_chars,
            "missing_table_severity": self.missing_table_severity.value,
            "missing_image_severity": self.missing_image_severity.value,
            "missing_high_coverage_image_severity": (
                self.missing_high_coverage_image_severity.value
            ),
            "missing_source_formula_severity": self.missing_source_formula_severity.value,
            "missing_odl_formula_severity": self.missing_odl_formula_severity.value,
            "allow_empty_source_pages": self.allow_empty_source_pages,
            "allow_page_set_mismatch": self.allow_page_set_mismatch,
        }


@dataclass(frozen=True, slots=True)
class PdfContentValidationIssue:
    code: str
    category: str
    page_number: int
    severity: PdfContentIssueSeverity
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "category": self.category,
            "page_number": self.page_number,
            "severity": self.severity.value,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class PdfPageContentValidation:
    page_number: int
    source_table_count: int
    output_table_count: int
    source_image_count: int
    source_image_coverage_ratio: float
    output_image_reference_count: int
    output_visual_description_count: int
    source_formula_count: int
    odl_formula_count: int
    output_formula_count: int
    blocking_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "table_counts": {
                "source": self.source_table_count,
                "output": self.output_table_count,
            },
            "image_counts": {
                "source": self.source_image_count,
                "source_coverage_ratio": self.source_image_coverage_ratio,
                "output_references": self.output_image_reference_count,
                "output_visual_descriptions": self.output_visual_description_count,
            },
            "formula_counts": {
                "source": self.source_formula_count,
                "odl": self.odl_formula_count,
                "output": self.output_formula_count,
            },
            "blocking_codes": list(self.blocking_codes),
            "warning_codes": list(self.warning_codes),
        }


@dataclass(frozen=True, slots=True)
class PdfContentValidationReport:
    """Structural validation result, not a claim of full document correctness.

    ``structural_passed`` only reflects configured blocking table/image/formula
    checks.  It does not cover OCR accuracy, prose completeness, reading order or
    semantic fidelity.  ``page_set_complete`` separately reports whether source and
    Markdown page numbers match when mismatch-tolerant validation is enabled.
    """

    structural_passed: bool
    page_set_complete: bool
    evaluated_page_count: int
    missing_markdown_pages: tuple[int, ...]
    unexpected_markdown_pages: tuple[int, ...]
    source_table_count: int
    output_table_count: int
    source_image_count: int
    high_image_coverage_page_count: int
    output_image_reference_count: int
    output_visual_description_count: int
    source_formula_count: int
    odl_formula_count: int
    output_formula_count: int
    table_affected_pages: tuple[int, ...]
    image_affected_pages: tuple[int, ...]
    formula_affected_pages: tuple[int, ...]
    blocking_issues: tuple[PdfContentValidationIssue, ...]
    warnings: tuple[PdfContentValidationIssue, ...]
    per_page: tuple[PdfPageContentValidation, ...]
    policy: PdfContentValidationPolicy

    def to_dict(self) -> dict[str, object]:
        """Return a report containing only JSON-native values."""

        return {
            "structural_passed": self.structural_passed,
            "page_set_complete": self.page_set_complete,
            "evaluated_page_count": self.evaluated_page_count,
            "missing_markdown_pages": list(self.missing_markdown_pages),
            "unexpected_markdown_pages": list(self.unexpected_markdown_pages),
            "blocking_issue_count": len(self.blocking_issues),
            "warning_count": len(self.warnings),
            "table_counts": {
                "source": self.source_table_count,
                "output": self.output_table_count,
            },
            "image_counts": {
                "source": self.source_image_count,
                "high_coverage_pages": self.high_image_coverage_page_count,
                "output_references": self.output_image_reference_count,
                "output_visual_descriptions": self.output_visual_description_count,
            },
            "formula_counts": {
                "source": self.source_formula_count,
                "odl": self.odl_formula_count,
                "output": self.output_formula_count,
            },
            "affected_pages": {
                "table": list(self.table_affected_pages),
                "image": list(self.image_affected_pages),
                "formula": list(self.formula_affected_pages),
            },
            "blocking_issues": [issue.to_dict() for issue in self.blocking_issues],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "per_page": [page.to_dict() for page in self.per_page],
            "policy": self.policy.to_dict(),
        }


class RegexMarkdownContentSignalDetector:
    """Conservative detector for tables, image evidence and formula markup."""

    _MARKDOWN_TABLE_DELIMITER_CELL_RE = re.compile(r"^:?-{3,}:?$")
    _HTML_TABLE_RE = re.compile(
        r"<table\b[^>]*>.*?</table>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    _HTML_TABLE_ROW_RE = re.compile(
        r"<tr\b[^>]*>(?P<body>.*?)</tr\s*>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    _HTML_TABLE_CELL_RE = re.compile(
        r"<(?P<tag>td|th)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)\s*>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    _HTML_TABLE_COLSPAN_RE = re.compile(
        r"\bcolspan\s*=\s*(?:\"(?P<double>\d+)\"|'(?P<single>\d+)'|(?P<bare>\d+))",
        flags=re.IGNORECASE,
    )
    _MARKDOWN_IMAGE_RE = re.compile(
        r"!\[[^\]\r\n]*\]\(\s*(?P<target><[^>\r\n]+>|[^)\r\n]*\S[^)\r\n]*)\s*\)",
        flags=re.IGNORECASE,
    )
    _REFERENCE_IMAGE_RE = re.compile(
        r"!\[(?P<alt>[^\]\r\n]*)\]\s*\[(?P<label>[^\]\r\n]*)\]",
        flags=re.IGNORECASE,
    )
    _REFERENCE_DEFINITION_RE = re.compile(
        r"^[\t ]{0,3}\[(?P<label>[^\]\r\n]+)\]:[\t ]*(?P<target>\S+)",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    _HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", flags=re.IGNORECASE | re.DOTALL)
    _HTML_IMAGE_SRC_RE = re.compile(
        r"\bsrc\s*=\s*(?:\"(?P<double>[^\"]+)\"|'(?P<single>[^']+)'|"
        r"(?P<bare>[^\s\"'=<>`]+))",
        flags=re.IGNORECASE,
    )
    _VISION_DESCRIPTION_RE = re.compile(
        r"\[视觉描述\|src=\S+?:\s*(?P<description>[^\]\r\n]+)\]"
    )
    _INLINE_IMAGE_DESCRIPTION_RE = re.compile(
        r"^\s*图片说明[：:]\s*(?P<description>.+)$",
        flags=re.MULTILINE,
    )
    _BLOCK_IMAGE_DESCRIPTION_RE = re.compile(
        r"^\s*>\s*\[图片\]\s*(?P<description>.+)$",
        flags=re.MULTILINE,
    )
    _FIGCAPTION_RE = re.compile(
        r"<figcaption\b[^>]*>(?P<description>.*?)</figcaption>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    _HTML_TAG_RE = re.compile(r"<[^>]+>")
    _HTML_COMMENT_RE = re.compile(r"<!--.*?-->", flags=re.DOTALL)
    _MARKDOWN_LINK_RE = re.compile(
        r"(?<!!)\[[^\]\r\n]*\]\(\s*(?:<[^>\r\n]+>|[^)\r\n]+)\s*\)"
    )
    _FORMULA_PATTERNS = (
        re.compile(
            r"<math\b[^>]*>(?P<body>.*?)</math>",
            flags=re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"\\begin\{(?P<environment>equation\*?|align\*?|gather\*?|multline\*?)\}"
            r"(?P<body>.*?)\\end\{(?P=environment)\}",
            flags=re.DOTALL,
        ),
        re.compile(r"\$\$(?P<body>.*?)\$\$", flags=re.DOTALL),
        re.compile(r"\\\[(?P<body>.*?)\\\]", flags=re.DOTALL),
        re.compile(r"\\\((?P<body>.*?)\\\)", flags=re.DOTALL),
    )
    _INLINE_DOLLAR_FORMULA_RE = re.compile(r"(?<!\$)\$(?!\$)([^$\r\n]+?)\$(?!\$)")
    _FENCED_CODE_RE = re.compile(
        r"^[\t ]{0,3}(?P<fence>`{3,}|~{3,})[^\r\n]*\r?\n.*?"
        r"^[\t ]{0,3}(?P=fence)[\t ]*$",
        flags=re.MULTILINE | re.DOTALL,
    )
    _UNCLOSED_FENCED_CODE_RE = re.compile(
        r"^[\t ]{0,3}(?:`{3,}|~{3,})[^\r\n]*\r?\n.*\Z",
        flags=re.MULTILINE | re.DOTALL,
    )
    _INLINE_CODE_RE = re.compile(r"(?<!`)`+[^`\r\n]*`+(?!`)")
    def __init__(self, *, min_visual_description_chars: int = 8) -> None:
        _require_native_positive_int(
            "min_visual_description_chars",
            min_visual_description_chars,
        )
        self.min_visual_description_chars = min_visual_description_chars

    def detect(self, markdown: str) -> MarkdownPageContentSignals:
        text = self._strip_code(markdown or "")
        table_matrices = self._extract_tables(text)
        formula_signatures = self._extract_formula_signatures(text)
        return MarkdownPageContentSignals(
            table_count=len(table_matrices),
            image_reference_count=self._count_image_references(text),
            visual_description_count=self._count_visual_descriptions(text),
            formula_count=len(formula_signatures),
            table_matrices=table_matrices,
            formula_signatures=formula_signatures,
        )

    @classmethod
    def _count_tables(cls, markdown: str) -> int:
        return len(cls._extract_tables(markdown))

    @classmethod
    def _extract_tables(cls, markdown: str) -> tuple[TableMatrix, ...]:
        matrices: list[TableMatrix] = []
        lines = markdown.splitlines()
        index = 1
        while index < len(lines):
            delimiter_cells = cls._split_pipe_row(lines[index])
            if not delimiter_cells or not all(
                cls._MARKDOWN_TABLE_DELIMITER_CELL_RE.fullmatch(cell)
                for cell in delimiter_cells
            ):
                index += 1
                continue

            header_cells = cls._split_pipe_row(lines[index - 1])
            cursor = index + 1
            body_rows: list[list[str]] = []
            while cursor < len(lines) and lines[cursor].strip() and "|" in lines[cursor]:
                body_rows.append(cls._split_pipe_row(lines[cursor]))
                cursor += 1

            expected_columns = len(delimiter_cells)
            rows_have_consistent_columns = bool(header_cells) and all(
                len(row) == expected_columns for row in [header_cells, *body_rows]
            )
            body_has_content = any(
                cls._effective_character_count(cell) > 0
                for row in body_rows
                for cell in row
            )
            if body_rows and rows_have_consistent_columns and body_has_content:
                matrix = normalize_table_matrix([header_cells, *body_rows])
                if matrix:
                    matrices.append(matrix)
            index = max(index + 1, cursor)

        for match in cls._HTML_TABLE_RE.finditer(markdown):
            matrix = cls._extract_html_table_matrix(match.group(0))
            if matrix:
                matrices.append(matrix)
        return tuple(matrices)

    @classmethod
    def _extract_html_table_matrix(cls, html_table: str) -> TableMatrix:
        soup = BeautifulSoup(html_table, "html.parser")
        table = soup.find("table")
        if table is None:
            return ()

        rows: list[list[str]] = []
        widths: list[int] = []
        carry: dict[int, int] = {}
        has_data = False
        for row_tag in table.find_all("tr"):
            if row_tag.find_parent("table") is not table:
                continue
            cells = row_tag.find_all(["td", "th"], recursive=False)
            if not cells and not carry:
                continue
            reserved = set(carry)
            row_values: dict[int, str] = {column: "" for column in reserved}
            new_carry: dict[int, int] = {}
            column = 0
            for cell in cells:
                while column in reserved or column in row_values:
                    column += 1
                try:
                    colspan = min(100, max(1, int(cell.get("colspan", 1))))
                    rowspan = min(100, max(1, int(cell.get("rowspan", 1))))
                except (TypeError, ValueError):
                    return ()
                cell_text = normalize_table_cell(cell.get_text(" ", strip=True))
                row_values[column] = cell_text
                if cell.name.casefold() == "td" and cell_text:
                    has_data = True
                for offset in range(1, colspan):
                    row_values[column + offset] = ""
                if rowspan > 1:
                    for offset in range(colspan):
                        new_carry[column + offset] = rowspan - 1
                column += colspan

            width = max(row_values, default=-1) + 1
            rows.append([row_values.get(index, "") for index in range(width)])
            widths.append(width)
            carry = {
                column: remaining - 1
                for column, remaining in carry.items()
                if remaining > 1
            }
            carry.update(new_carry)

        if not rows or not has_data or len(set(widths)) != 1:
            return ()
        return normalize_table_matrix(rows)

    @classmethod
    def _html_cell_column_span(cls, attributes: str) -> int:
        match = cls._HTML_TABLE_COLSPAN_RE.search(attributes)
        if match is None:
            return 1
        raw_value = next(
            value
            for value in match.group("double", "single", "bare")
            if value is not None
        )
        return max(1, int(raw_value))

    @staticmethod
    def _split_pipe_row(value: str) -> list[str]:
        """Split a GFM row without treating escaped or inline-code pipes as columns."""

        row = value.strip()
        if not row or "|" not in row:
            return []
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|") and not row.endswith(r"\|"):
            row = row[:-1]

        cells: list[str] = []
        current: list[str] = []
        escaped = False
        code_fence_length = 0
        index = 0
        while index < len(row):
            character = row[index]
            if character == "`" and not escaped:
                run_end = index + 1
                while run_end < len(row) and row[run_end] == "`":
                    run_end += 1
                run_length = run_end - index
                if code_fence_length == 0:
                    code_fence_length = run_length
                elif run_length == code_fence_length:
                    code_fence_length = 0
                current.extend(row[index:run_end])
                index = run_end
                escaped = False
                continue
            if character == "|" and not escaped and code_fence_length == 0:
                cells.append("".join(current).strip())
                current = []
            else:
                current.append(character)
            if character == "\\" and not escaped:
                escaped = True
            else:
                escaped = False
            index += 1
        cells.append("".join(current).strip())
        return cells

    @classmethod
    def _count_image_references(cls, markdown: str) -> int:
        definitions = {
            cls._normalize_reference_label(match.group("label"))
            for match in cls._REFERENCE_DEFINITION_RE.finditer(markdown)
            if cls._reference_target_is_valid(match.group("target"))
        }
        valid_reference_images = sum(
            cls._normalize_reference_label(match.group("label") or match.group("alt"))
            in definitions
            for match in cls._REFERENCE_IMAGE_RE.finditer(markdown)
        )
        valid_html_images = 0
        for match in cls._HTML_IMAGE_RE.finditer(markdown):
            source_match = cls._HTML_IMAGE_SRC_RE.search(match.group(0))
            if source_match is None:
                continue
            source = next(
                (
                    value
                    for value in source_match.group("double", "single", "bare")
                    if value is not None
                ),
                "",
            )
            if source.strip():
                valid_html_images += 1
        return (
            len(cls._MARKDOWN_IMAGE_RE.findall(markdown))
            + valid_reference_images
            + valid_html_images
        )

    @staticmethod
    def _normalize_reference_label(value: str) -> str:
        return " ".join(value.split()).casefold()

    @staticmethod
    def _reference_target_is_valid(value: str) -> bool:
        target = value.strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        return bool(target)

    def _count_visual_descriptions(self, markdown: str) -> int:
        descriptions: list[str] = []
        for pattern in (
            self._VISION_DESCRIPTION_RE,
            self._INLINE_IMAGE_DESCRIPTION_RE,
            self._BLOCK_IMAGE_DESCRIPTION_RE,
            self._FIGCAPTION_RE,
        ):
            descriptions.extend(match.group("description") for match in pattern.finditer(markdown))
        return sum(
            self._effective_character_count(self._HTML_TAG_RE.sub("", description))
            >= self.min_visual_description_chars
            for description in descriptions
        )

    @classmethod
    def _count_formulas(cls, markdown: str) -> int:
        return len(cls._extract_formula_signatures(markdown))

    @classmethod
    def _extract_formula_signatures(cls, markdown: str) -> tuple[FormulaSignature, ...]:
        remaining = markdown
        signatures: list[FormulaSignature] = []
        for pattern in cls._FORMULA_PATTERNS:
            matches = list(pattern.finditer(remaining))
            signatures.extend(
                signature
                for match in matches
                if cls._formula_body_has_content(match.group("body"))
                if (signature := formula_signature(match.group("body")))
            )
            remaining = pattern.sub(" ", remaining)
        inline_matches = list(cls._INLINE_DOLLAR_FORMULA_RE.finditer(remaining))
        signatures.extend(
            signature
            for match in inline_matches
            if cls._looks_like_inline_formula(match.group(1))
            if (signature := formula_signature(match.group(1)))
        )
        remaining = cls._INLINE_DOLLAR_FORMULA_RE.sub(" ", remaining)
        remaining = cls._strip_malformed_formula_regions(remaining)
        remaining = cls._strip_non_formula_markup(remaining)
        signatures.extend(
            signature
            for line in remaining.splitlines()
            if cls._looks_like_plain_equation(line)
            if (signature := formula_signature(line))
        )
        return tuple(signatures)

    @classmethod
    def _formula_body_has_content(cls, value: str) -> bool:
        without_tags = cls._HTML_TAG_RE.sub(" ", value)
        return bool(
            re.search(
                r"[A-Za-z0-9\u3400-\u9fff]|\\[A-Za-z]+|[\u2211\u222b\u221a\u221e\u03c0]",
                without_tags,
            )
        )

    @staticmethod
    def _looks_like_inline_formula(value: str) -> bool:
        compact = re.sub(r"\s+", "", value)
        if not compact or re.fullmatch(r"[\d.,]+", compact):
            return False
        return bool(
            re.search(r"\\[A-Za-z]+|[=^_{}+*/×÷≤≥∑∫√]", compact)
            or re.fullmatch(r"[A-Za-z]", compact)
        )

    @staticmethod
    def _looks_like_plain_equation(value: str) -> bool:
        stripped = value.strip()
        if re.search(r'''[<>"'`:$]''', stripped):
            return False
        compact = re.sub(r"\s+", "", stripped)
        if not 3 <= len(compact) <= 200 or compact.count("=") != 1:
            return False
        left, right = compact.split("=", 1)
        if not left or not right:
            return False
        allowed = r"[A-Za-z0-9\u3400-\u9fff_{}()[\]+\-*/×÷·.,%^\\]+"
        if re.fullmatch(allowed, left) is None or re.fullmatch(allowed, right) is None:
            return False
        has_math_operator = bool(re.search(r"[_{}()[\]+\-*/×÷·%^\\]", compact))
        has_spaced_equals = bool(re.search(r"\s=\s", stripped))
        short_symbolic_left = bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]?", left))
        return (
            has_math_operator
            or has_spaced_equals
            or short_symbolic_left
        )

    @classmethod
    def _strip_malformed_formula_regions(cls, value: str) -> str:
        # Valid pairs were removed earlier.  Any opening marker left here is unclosed
        # or closes with a different environment, so discard the entire malformed
        # region before considering plain-text equations.
        without_html_math = re.sub(
            r"<math\b[^>]*>.*\Z",
            " ",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        without_html_math = re.sub(
            r"^.*</math\s*>.*$",
            " ",
            without_html_math,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        without_environments = re.sub(
            r"\\begin\{(?:equation\*?|align\*?|gather\*?|multline\*?)\}.*?"
            r"(?:\\end\{(?:equation\*?|align\*?|gather\*?|multline\*?)\}|\Z)",
            " ",
            without_html_math,
            flags=re.DOTALL,
        )
        without_environments = re.sub(
            r"^.*(?:\\begin|\\end)\{(?:equation\*?|align\*?|gather\*?|multline\*?)\}.*$",
            " ",
            without_environments,
            flags=re.MULTILINE,
        )
        without_dollars = re.sub(r"\$\$.*\Z", " ", without_environments, flags=re.DOTALL)
        without_brackets = re.sub(r"\\\[.*\Z", " ", without_dollars, flags=re.DOTALL)
        return re.sub(r"\\\(.*\Z", " ", without_brackets, flags=re.DOTALL)

    @classmethod
    def _strip_non_formula_markup(cls, value: str) -> str:
        without_comments = cls._HTML_COMMENT_RE.sub(" ", value)
        without_images = cls._MARKDOWN_IMAGE_RE.sub(" ", without_comments)
        without_reference_images = cls._REFERENCE_IMAGE_RE.sub(" ", without_images)
        without_links = cls._MARKDOWN_LINK_RE.sub(" ", without_reference_images)
        without_html = cls._HTML_TAG_RE.sub(" ", without_links)
        return cls._REFERENCE_DEFINITION_RE.sub(" ", without_html)

    @classmethod
    def _strip_code(cls, markdown: str) -> str:
        without_fenced = cls._FENCED_CODE_RE.sub(" ", markdown)
        without_unclosed_fenced = cls._UNCLOSED_FENCED_CODE_RE.sub(" ", without_fenced)
        return cls._INLINE_CODE_RE.sub(" ", without_unclosed_fenced)

    @staticmethod
    def _effective_character_count(value: str) -> int:
        return sum(character.isalnum() for character in value)


class PdfContentValidator:
    """Compare source-page structure with final per-page Markdown representations."""

    def __init__(
        self,
        *,
        policy: PdfContentValidationPolicy | None = None,
        detector: MarkdownContentSignalDetector | None = None,
    ) -> None:
        self.policy = policy or PdfContentValidationPolicy()
        self.detector = detector or RegexMarkdownContentSignalDetector(
            min_visual_description_chars=self.policy.min_visual_description_chars
        )

    def validate(
        self,
        source_pages: Sequence[PdfSourcePageStructure],
        final_markdown_by_page: Mapping[int, str],
    ) -> PdfContentValidationReport:
        pages = self._normalize_source_pages(source_pages)
        markdown_pages = self._normalize_markdown_pages(final_markdown_by_page)
        if not pages and not self.policy.allow_empty_source_pages:
            raise ValueError(
                "source_pages must not be empty unless allow_empty_source_pages is enabled"
            )

        source_page_numbers = {page.page_number for page in pages}
        markdown_page_numbers = set(markdown_pages)
        missing_markdown_pages = tuple(sorted(source_page_numbers - markdown_page_numbers))
        unexpected_markdown_pages = tuple(sorted(markdown_page_numbers - source_page_numbers))
        page_set_complete = not missing_markdown_pages and not unexpected_markdown_pages
        if not page_set_complete and not self.policy.allow_page_set_mismatch:
            raise ValueError(
                "source/Markdown page set mismatch: "
                f"missing={list(missing_markdown_pages)}, "
                f"unexpected={list(unexpected_markdown_pages)}"
            )

        blocking: list[PdfContentValidationIssue] = []
        warnings: list[PdfContentValidationIssue] = []
        page_results: list[PdfPageContentValidation] = []
        affected: dict[str, list[int]] = {"table": [], "image": [], "formula": []}

        source_table_count = 0
        output_table_count = 0
        source_image_count = 0
        high_image_coverage_page_count = 0
        output_image_reference_count = 0
        output_visual_description_count = 0
        source_formula_count = 0
        odl_formula_count = 0
        output_formula_count = 0

        for page in pages:
            output = self.detector.detect(markdown_pages.get(page.page_number, ""))
            if not isinstance(output, MarkdownPageContentSignals):
                raise TypeError("detector.detect must return MarkdownPageContentSignals")
            page_blocking: list[str] = []
            page_warnings: list[str] = []
            source_table_count += page.table_count
            output_table_count += output.table_count
            source_image_count += page.image_count
            output_image_reference_count += output.image_reference_count
            output_visual_description_count += output.visual_description_count
            source_formula_count += page.formula_count
            odl_formula_count += page.odl_formula_count
            output_formula_count += output.formula_count

            if output.table_count < page.table_count:
                self._add_issue(
                    code=(
                        "TABLE_OUTPUT_MISSING"
                        if output.table_count == 0
                        else "TABLE_OUTPUT_COUNT_SHORTFALL"
                    ),
                    category="table",
                    page_number=page.page_number,
                    severity=self.policy.missing_table_severity,
                    message="Final Markdown contains fewer valid tables than the source evidence.",
                    blocking=blocking,
                    warnings=warnings,
                    page_blocking=page_blocking,
                    page_warnings=page_warnings,
                    affected=affected,
                )
            elif page.table_matrices and self._unmatched_values(
                page.table_matrices,
                output.table_matrices,
            ):
                self._add_issue(
                    code="TABLE_CONTENT_MISMATCH",
                    category="table",
                    page_number=page.page_number,
                    severity=self.policy.missing_table_severity,
                    message=(
                        "Final table row/column shape or normalized cell values differ "
                        "from the source PDF table matrix."
                    ),
                    blocking=blocking,
                    warnings=warnings,
                    page_blocking=page_blocking,
                    page_warnings=page_warnings,
                    affected=affected,
                )

            high_image_coverage = (
                page.image_coverage_ratio >= self.policy.high_image_coverage_ratio
            )
            if high_image_coverage:
                high_image_coverage_page_count += 1
            has_source_image_evidence = page.image_count > 0 or high_image_coverage
            # A scanned page is commonly exposed by PDF internals as dozens or
            # hundreds of raster tiles.  Page fallback deliberately renders and
            # assesses that source page once, so requiring one Markdown artifact
            # per low-level image object would reject complete full-page OCR.
            # Non-raster-dominant pages still keep the per-asset advisory count;
            # the image asset policy separately enforces unresolved visual tasks.
            expected_image_representations = (
                1 if high_image_coverage else page.image_count
            )
            output_image_representations = (
                output.image_reference_count + output.visual_description_count
            )
            if (
                has_source_image_evidence
                and output_image_representations < expected_image_representations
            ):
                output_image_missing = output_image_representations == 0
                self._add_issue(
                    code=(
                        (
                            "HIGH_IMAGE_COVERAGE_OUTPUT_MISSING"
                            if high_image_coverage
                            else "IMAGE_OUTPUT_MISSING"
                        )
                        if output_image_missing
                        else (
                            "HIGH_IMAGE_COVERAGE_OUTPUT_COUNT_SHORTFALL"
                            if high_image_coverage
                            else "IMAGE_OUTPUT_COUNT_SHORTFALL"
                        )
                    ),
                    category="image",
                    page_number=page.page_number,
                    severity=(
                        self.policy.missing_high_coverage_image_severity
                        if high_image_coverage
                        else self.policy.missing_image_severity
                    ),
                    message=(
                        "Final Markdown contains fewer valid image references or explicit "
                        "visual descriptions than the source evidence."
                    ),
                    blocking=blocking,
                    warnings=warnings,
                    page_blocking=page_blocking,
                    page_warnings=page_warnings,
                    affected=affected,
                )

            source_formula_count_shortfall = output.formula_count < page.formula_count
            if source_formula_count_shortfall:
                self._add_issue(
                    code=(
                        "FORMULA_OUTPUT_MISSING"
                        if output.formula_count == 0
                        else "FORMULA_OUTPUT_COUNT_SHORTFALL"
                    ),
                    category="formula",
                    page_number=page.page_number,
                    severity=self.policy.missing_source_formula_severity,
                    message="Final Markdown contains fewer formulas than the source evidence.",
                    blocking=blocking,
                    warnings=warnings,
                    page_blocking=page_blocking,
                    page_warnings=page_warnings,
                    affected=affected,
                )
            elif output.formula_count < page.odl_formula_count:
                self._add_issue(
                    code=(
                        "ODL_FORMULA_OUTPUT_MISSING"
                        if output.formula_count == 0
                        else "ODL_FORMULA_OUTPUT_COUNT_SHORTFALL"
                    ),
                    category="formula",
                    page_number=page.page_number,
                    severity=self.policy.missing_odl_formula_severity,
                    message=(
                        "Final Markdown contains fewer formulas than the OpenDataLoader "
                        "formula evidence."
                    ),
                    blocking=blocking,
                    warnings=warnings,
                    page_blocking=page_blocking,
                    page_warnings=page_warnings,
                    affected=affected,
                )
            elif page.formula_signatures and self._unmatched_values(
                page.formula_signatures,
                output.formula_signatures,
            ):
                self._add_issue(
                    code="FORMULA_TOKEN_MISMATCH",
                    category="formula",
                    page_number=page.page_number,
                    severity=self.policy.missing_source_formula_severity,
                    message=(
                        "Final formula variables, numbers, operators, subscripts or formula "
                        "identifier differ from the source PDF formula signature."
                    ),
                    blocking=blocking,
                    warnings=warnings,
                    page_blocking=page_blocking,
                    page_warnings=page_warnings,
                    affected=affected,
                )

            page_results.append(
                PdfPageContentValidation(
                    page_number=page.page_number,
                    source_table_count=page.table_count,
                    output_table_count=output.table_count,
                    source_image_count=page.image_count,
                    source_image_coverage_ratio=page.image_coverage_ratio,
                    output_image_reference_count=output.image_reference_count,
                    output_visual_description_count=output.visual_description_count,
                    source_formula_count=page.formula_count,
                    odl_formula_count=page.odl_formula_count,
                    output_formula_count=output.formula_count,
                    blocking_codes=tuple(page_blocking),
                    warning_codes=tuple(page_warnings),
                )
            )

        return PdfContentValidationReport(
            structural_passed=not blocking,
            page_set_complete=page_set_complete,
            evaluated_page_count=len(pages),
            missing_markdown_pages=missing_markdown_pages,
            unexpected_markdown_pages=unexpected_markdown_pages,
            source_table_count=source_table_count,
            output_table_count=output_table_count,
            source_image_count=source_image_count,
            high_image_coverage_page_count=high_image_coverage_page_count,
            output_image_reference_count=output_image_reference_count,
            output_visual_description_count=output_visual_description_count,
            source_formula_count=source_formula_count,
            odl_formula_count=odl_formula_count,
            output_formula_count=output_formula_count,
            table_affected_pages=tuple(affected["table"]),
            image_affected_pages=tuple(affected["image"]),
            formula_affected_pages=tuple(affected["formula"]),
            blocking_issues=tuple(blocking),
            warnings=tuple(warnings),
            per_page=tuple(page_results),
            policy=self.policy,
        )

    @staticmethod
    def _normalize_source_pages(
        source_pages: Sequence[PdfSourcePageStructure],
    ) -> list[PdfSourcePageStructure]:
        normalized: dict[int, PdfSourcePageStructure] = {}
        for page in source_pages:
            if not isinstance(page, PdfSourcePageStructure):
                raise TypeError("source_pages must contain PdfSourcePageStructure values")
            if page.page_number in normalized:
                raise ValueError(f"duplicate source page_number: {page.page_number}")
            normalized[page.page_number] = page
        return [normalized[page_number] for page_number in sorted(normalized)]

    @staticmethod
    def _normalize_markdown_pages(markdown_pages: Mapping[int, str]) -> dict[int, str]:
        normalized: dict[int, str] = {}
        for page_number, markdown in markdown_pages.items():
            _require_native_positive_int("final_markdown_by_page key", page_number)
            if not isinstance(markdown, str):
                raise TypeError("final_markdown_by_page values must be strings")
            normalized[page_number] = markdown
        return normalized

    @staticmethod
    def _unmatched_values(
        expected: Sequence[object],
        actual: Sequence[object],
    ) -> tuple[object, ...]:
        """Return expected values not found one-to-one in the actual sequence."""

        remaining = list(actual)
        missing: list[object] = []
        for value in expected:
            try:
                index = remaining.index(value)
            except ValueError:
                missing.append(value)
            else:
                remaining.pop(index)
        return tuple(missing)

    @staticmethod
    def _add_issue(
        *,
        code: str,
        category: str,
        page_number: int,
        severity: PdfContentIssueSeverity,
        message: str,
        blocking: list[PdfContentValidationIssue],
        warnings: list[PdfContentValidationIssue],
        page_blocking: list[str],
        page_warnings: list[str],
        affected: dict[str, list[int]],
    ) -> None:
        issue = PdfContentValidationIssue(
            code=code,
            category=category,
            page_number=page_number,
            severity=severity,
            message=message,
        )
        if severity is PdfContentIssueSeverity.BLOCKING:
            blocking.append(issue)
            page_blocking.append(code)
        else:
            warnings.append(issue)
            page_warnings.append(code)
        if page_number not in affected[category]:
            affected[category].append(page_number)


def validate_pdf_content(
    source_pages: Sequence[PdfSourcePageStructure],
    final_markdown_by_page: Mapping[int, str],
    *,
    policy: PdfContentValidationPolicy | None = None,
    detector: MarkdownContentSignalDetector | None = None,
) -> PdfContentValidationReport:
    """Functional entry point for callers that do not need to retain a validator."""

    return PdfContentValidator(policy=policy, detector=detector).validate(
        source_pages,
        final_markdown_by_page,
    )


__all__ = [
    "FormulaSignature",
    "MarkdownContentSignalDetector",
    "MarkdownPageContentSignals",
    "PdfContentIssueSeverity",
    "PdfContentValidationIssue",
    "PdfContentValidationPolicy",
    "PdfContentValidationReport",
    "PdfContentValidator",
    "PdfPageContentValidation",
    "PdfSourcePageStructure",
    "RegexMarkdownContentSignalDetector",
    "TableMatrix",
    "formula_signature",
    "normalize_table_cell",
    "normalize_table_matrix",
    "validate_pdf_content",
]
