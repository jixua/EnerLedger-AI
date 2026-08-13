from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from html import unescape
from pathlib import Path
from typing import TypeAlias

try:  # PyMuPDF 1.24+ exposes the canonical ``pymupdf`` module name.
    import pymupdf
except ImportError:  # pragma: no cover - compatibility with the declared 1.23 floor
    import fitz as pymupdf


class PdfQualityStatus(StrEnum):
    """Document-level outcome of PDF parsing quality analysis."""

    PASSED = "PASSED"
    PAGE_COUNT_MISMATCH = "PAGE_COUNT_MISMATCH"
    PAGE_PROVENANCE_INVALID = "PAGE_PROVENANCE_INVALID"
    OCR_REQUIRED = "OCR_REQUIRED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"


@dataclass(frozen=True, slots=True)
class PdfOcrPageResult:
    """Optional OCR output for one original PDF page.

    Confidence is expressed on a normalized 0..1 scale. A missing confidence is
    deliberately treated as low confidence: OCR text without a quality signal must
    not silently make a quality report pass.
    """

    page_number: int
    text: str
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("OCR page_number must be greater than zero")
        if self.confidence is not None and (
            not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1
        ):
            raise ValueError("OCR confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class PdfQualityThresholds:
    """Thresholds recorded alongside every report for reproducibility."""

    min_effective_text_chars: int
    image_only_max_text_chars: int
    image_only_min_coverage_ratio: float
    min_ocr_confidence: float
    min_text_retention_ratio: float
    min_text_precision_ratio: float

    def to_dict(self) -> dict[str, int | float]:
        return {
            "min_effective_text_chars": self.min_effective_text_chars,
            "image_only_max_text_chars": self.image_only_max_text_chars,
            "image_only_min_coverage_ratio": self.image_only_min_coverage_ratio,
            "min_ocr_confidence": self.min_ocr_confidence,
            "min_text_retention_ratio": self.min_text_retention_ratio,
            "min_text_precision_ratio": self.min_text_precision_ratio,
        }


@dataclass(frozen=True, slots=True)
class PdfScanDetectionReport:
    """Source-only classification used before selecting a PDF parser backend."""

    page_count: int
    substantive_page_count: int
    scanned_page_numbers: tuple[int, ...]
    blank_page_numbers: tuple[int, ...]

    @property
    def scanned_page_count(self) -> int:
        return len(self.scanned_page_numbers)

    @property
    def is_scanned_document(self) -> bool:
        return bool(
            self.substantive_page_count
            and self.scanned_page_count == self.substantive_page_count
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "page_count": self.page_count,
            "substantive_page_count": self.substantive_page_count,
            "scanned_page_count": self.scanned_page_count,
            "scanned_page_numbers": list(self.scanned_page_numbers),
            "blank_page_numbers": list(self.blank_page_numbers),
            "is_scanned_document": self.is_scanned_document,
        }


@dataclass(frozen=True, slots=True)
class PdfPageQuality:
    page_number: int
    width: float
    height: float
    rotation: int
    orientation: str
    text_block_area: float
    text_area_ratio: float
    visible_text_area_ratio: float
    visible_text_char_count: int
    hidden_text_char_count: int
    image_bbox_area: float
    image_coverage_ratio: float
    image_count: int
    vector_drawing_count: int
    vector_segment_count: int
    vector_drawing_area: float
    vector_coverage_ratio: float
    markdown_image_reference_count: int
    pdf_text_char_count: int
    markdown_text_char_count: int
    ocr_text_char_count: int
    effective_text_char_count: int
    text_retention_ratio: float | None
    text_precision_ratio: float | None
    text_fidelity_output_char_count: int
    content_covered: bool
    markdown_section_present: bool
    is_image_only: bool
    ocr_required: bool
    ocr_applied: bool
    ocr_confidence: float | None
    low_confidence: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "orientation": self.orientation,
            "text_block_area": self.text_block_area,
            "text_area_ratio": self.text_area_ratio,
            "visible_text_area_ratio": self.visible_text_area_ratio,
            "visible_text_char_count": self.visible_text_char_count,
            "hidden_text_char_count": self.hidden_text_char_count,
            "image_bbox_area": self.image_bbox_area,
            "image_coverage_ratio": self.image_coverage_ratio,
            "image_count": self.image_count,
            "vector_drawing_count": self.vector_drawing_count,
            "vector_segment_count": self.vector_segment_count,
            "vector_drawing_area": self.vector_drawing_area,
            "vector_coverage_ratio": self.vector_coverage_ratio,
            "markdown_image_reference_count": self.markdown_image_reference_count,
            "pdf_text_char_count": self.pdf_text_char_count,
            "markdown_text_char_count": self.markdown_text_char_count,
            "ocr_text_char_count": self.ocr_text_char_count,
            "effective_text_char_count": self.effective_text_char_count,
            "text_retention_ratio": self.text_retention_ratio,
            "text_precision_ratio": self.text_precision_ratio,
            "text_fidelity_output_char_count": self.text_fidelity_output_char_count,
            "content_covered": self.content_covered,
            "markdown_section_present": self.markdown_section_present,
            "is_image_only": self.is_image_only,
            "ocr_required": self.ocr_required,
            "ocr_applied": self.ocr_applied,
            "ocr_confidence": self.ocr_confidence,
            "low_confidence": self.low_confidence,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class PdfQualityReport:
    status: PdfQualityStatus
    pdf_page_count: int
    markdown_page_count: int
    page_markers: tuple[int, ...]
    page_markers_valid: bool
    page_provenance_valid: bool
    text_coverage_ratio: float
    ocr_required_pages: tuple[int, ...]
    ocr_page_count: int
    low_confidence_pages: tuple[int, ...]
    warnings: tuple[str, ...]
    per_page: tuple[PdfPageQuality, ...]
    thresholds: PdfQualityThresholds

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable report without leaking Enum/dataclass objects."""

        return {
            "status": self.status.value,
            "pdf_page_count": self.pdf_page_count,
            "markdown_page_count": self.markdown_page_count,
            "page_markers": list(self.page_markers),
            "page_markers_valid": self.page_markers_valid,
            "page_provenance_valid": self.page_provenance_valid,
            "text_coverage_ratio": self.text_coverage_ratio,
            "ocr_required_pages": list(self.ocr_required_pages),
            "ocr_page_count": self.ocr_page_count,
            "low_confidence_pages": list(self.low_confidence_pages),
            "warnings": list(self.warnings),
            "per_page": [page.to_dict() for page in self.per_page],
            "thresholds": self.thresholds.to_dict(),
        }


_OcrMappingValue: TypeAlias = PdfOcrPageResult | Mapping[str, object]
OcrResults: TypeAlias = Mapping[int, _OcrMappingValue] | Iterable[PdfOcrPageResult]
_Rectangle: TypeAlias = tuple[float, float, float, float]


class PdfQualityAnalyzer:
    """Compare an OpenDataLoader Markdown result with its original PDF pages.

    ``text_coverage_ratio`` is the fraction of original pages whose *indexable output*
    body text reaches ``min_effective_text_chars``. Effective text is the best
    available text from the mapped OpenDataLoader Markdown section or optional OCR.
    The PDF text layer remains source-side evidence only: letting it count as output
    would incorrectly pass a page when OpenDataLoader emitted an image link but lost
    the actual body text.
    """

    _PAGE_MARKER_RE = re.compile(
        r"^[\t ]*<!--[\t ]*ODL_PAGE:(\d+)[\t ]*-->[\t ]*\r?$",
        flags=re.MULTILINE,
    )
    _MARKDOWN_IMAGE_RE = re.compile(
        r"!\[[^\]\r\n]*\]\(\s*(?:<[^>\r\n]*>|[^)\r\n]*)\s*\)",
        flags=re.IGNORECASE,
    )
    _MARKDOWN_REFERENCE_IMAGE_RE = re.compile(
        r"!\[[^\]\r\n]*\]\s*\[[^\]\r\n]*\]",
        flags=re.IGNORECASE,
    )
    _MARKDOWN_REFERENCE_DEFINITION_RE = re.compile(
        r"^[\t ]{0,3}\[[^\]\r\n]+\]:[\t ]*\S+[^\r\n]*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    _HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", flags=re.IGNORECASE | re.DOTALL)
    _HTML_COMMENT_RE = re.compile(r"<!--.*?-->", flags=re.DOTALL)
    _HTML_NON_CONTENT_RE = re.compile(
        r"<(?:script|style|noscript)\b[^>]*>.*?</(?:script|style|noscript)\s*>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    _HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>", flags=re.DOTALL)
    _CONTROLLED_VISION_SUPPLEMENT_RE = re.compile(
        r"^[\t ]*<!--[\t ]*PAGE_FALLBACK:VISION[\t ]*-->[\t ]*"
        r"(?:\r?\n)+[\t ]*图片说明：",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    _MARKDOWN_INLINE_LINK_RE = re.compile(
        r"(?<!!)\[(?P<label>[^\]\r\n]*)\]\(\s*(?:<[^>\r\n]+>|[^)\r\n]+)\s*\)"
    )
    _MARKDOWN_REFERENCE_LINK_RE = re.compile(
        r"(?<!!)\[(?P<label>[^\]\r\n]+)\]\s*\[[^\]\r\n]*\]"
    )
    _MARKDOWN_TABLE_DELIMITER_LINE_RE = re.compile(
        r"^[\t ]*\|?[\t ]*:?-{3,}:?(?:[\t ]*\|[\t ]*:?-{3,}:?)+[\t ]*\|?[\t ]*$",
        flags=re.MULTILINE,
    )
    _MARKDOWN_THEMATIC_BREAK_RE = re.compile(
        r"^[\t ]{0,3}(?:\*[\t ]*){3,}$|^[\t ]{0,3}(?:-[\t ]*){3,}$|"
        r"^[\t ]{0,3}(?:_[\t ]*){3,}$",
        flags=re.MULTILINE,
    )
    _MARKDOWN_LINE_PREFIX_RE = re.compile(
        r"^[\t ]{0,3}(?:#{1,6}[\t ]+|>[\t ]?|[-+*][\t ]+|\d+[.)][\t ]+)",
        flags=re.MULTILINE,
    )
    _MARKDOWN_EMPHASIS_RE = re.compile(
        r"(?<![\w])(?:\*{1,3}|_{1,3})(?=[\w])|"
        r"(?<=[\w])(?:\*{1,3}|_{1,3})(?![\w])"
    )
    _MAX_VISIBLE_TEXT_AREA_RATIO_FOR_SCAN = 0.02

    def __init__(
        self,
        *,
        min_effective_text_chars: int = 20,
        image_only_max_text_chars: int = 8,
        image_only_min_coverage_ratio: float = 0.6,
        min_ocr_confidence: float = 0.8,
        min_text_retention_ratio: float = 0.97,
        min_text_precision_ratio: float | None = None,
    ) -> None:
        if min_effective_text_chars < 1:
            raise ValueError("min_effective_text_chars must be greater than zero")
        if image_only_max_text_chars < 0:
            raise ValueError("image_only_max_text_chars must not be negative")
        if not 0 <= image_only_min_coverage_ratio <= 1:
            raise ValueError("image_only_min_coverage_ratio must be between 0 and 1")
        if not 0 <= min_ocr_confidence <= 1:
            raise ValueError("min_ocr_confidence must be between 0 and 1")
        if not 0 <= min_text_retention_ratio <= 1:
            raise ValueError("min_text_retention_ratio must be between 0 and 1")
        if min_text_precision_ratio is None:
            min_text_precision_ratio = min_text_retention_ratio
        if not 0 <= min_text_precision_ratio <= 1:
            raise ValueError("min_text_precision_ratio must be between 0 and 1")

        self.thresholds = PdfQualityThresholds(
            min_effective_text_chars=min_effective_text_chars,
            image_only_max_text_chars=image_only_max_text_chars,
            image_only_min_coverage_ratio=image_only_min_coverage_ratio,
            min_ocr_confidence=min_ocr_confidence,
            min_text_retention_ratio=min_text_retention_ratio,
            min_text_precision_ratio=min_text_precision_ratio,
        )

    def analyze(
        self,
        pdf_path: str | Path,
        markdown: str,
        *,
        ocr_results: OcrResults | None = None,
    ) -> PdfQualityReport:
        normalized_ocr, ocr_warnings = self._normalize_ocr_results(ocr_results)
        global_warnings = list(ocr_warnings)

        document = pymupdf.open(filename=str(pdf_path))
        try:
            pdf_page_count = document.page_count
            sections, page_markers, marker_warnings = self._split_markdown(markdown)
            global_warnings.extend(marker_warnings)

            expected_markers = list(range(1, pdf_page_count + 1))
            page_markers_valid = page_markers == expected_markers
            page_provenance_valid = (
                page_markers_valid and "CONTENT_BEFORE_FIRST_PAGE_MARKER" not in marker_warnings
            )
            if len(page_markers) != pdf_page_count:
                global_warnings.append(
                    "PAGE_MARKER_COUNT_MISMATCH:"
                    f"expected={pdf_page_count},actual={len(page_markers)}"
                )
            if page_markers != expected_markers:
                global_warnings.append(
                    "PAGE_MARKER_SEQUENCE_MISMATCH:"
                    f"expected={expected_markers},actual={page_markers}"
                )

            for page_number in sorted(normalized_ocr):
                if page_number > pdf_page_count:
                    global_warnings.append(f"OCR_RESULT_PAGE_OUT_OF_RANGE:page={page_number}")

            per_page: list[PdfPageQuality] = []
            for page_index in range(pdf_page_count):
                page_number = page_index + 1
                page = document.load_page(page_index)
                section = sections.get(page_number)
                ocr_result = normalized_ocr.get(page_number)
                page_quality = self._analyze_page(
                    page,
                    page_number=page_number,
                    markdown_section=section,
                    ocr_result=ocr_result,
                )
                per_page.append(page_quality)
                global_warnings.extend(
                    f"PAGE_{page_number}_{warning}" for warning in page_quality.warnings
                )
        finally:
            document.close()

        ocr_required_pages = tuple(page.page_number for page in per_page if page.ocr_required)
        ocr_page_count = sum(1 for page in per_page if page.ocr_applied)
        low_confidence_pages = tuple(page.page_number for page in per_page if page.low_confidence)
        covered_page_count = sum(page.content_covered for page in per_page)
        text_coverage_ratio = (
            round(covered_page_count / pdf_page_count, 6) if pdf_page_count else 1.0
        )

        if not page_markers_valid:
            status = PdfQualityStatus.PAGE_COUNT_MISMATCH
        elif not page_provenance_valid:
            status = PdfQualityStatus.PAGE_PROVENANCE_INVALID
        elif low_confidence_pages:
            status = PdfQualityStatus.LOW_CONFIDENCE
        elif ocr_required_pages:
            status = PdfQualityStatus.OCR_REQUIRED
        else:
            status = PdfQualityStatus.PASSED

        return PdfQualityReport(
            status=status,
            pdf_page_count=pdf_page_count,
            markdown_page_count=len(page_markers),
            page_markers=tuple(page_markers),
            page_markers_valid=page_markers_valid,
            page_provenance_valid=page_provenance_valid,
            text_coverage_ratio=text_coverage_ratio,
            ocr_required_pages=ocr_required_pages,
            ocr_page_count=ocr_page_count,
            low_confidence_pages=low_confidence_pages,
            warnings=tuple(dict.fromkeys(global_warnings)),
            per_page=tuple(per_page),
            thresholds=self.thresholds,
        )

    def detect_scanned_document(
        self,
        pdf_path: str | Path,
        *,
        max_pages: int | None = None,
    ) -> PdfScanDetectionReport:
        """Classify a wholly scanned PDF before any parser backend is invoked.

        The page decision intentionally reuses ``_analyze_page`` so backend routing
        and the post-parse quality gate cannot disagree about raster-dominant pages,
        hidden OCR layers, or the configured image coverage threshold. Truly blank
        pages do not prevent an otherwise scanned document from using MinerU.
        """

        document = pymupdf.open(filename=str(pdf_path))
        try:
            page_count = document.page_count
            if max_pages is not None and page_count > max_pages:
                raise ValueError(
                    "PDF scan detection page limit exceeded: "
                    f"actual={page_count},limit={max_pages}"
                )
            scanned_pages: list[int] = []
            blank_pages: list[int] = []
            substantive_page_count = 0
            for page_index in range(page_count):
                page_number = page_index + 1
                page_quality = self._analyze_page(
                    document.load_page(page_index),
                    page_number=page_number,
                    markdown_section="",
                    ocr_result=None,
                )
                substantive = bool(
                    page_quality.image_count
                    or page_quality.pdf_text_char_count
                    or page_quality.vector_drawing_count
                )
                if not substantive:
                    blank_pages.append(page_number)
                    continue
                substantive_page_count += 1
                if page_quality.is_image_only:
                    scanned_pages.append(page_number)
        finally:
            document.close()

        return PdfScanDetectionReport(
            page_count=page_count,
            substantive_page_count=substantive_page_count,
            scanned_page_numbers=tuple(scanned_pages),
            blank_page_numbers=tuple(blank_pages),
        )

    def _analyze_page(
        self,
        page: pymupdf.Page,
        *,
        page_number: int,
        markdown_section: str | None,
        ocr_result: PdfOcrPageResult | None,
    ) -> PdfPageQuality:
        display_rect = page.rect
        coordinate_bounds = (0.0, 0.0, float(page.cropbox.width), float(page.cropbox.height))
        page_area = coordinate_bounds[2] * coordinate_bounds[3]

        text_rectangles: list[_Rectangle] = []
        pdf_text_parts: list[str] = []
        for block in page.get_text("blocks"):
            if len(block) < 5:
                continue
            if len(block) >= 7 and block[6] != 0:
                continue
            text = str(block[4] or "")
            if not text.strip():
                continue
            pdf_text_parts.append(text)
            rectangle = self._clip_rectangle(block[:4], coordinate_bounds)
            if rectangle is not None:
                text_rectangles.append(rectangle)

        image_rectangles = self._image_rectangles(page, coordinate_bounds)
        vector_rectangles, vector_segment_count = self._vector_drawing_geometry(
            page,
            coordinate_bounds,
        )
        text_block_area = self._rectangle_union_area(text_rectangles)
        (
            visible_text_rectangles,
            visible_pdf_text,
            hidden_pdf_text,
        ) = self._visible_text_geometry(
            page,
            coordinate_bounds,
            fallback_rectangles=text_rectangles,
            fallback_text="\n".join(pdf_text_parts),
        )
        visible_text_area = self._rectangle_union_area(visible_text_rectangles)
        image_bbox_area = self._rectangle_union_area(image_rectangles)
        vector_drawing_area = self._rectangle_union_area(vector_rectangles)
        text_area_ratio = text_block_area / page_area if page_area else 0.0
        visible_text_area_ratio = visible_text_area / page_area if page_area else 0.0
        image_coverage_ratio = image_bbox_area / page_area if page_area else 0.0
        vector_coverage_ratio = vector_drawing_area / page_area if page_area else 0.0

        pdf_text_char_count = self._effective_character_count("\n".join(pdf_text_parts))
        visible_text_char_count = self._effective_character_count(visible_pdf_text)
        hidden_text_char_count = self._effective_character_count(hidden_pdf_text)
        markdown_text_char_count = self._effective_character_count(markdown_section or "")
        markdown_image_reference_count = self._image_reference_count(markdown_section or "")
        ocr_text_char_count = self._effective_character_count(
            ocr_result.text if ocr_result is not None else ""
        )
        effective_text_char_count = max(markdown_text_char_count, ocr_text_char_count)
        # PAGE_FALLBACK:VISION is an explicitly delimited, project-generated visual
        # supplement.  It can legitimately add chart relationships that do not
        # exist in the PDF text layer, so it must not lower (or artificially raise)
        # the fidelity score of the original textual body.
        fidelity_markdown = self._without_controlled_vision_supplement(
            markdown_section or ""
        )
        markdown_text_normalized = self._normalized_effective_text(fidelity_markdown)
        ocr_text_normalized = self._normalized_effective_text(
            ocr_result.text if ocr_result is not None else ""
        )

        has_image_evidence = bool(image_rectangles or markdown_image_reference_count)
        output_has_only_image_references = bool(
            markdown_image_reference_count > 0
            and markdown_text_char_count <= self.thresholds.image_only_max_text_chars
        )
        raster_dominant = bool(
            image_rectangles
            and image_coverage_ratio >= self.thresholds.image_only_min_coverage_ratio
        )
        hidden_text_layer = bool(raster_dominant and hidden_text_char_count > 0)
        low_visible_text_area = bool(
            raster_dominant
            and visible_text_area_ratio <= self._MAX_VISIBLE_TEXT_AREA_RATIO_FOR_SCAN
        )
        source_is_scanned_image = bool(
            raster_dominant
            and (
                visible_text_char_count <= self.thresholds.image_only_max_text_chars
                or hidden_text_layer
                or low_visible_text_area
            )
        )
        # An invisible OCR layer is not authoritative source text.  Once the page is
        # classified as a scan, only genuinely visible PDF text may participate in
        # retention; the independent page OCR result remains the required output.
        retention_source_text = (
            visible_pdf_text if source_is_scanned_image else "\n".join(pdf_text_parts)
        )
        pdf_text_normalized = self._normalized_effective_text(retention_source_text)
        (
            text_retention_ratio,
            text_precision_ratio,
            text_fidelity_output_char_count,
        ) = self._best_text_fidelity(
            pdf_text_normalized,
            markdown_text_normalized,
            ocr_text_normalized,
        )
        is_image_only = output_has_only_image_references or source_is_scanned_image
        ocr_applied = ocr_result is not None
        source_has_text = (
            visible_text_char_count > 0 if source_is_scanned_image else pdf_text_char_count > 0
        )
        source_has_meaningful_content = bool(has_image_evidence or source_has_text)
        text_retention_insufficient = bool(
            source_has_text
            and (
                text_retention_ratio is None
                or text_retention_ratio < self.thresholds.min_text_retention_ratio
            )
        )
        text_precision_insufficient = bool(
            source_has_text
            and (
                text_precision_ratio is None
                or text_precision_ratio < self.thresholds.min_text_precision_ratio
            )
        )
        text_fidelity_insufficient = bool(
            text_retention_insufficient or text_precision_insufficient
        )
        scan_ocr_incomplete = bool(
            source_is_scanned_image
            and (
                ocr_result is None
                or ocr_text_char_count < self.thresholds.min_effective_text_chars
            )
        )
        # 原始页面有正文/图片证据而 ODL/OCR 的最终可索引正文不足时必须补齐。
        # 这同时覆盖“ODL 只有图片链接”和“ODL 丢失 born-digital 文本”两类情况；
        # 真正没有正文也没有图片的空白页不会被无意义地送入 OCR。
        ocr_required = bool(
            scan_ocr_incomplete
            or text_fidelity_insufficient
            or (
                has_image_evidence
                and effective_text_char_count < self.thresholds.min_effective_text_chars
            )
        )
        low_confidence = bool(
            ocr_result is not None
            and (
                ocr_result.confidence is None
                or ocr_result.confidence < self.thresholds.min_ocr_confidence
            )
        )

        warnings: list[str] = []
        if markdown_section is None:
            warnings.append("MARKDOWN_SECTION_MISSING")
        if (
            source_has_meaningful_content
            and (
                markdown_text_char_count < min(
                    self.thresholds.min_effective_text_chars,
                    max(1, pdf_text_char_count),
                )
                or text_fidelity_insufficient
            )
        ):
            warnings.append("ODL_TEXT_INSUFFICIENT")
        if text_retention_insufficient:
            warnings.append("TEXT_RETENTION_LOW")
        if text_precision_insufficient:
            warnings.append("TEXT_PRECISION_LOW")
        if is_image_only:
            warnings.append("IMAGE_ONLY")
        if hidden_text_layer:
            warnings.append("HIDDEN_TEXT_LAYER")
        if low_visible_text_area and visible_text_char_count > 0:
            warnings.append("RASTER_DOMINANT_LOW_VISIBLE_TEXT_AREA")
        if ocr_applied and ocr_text_char_count < self.thresholds.min_effective_text_chars:
            warnings.append("OCR_TEXT_INSUFFICIENT")
        if ocr_result is not None and ocr_result.confidence is None:
            warnings.append("OCR_CONFIDENCE_MISSING")
        elif low_confidence:
            warnings.append("OCR_LOW_CONFIDENCE")
        if ocr_required:
            warnings.append("OCR_REQUIRED")

        if source_has_text:
            content_covered = bool(
                text_retention_ratio is not None
                and text_retention_ratio >= self.thresholds.min_text_retention_ratio
                and text_precision_ratio is not None
                and text_precision_ratio >= self.thresholds.min_text_precision_ratio
                and effective_text_char_count > 0
            )
        elif has_image_evidence:
            content_covered = bool(
                ocr_result is not None
                and ocr_text_char_count >= self.thresholds.min_effective_text_chars
            )
        else:
            # A genuinely blank source page is covered by its valid page marker.
            content_covered = True

        rotation = int(page.rotation or 0) % 360
        width = float(display_rect.width)
        height = float(display_rect.height)
        if math.isclose(width, height, rel_tol=0.0, abs_tol=0.01):
            orientation = "square"
        elif width > height:
            orientation = "landscape"
        else:
            orientation = "portrait"

        return PdfPageQuality(
            page_number=page_number,
            width=round(width, 3),
            height=round(height, 3),
            rotation=rotation,
            orientation=orientation,
            text_block_area=round(text_block_area, 3),
            text_area_ratio=round(text_area_ratio, 6),
            visible_text_area_ratio=round(visible_text_area_ratio, 6),
            visible_text_char_count=visible_text_char_count,
            hidden_text_char_count=hidden_text_char_count,
            image_bbox_area=round(image_bbox_area, 3),
            image_coverage_ratio=round(image_coverage_ratio, 6),
            image_count=len(image_rectangles),
            vector_drawing_count=len(vector_rectangles),
            vector_segment_count=vector_segment_count,
            vector_drawing_area=round(vector_drawing_area, 3),
            vector_coverage_ratio=round(vector_coverage_ratio, 6),
            markdown_image_reference_count=markdown_image_reference_count,
            pdf_text_char_count=pdf_text_char_count,
            markdown_text_char_count=markdown_text_char_count,
            ocr_text_char_count=ocr_text_char_count,
            effective_text_char_count=effective_text_char_count,
            text_retention_ratio=(
                round(text_retention_ratio, 6) if text_retention_ratio is not None else None
            ),
            text_precision_ratio=(
                round(text_precision_ratio, 6) if text_precision_ratio is not None else None
            ),
            text_fidelity_output_char_count=text_fidelity_output_char_count,
            content_covered=content_covered,
            markdown_section_present=markdown_section is not None,
            is_image_only=is_image_only,
            ocr_required=ocr_required,
            ocr_applied=ocr_applied,
            ocr_confidence=ocr_result.confidence if ocr_result is not None else None,
            low_confidence=low_confidence,
            warnings=tuple(warnings),
        )

    def _split_markdown(
        self,
        markdown: str,
    ) -> tuple[dict[int, str], list[int], list[str]]:
        matches = list(self._PAGE_MARKER_RE.finditer(markdown or ""))
        page_markers = [int(match.group(1)) for match in matches]
        warnings: list[str] = []
        sections: dict[int, str] = {}

        if not matches:
            warnings.append("PAGE_MARKERS_MISSING")
            return sections, page_markers, warnings

        if markdown[: matches[0].start()].strip():
            warnings.append("CONTENT_BEFORE_FIRST_PAGE_MARKER")

        for index, match in enumerate(matches):
            page_number = int(match.group(1))
            section_end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
            section = markdown[match.end() : section_end]
            if page_number in sections:
                warnings.append(f"DUPLICATE_PAGE_MARKER:page={page_number}")
                sections[page_number] = f"{sections[page_number]}\n{section}"
            else:
                sections[page_number] = section

        return sections, page_markers, warnings

    def _normalize_ocr_results(
        self,
        ocr_results: OcrResults | None,
    ) -> tuple[dict[int, PdfOcrPageResult], list[str]]:
        if ocr_results is None:
            return {}, []

        normalized: dict[int, PdfOcrPageResult] = {}
        warnings: list[str] = []
        if isinstance(ocr_results, Mapping):
            items: Iterable[tuple[int, _OcrMappingValue]] = ocr_results.items()
            for raw_page_number, raw_result in items:
                page_number = int(raw_page_number)
                if isinstance(raw_result, PdfOcrPageResult):
                    result = raw_result
                    if result.page_number != page_number:
                        warnings.append(
                            "OCR_RESULT_PAGE_NUMBER_MISMATCH:"
                            f"key={page_number},value={result.page_number}"
                        )
                        result = PdfOcrPageResult(
                            page_number=page_number,
                            text=result.text,
                            confidence=result.confidence,
                        )
                elif isinstance(raw_result, Mapping):
                    confidence_value = raw_result.get("confidence")
                    result = PdfOcrPageResult(
                        page_number=page_number,
                        text=str(raw_result.get("text") or ""),
                        confidence=(
                            float(confidence_value) if confidence_value is not None else None
                        ),
                    )
                else:
                    raise TypeError("OCR mapping values must be PdfOcrPageResult or a mapping")
                if page_number in normalized:
                    warnings.append(f"DUPLICATE_OCR_RESULT:page={page_number}")
                normalized[page_number] = result
            return normalized, warnings

        for result in ocr_results:
            if not isinstance(result, PdfOcrPageResult):
                raise TypeError("OCR iterable values must be PdfOcrPageResult instances")
            if result.page_number in normalized:
                warnings.append(f"DUPLICATE_OCR_RESULT:page={result.page_number}")
            normalized[result.page_number] = result
        return normalized, warnings

    @classmethod
    def _effective_character_count(cls, text: str) -> int:
        return len(cls._normalized_effective_text(text))

    @classmethod
    def _normalized_effective_text(cls, text: str) -> str:
        without_page_markers = cls._PAGE_MARKER_RE.sub("", text or "")
        without_comments = cls._HTML_COMMENT_RE.sub("", without_page_markers)
        without_non_content = cls._HTML_NON_CONTENT_RE.sub("", without_comments)
        without_html_images = cls._HTML_IMAGE_RE.sub("", without_non_content)
        without_markdown_images = cls._MARKDOWN_IMAGE_RE.sub("", without_html_images)
        without_reference_images = cls._MARKDOWN_REFERENCE_IMAGE_RE.sub("", without_markdown_images)
        without_reference_definitions = cls._MARKDOWN_REFERENCE_DEFINITION_RE.sub(
            "", without_reference_images
        )
        without_inline_link_targets = cls._MARKDOWN_INLINE_LINK_RE.sub(
            lambda match: match.group("label"),
            without_reference_definitions,
        )
        without_reference_link_targets = cls._MARKDOWN_REFERENCE_LINK_RE.sub(
            lambda match: match.group("label"),
            without_inline_link_targets,
        )
        without_html_tags = cls._HTML_TAG_RE.sub("", without_reference_link_targets)
        without_table_delimiters = cls._MARKDOWN_TABLE_DELIMITER_LINE_RE.sub(
            "", without_html_tags
        )
        without_thematic_breaks = cls._MARKDOWN_THEMATIC_BREAK_RE.sub(
            "", without_table_delimiters
        )
        without_line_prefixes = cls._MARKDOWN_LINE_PREFIX_RE.sub(
            "", without_thematic_breaks
        )
        without_emphasis = cls._MARKDOWN_EMPHASIS_RE.sub("", without_line_prefixes)
        plain_text = unescape(without_emphasis.replace("`", "").replace("~~", ""))
        latex_operators = {
            r"\times": "×",
            r"\cdot": "×",
            r"\div": "÷",
            r"\leq": "≤",
            r"\le": "≤",
            r"\geq": "≥",
            r"\ge": "≥",
            r"\neq": "≠",
            r"\pm": "±",
        }
        for source, target in latex_operators.items():
            plain_text = plain_text.replace(source, target)
        plain_text = plain_text.replace("·", "×").replace("*", "×")
        retained_symbols = "=≈≠≤≥±×÷∑∫√+-*/^%"
        return "".join(
            unicodedata.normalize("NFKC", character).casefold()
            for character in plain_text
            if character.isalnum() or character in retained_symbols
        )

    @classmethod
    def _without_controlled_vision_supplement(cls, text: str) -> str:
        """Exclude only the controlled visual appendix from text fidelity scoring.

        The fallback writer always emits both the exact marker and ``图片说明：``
        prefix.  A bare marker is intentionally not trusted, otherwise arbitrary
        extra text could hide behind a forged comment and bypass precision checks.
        """

        match = cls._CONTROLLED_VISION_SUPPLEMENT_RE.search(text or "")
        return text[: match.start()] if match is not None else text

    @staticmethod
    def _best_text_fidelity(
        source: str,
        *outputs: str,
    ) -> tuple[float | None, float | None, int]:
        """Return recall and precision from the same best output candidate.

        Recall alone accepts ``source + hallucination`` and duplicated bodies.  The
        paired precision denominator makes either form of added text fail, while
        selecting a single candidate prevents recall from Markdown and precision
        from OCR being combined into a synthetic pass.
        """

        if not source:
            return None, None, 0
        best_recall = 0.0
        best_precision = 0.0
        best_output_length = 0
        best_score = (-1.0, -1.0, -1.0, -1.0)
        for output in outputs:
            if not output:
                continue
            if source in output:
                matched = len(source)
            else:
                matched = sum(
                    block.size
                    for block in SequenceMatcher(
                        None,
                        source,
                        output,
                        # Large pages can contain tens of thousands of repeated CJK
                        # characters.  SequenceMatcher's popularity filter bounds
                        # the pathological case; the contiguous fast path above
                        # remains exact for unmodified source text.
                        autojunk=max(len(source), len(output)) > 4_000,
                    ).get_matching_blocks()
                )
            recall = matched / len(source)
            precision = matched / len(output)
            harmonic_mean = (
                2 * recall * precision / (recall + precision)
                if recall + precision
                else 0.0
            )
            score = (min(recall, precision), harmonic_mean, recall, precision)
            if score > best_score:
                best_score = score
                best_recall = recall
                best_precision = precision
                best_output_length = len(output)
        return best_recall, best_precision, best_output_length

    @classmethod
    def _visible_text_geometry(
        cls,
        page: pymupdf.Page,
        coordinate_bounds: _Rectangle,
        *,
        fallback_rectangles: Sequence[_Rectangle],
        fallback_text: str,
    ) -> tuple[list[_Rectangle], str, str]:
        """Return visible text geometry and split visible/invisible text content.

        PyMuPDF ``get_text()`` intentionally exposes render-mode-3 OCR layers.  They
        are searchable but invisible, so treating them as born-digital text lets a
        full-page scan bypass real OCR.  Text trace type 3 (or zero opacity) is kept
        as diagnostic evidence but excluded from visible geometry.
        """

        try:
            traces = page.get_texttrace()
        except (AttributeError, RuntimeError, ValueError):
            return list(fallback_rectangles), fallback_text, ""
        if not traces:
            return list(fallback_rectangles), fallback_text, ""

        visible_rectangles: list[_Rectangle] = []
        visible_parts: list[str] = []
        hidden_parts: list[str] = []
        for trace in traces:
            characters = "".join(
                chr(int(character[0]))
                for character in (trace.get("chars") or ())
                if character and isinstance(character[0], int) and character[0] > 0
            )
            text_is_hidden = bool(
                int(trace.get("type", 0) or 0) == 3
                or float(trace.get("opacity", 1.0) or 0.0) <= 0.01
            )
            if text_is_hidden:
                hidden_parts.append(characters)
                continue
            visible_parts.append(characters)
            rectangle = cls._clip_rectangle(trace.get("bbox", ()), coordinate_bounds)
            if rectangle is not None:
                visible_rectangles.append(rectangle)
        return visible_rectangles, "\n".join(visible_parts), "\n".join(hidden_parts)

    @classmethod
    def _image_reference_count(cls, text: str) -> int:
        return (
            len(cls._MARKDOWN_IMAGE_RE.findall(text or ""))
            + len(cls._MARKDOWN_REFERENCE_IMAGE_RE.findall(text or ""))
            + len(cls._HTML_IMAGE_RE.findall(text or ""))
        )

    @classmethod
    def _image_rectangles(
        cls,
        page: pymupdf.Page,
        coordinate_bounds: _Rectangle,
    ) -> list[_Rectangle]:
        rectangles: list[_Rectangle] = []
        try:
            image_info = page.get_image_info(xrefs=True)
        except (AttributeError, RuntimeError):  # pragma: no cover - legacy fallback
            image_info = []

        for image in image_info:
            rectangle = cls._clip_rectangle(image.get("bbox", ()), coordinate_bounds)
            if rectangle is not None:
                rectangles.append(rectangle)

        if rectangles:
            return rectangles

        for image in page.get_images(full=True):  # pragma: no cover - legacy fallback
            xref = image[0]
            for bbox in page.get_image_rects(xref):
                rectangle = cls._clip_rectangle(bbox, coordinate_bounds)
                if rectangle is not None:
                    rectangles.append(rectangle)
        return rectangles

    @classmethod
    def _vector_drawing_geometry(
        cls,
        page: pymupdf.Page,
        coordinate_bounds: _Rectangle,
    ) -> tuple[list[_Rectangle], int]:
        rectangles: list[_Rectangle] = []
        segment_count = 0
        try:
            drawings = page.get_drawings()
        except (AttributeError, RuntimeError, ValueError):
            return rectangles, segment_count
        for drawing in drawings:
            items = drawing.get("items") or ()
            segment_count += len(items)
            rectangle = cls._clip_rectangle(drawing.get("rect", ()), coordinate_bounds)
            if rectangle is not None:
                rectangles.append(rectangle)
        return rectangles, segment_count

    @staticmethod
    def _clip_rectangle(
        raw_rectangle: Sequence[float],
        bounds: _Rectangle,
    ) -> _Rectangle | None:
        if len(raw_rectangle) < 4:
            return None
        try:
            raw_x0, raw_y0, raw_x1, raw_y1 = (float(raw_rectangle[index]) for index in range(4))
        except (TypeError, ValueError, OverflowError):
            return None
        if not all(math.isfinite(value) for value in (raw_x0, raw_y0, raw_x1, raw_y1)):
            return None

        x0 = max(bounds[0], min(raw_x0, raw_x1))
        y0 = max(bounds[1], min(raw_y0, raw_y1))
        x1 = min(bounds[2], max(raw_x0, raw_x1))
        y1 = min(bounds[3], max(raw_y0, raw_y1))
        if x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1, y1

    @staticmethod
    def _rectangle_union_area(rectangles: Sequence[_Rectangle]) -> float:
        """Return the exact union area so overlapping blocks/images are not double-counted."""

        if not rectangles:
            return 0.0
        x_coordinates = sorted({value for rectangle in rectangles for value in rectangle[::2]})
        area = 0.0
        for left, right in zip(x_coordinates, x_coordinates[1:], strict=False):
            if right <= left:
                continue
            y_intervals = sorted(
                (rectangle[1], rectangle[3])
                for rectangle in rectangles
                if rectangle[0] < right and rectangle[2] > left
            )
            if not y_intervals:
                continue
            covered_y = 0.0
            current_start, current_end = y_intervals[0]
            for start, end in y_intervals[1:]:
                if start > current_end:
                    covered_y += current_end - current_start
                    current_start, current_end = start, end
                else:
                    current_end = max(current_end, end)
            covered_y += current_end - current_start
            area += (right - left) * covered_y
        return area


__all__ = [
    "OcrResults",
    "PdfOcrPageResult",
    "PdfPageQuality",
    "PdfQualityAnalyzer",
    "PdfQualityReport",
    "PdfScanDetectionReport",
    "PdfQualityStatus",
    "PdfQualityThresholds",
]
