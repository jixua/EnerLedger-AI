from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest

from app.rag.core.parser.pdf.quality import (
    PdfOcrPageResult,
    PdfQualityAnalyzer,
    PdfQualityStatus,
)


def _png_bytes() -> bytes:
    pixmap = pymupdf.Pixmap(
        pymupdf.csRGB,
        pymupdf.IRect(0, 0, 8, 8),
        False,
    )
    pixmap.clear_with(0xDDEEFF)
    return pixmap.tobytes("png")


def _write_text_pdf(path: Path) -> None:
    document = pymupdf.open()
    first_page = document.new_page(width=200, height=300)
    first_page.insert_textbox(
        pymupdf.Rect(20, 20, 180, 120),
        "Carbon accounting source text for the first original PDF page.",
        fontsize=10,
    )

    second_page = document.new_page(width=200, height=300)
    second_page.insert_image(second_page.rect, stream=_png_bytes())
    second_page.insert_textbox(
        pymupdf.Rect(20, 20, 180, 120),
        "Energy consumption and emission factor text for the second page.",
        fontsize=10,
    )
    second_page.set_rotation(90)
    document.save(path)
    document.close()


def _write_image_only_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=240, height=320)
    page.insert_image(page.rect, stream=_png_bytes())
    document.save(path)
    document.close()


def _write_mixed_text_and_scan_pdf(path: Path) -> None:
    document = pymupdf.open()
    text_page = document.new_page(width=240, height=320)
    text_page.insert_text(
        (20, 40),
        "Born digital carbon accounting content must stay on the local parser.",
        fontsize=9,
    )
    scan_page = document.new_page(width=240, height=320)
    scan_page.insert_image(scan_page.rect, stream=_png_bytes())
    document.save(path)
    document.close()


def _write_scan_pdf_with_blank_page(path: Path) -> None:
    document = pymupdf.open()
    scan_page = document.new_page(width=240, height=320)
    scan_page.insert_image(scan_page.rect, stream=_png_bytes())
    document.new_page(width=240, height=320)
    document.save(path)
    document.close()


def _write_raster_pdf_with_text_layer(
    path: Path,
    text: str,
    *,
    hidden: bool,
) -> None:
    document = pymupdf.open()
    page = document.new_page(width=240, height=320)
    page.insert_image(page.rect, stream=_png_bytes())
    page.insert_text(
        (8, 12),
        text,
        fontsize=3 if not hidden else 10,
        render_mode=3 if hidden else 0,
    )
    document.save(path)
    document.close()


def _write_single_text_pdf(path: Path, text: str) -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    y = 40
    for line in text.splitlines() or [text]:
        page.insert_text((30, y), line, fontsize=9)
        y += 14
    document.save(path)
    document.close()


def _analyzer() -> PdfQualityAnalyzer:
    return PdfQualityAnalyzer(
        min_effective_text_chars=10,
        image_only_max_text_chars=3,
        image_only_min_coverage_ratio=0.8,
        min_ocr_confidence=0.75,
    )


def test_scan_detection_classifies_image_only_document(tmp_path: Path) -> None:
    pdf_path = tmp_path / "scan.pdf"
    _write_image_only_pdf(pdf_path)

    report = _analyzer().detect_scanned_document(pdf_path)

    assert report.is_scanned_document is True
    assert report.page_count == 1
    assert report.substantive_page_count == 1
    assert report.scanned_page_numbers == (1,)
    assert report.blank_page_numbers == ()


def test_scan_detection_classifies_raster_pdf_with_hidden_text_layer(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "scan-with-hidden-ocr.pdf"
    _write_raster_pdf_with_text_layer(
        pdf_path,
        "hidden searchable OCR layer",
        hidden=True,
    )

    report = _analyzer().detect_scanned_document(pdf_path)

    assert report.is_scanned_document is True
    assert report.scanned_page_numbers == (1,)


def test_scan_detection_does_not_classify_mixed_document(tmp_path: Path) -> None:
    pdf_path = tmp_path / "mixed.pdf"
    _write_mixed_text_and_scan_pdf(pdf_path)

    report = _analyzer().detect_scanned_document(pdf_path)

    assert report.is_scanned_document is False
    assert report.substantive_page_count == 2
    assert report.scanned_page_numbers == (2,)


def test_scan_detection_ignores_blank_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "scan-with-blank.pdf"
    _write_scan_pdf_with_blank_page(pdf_path)

    report = _analyzer().detect_scanned_document(pdf_path)

    assert report.is_scanned_document is True
    assert report.substantive_page_count == 1
    assert report.scanned_page_numbers == (1,)
    assert report.blank_page_numbers == (2,)


def test_scan_detection_rejects_page_count_above_preflight_limit(tmp_path: Path) -> None:
    pdf_path = tmp_path / "two-pages.pdf"
    _write_mixed_text_and_scan_pdf(pdf_path)

    with pytest.raises(ValueError, match="page limit exceeded"):
        _analyzer().detect_scanned_document(pdf_path, max_pages=1)


def test_analyze_collects_page_geometry_and_accepts_strict_markers(tmp_path: Path) -> None:
    pdf_path = tmp_path / "two-pages.pdf"
    _write_text_pdf(pdf_path)
    markdown = (
        "<!-- ODL_PAGE:1 -->\r\n"
        "Carbon accounting source text for the first original PDF page.\r\n"
        "![diagram](https://assets.example/diagram.png)\r\n"
        "<!-- ODL_PAGE:2 -->\r\n"
        "Energy consumption and emission factor text for the second page.\r\n"
        '<img src="https://assets.example/table.png" alt="table text">\r\n'
    )

    report = _analyzer().analyze(pdf_path, markdown)

    assert report.status is PdfQualityStatus.PASSED
    assert report.pdf_page_count == 2
    assert report.markdown_page_count == 2
    assert report.page_markers == (1, 2)
    assert report.page_markers_valid is True
    assert report.text_coverage_ratio == 1.0
    assert report.ocr_page_count == 0
    assert report.low_confidence_pages == ()

    first_page, second_page = report.per_page
    assert first_page.rotation == 0
    assert first_page.orientation == "portrait"
    assert first_page.text_block_area > 0
    assert first_page.text_area_ratio > 0
    assert first_page.image_coverage_ratio == 0

    assert second_page.rotation == 90
    assert second_page.orientation == "landscape"
    assert second_page.width > second_page.height
    assert second_page.text_block_area > 0
    assert second_page.image_bbox_area == pytest.approx(200 * 300)
    assert second_page.image_coverage_ratio == pytest.approx(1.0)
    assert second_page.is_image_only is False


@pytest.mark.parametrize(
    ("markdown", "expected_markers"),
    [
        (
            "<!-- ODL_PAGE:2 -->\nsecond page\n<!-- ODL_PAGE:1 -->\nfirst page",
            (2, 1),
        ),
        ("<!-- ODL_PAGE:1 -->\nonly one mapped page", (1,)),
    ],
)
def test_page_marker_count_and_order_are_strictly_validated(
    tmp_path: Path,
    markdown: str,
    expected_markers: tuple[int, ...],
) -> None:
    pdf_path = tmp_path / "two-pages.pdf"
    _write_text_pdf(pdf_path)

    report = _analyzer().analyze(pdf_path, markdown)

    assert report.status is PdfQualityStatus.PAGE_COUNT_MISMATCH
    assert report.page_markers == expected_markers
    assert report.page_markers_valid is False


def test_unmapped_preamble_is_reported_without_changing_valid_marker_sequence(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "two-pages.pdf"
    _write_text_pdf(pdf_path)
    markdown = (
        "unmapped document heading\n"
        "<!-- ODL_PAGE:1 -->\n"
        "Carbon accounting source text for the first original PDF page.\n"
        "<!-- ODL_PAGE:2 -->\n"
        "Energy consumption and emission factor text for the second page."
    )

    report = _analyzer().analyze(pdf_path, markdown)

    assert report.status is PdfQualityStatus.PAGE_PROVENANCE_INVALID
    assert report.page_markers_valid is True
    assert report.page_provenance_valid is False
    assert "CONTENT_BEFORE_FIRST_PAGE_MARKER" in report.warnings


def test_image_references_do_not_count_as_body_and_image_only_requires_ocr(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "scan.pdf"
    _write_image_only_pdf(pdf_path)
    markdown = (
        "<!-- ODL_PAGE:1 -->\n"
        "![descriptive alternate text](https://assets.example/full-page.png)\n"
        '<img src="https://assets.example/duplicate.png" alt="more alternate text">\n'
        "![reference image][scan]\n"
        "[scan]: https://assets.example/reference.png\n"
    )

    report = _analyzer().analyze(pdf_path, markdown)

    page = report.per_page[0]
    assert report.status is PdfQualityStatus.OCR_REQUIRED
    assert report.text_coverage_ratio == 0.0
    assert page.markdown_text_char_count == 0
    assert page.pdf_text_char_count == 0
    assert page.image_coverage_ratio == pytest.approx(1.0)
    assert page.is_image_only is True
    assert page.ocr_required is True
    assert "IMAGE_ONLY" in page.warnings
    assert "OCR_REQUIRED" in page.warnings


def test_pdf_text_layer_cannot_hide_an_image_only_odl_output(tmp_path: Path) -> None:
    pdf_path = tmp_path / "text-layer-with-lost-odl-body.pdf"
    _write_text_pdf(pdf_path)
    markdown = (
        "<!-- ODL_PAGE:1 -->\n"
        "![page preview](images/page-1.png)\n"
        "<!-- ODL_PAGE:2 -->\n"
        "Energy consumption and emission factor text for the second page."
    )

    report = _analyzer().analyze(pdf_path, markdown)

    first_page = report.per_page[0]
    assert first_page.pdf_text_char_count >= 10
    assert first_page.markdown_text_char_count == 0
    assert first_page.effective_text_char_count == 0
    assert first_page.is_image_only is True
    assert first_page.ocr_required is True
    assert report.status is PdfQualityStatus.OCR_REQUIRED
    assert report.text_coverage_ratio == 0.5


def test_missing_odl_body_on_text_page_requires_fallback(tmp_path: Path) -> None:
    pdf_path = tmp_path / "text-layer-with-empty-odl-section.pdf"
    _write_text_pdf(pdf_path)
    markdown = (
        "<!-- ODL_PAGE:1 -->\n"
        "<!-- ODL_PAGE:2 -->\n"
        "Energy consumption and emission factor text for the second page."
    )

    report = _analyzer().analyze(pdf_path, markdown)

    first_page = report.per_page[0]
    assert first_page.pdf_text_char_count >= 10
    assert first_page.effective_text_char_count == 0
    assert first_page.ocr_required is True
    assert "ODL_TEXT_INSUFFICIENT" in first_page.warnings
    assert report.status is PdfQualityStatus.OCR_REQUIRED


def test_scanned_page_requires_real_ocr_even_when_odl_emits_long_text(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "scan-with-unverified-odl-text.pdf"
    _write_image_only_pdf(pdf_path)
    markdown = (
        "<!-- ODL_PAGE:1 -->\n"
        "这是一段长度足够但没有经过本项目页级 OCR 的扫描页识别结果，不能直接通过门禁。"
    )

    report = _analyzer().analyze(pdf_path, markdown)

    page = report.per_page[0]
    assert page.is_image_only is True
    assert page.ocr_applied is False
    assert page.ocr_required is True
    assert report.ocr_page_count == 0
    assert report.status is PdfQualityStatus.OCR_REQUIRED


def test_full_page_raster_with_hidden_text_layer_still_requires_real_ocr(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "scan-with-hidden-text-layer.pdf"
    hidden_text = "Hidden searchable emission accounting text must not bypass OCR."
    _write_raster_pdf_with_text_layer(pdf_path, hidden_text, hidden=True)

    report = _analyzer().analyze(
        pdf_path,
        f"<!-- ODL_PAGE:1 -->\n{hidden_text}",
    )

    page = report.per_page[0]
    assert page.hidden_text_char_count > 0
    assert page.visible_text_char_count == 0
    assert page.is_image_only is True
    assert page.ocr_required is True
    assert "HIDDEN_TEXT_LAYER" in page.warnings
    assert report.status is PdfQualityStatus.OCR_REQUIRED


def test_full_page_raster_with_tiny_visible_page_number_requires_real_ocr(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "scan-with-visible-page-number.pdf"
    _write_raster_pdf_with_text_layer(pdf_path, "Page1", hidden=False)

    report = _analyzer().analyze(pdf_path, "<!-- ODL_PAGE:1 -->\nPage1")

    page = report.per_page[0]
    assert page.visible_text_char_count == 5
    assert page.visible_text_area_ratio < 0.02
    assert page.is_image_only is True
    assert page.ocr_required is True
    assert "RASTER_DOMINANT_LOW_VISIBLE_TEXT_AREA" in page.warnings
    assert report.status is PdfQualityStatus.OCR_REQUIRED


def test_long_source_page_with_severely_truncated_odl_text_requires_fallback(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "truncated.pdf"
    source_lines = [
        f"Emission source {index} uses factor {index}.25 tCO2 per unit in 2025."
        for index in range(1, 16)
    ]
    _write_single_text_pdf(pdf_path, "\n".join(source_lines))
    markdown = "<!-- ODL_PAGE:1 -->\nEmission source 1 uses factor 1.25 tCO2."

    report = _analyzer().analyze(pdf_path, markdown)

    page = report.per_page[0]
    assert page.effective_text_char_count >= 10
    assert page.text_retention_ratio is not None
    assert page.text_retention_ratio < 0.8
    assert page.content_covered is False
    assert page.ocr_required is True
    assert report.text_coverage_ratio == 0.0
    assert report.status is PdfQualityStatus.OCR_REQUIRED


def test_same_characters_in_wrong_order_cannot_pass_retention_gate(tmp_path: Path) -> None:
    pdf_path = tmp_path / "reordered-text.pdf"
    source_text = "ABCDEFGH12345678carbonfactor2025"
    _write_single_text_pdf(pdf_path, source_text)

    report = _analyzer().analyze(
        pdf_path,
        f"<!-- ODL_PAGE:1 -->\n{source_text[::-1]}",
    )

    page = report.per_page[0]
    assert page.text_retention_ratio is not None
    assert page.text_retention_ratio < 0.8
    assert page.text_precision_ratio is not None
    assert page.text_precision_ratio < 0.8
    assert page.ocr_required is True
    assert report.status is PdfQualityStatus.OCR_REQUIRED


def test_complete_source_with_large_extra_output_fails_precision_gate(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "hallucinated-suffix.pdf"
    source_text = "Carbon accounting factor is 2.35 tCO2 per unit in 2025."
    _write_single_text_pdf(pdf_path, source_text)
    hallucinated = "HALLUCINATED999" * 100

    report = _analyzer().analyze(
        pdf_path,
        f"<!-- ODL_PAGE:1 -->\n{source_text}\n{hallucinated}",
    )

    page = report.per_page[0]
    assert page.text_retention_ratio == 1.0
    assert page.text_precision_ratio is not None
    assert page.text_precision_ratio < 0.1
    assert page.text_fidelity_output_char_count > page.pdf_text_char_count
    assert "TEXT_PRECISION_LOW" in page.warnings
    assert page.content_covered is False
    assert page.ocr_required is True
    assert report.status is PdfQualityStatus.OCR_REQUIRED


def test_duplicated_complete_body_fails_precision_gate(tmp_path: Path) -> None:
    pdf_path = tmp_path / "duplicated-body.pdf"
    source_text = "Emission factor 1.25 tCO2 applies to the reporting year 2025."
    _write_single_text_pdf(pdf_path, source_text)

    report = _analyzer().analyze(
        pdf_path,
        f"<!-- ODL_PAGE:1 -->\n{source_text}\n{source_text}",
    )

    page = report.per_page[0]
    assert page.text_retention_ratio == 1.0
    assert page.text_precision_ratio == pytest.approx(0.5)
    assert "TEXT_PRECISION_LOW" in page.warnings
    assert page.ocr_required is True
    assert report.status is PdfQualityStatus.OCR_REQUIRED


def test_controlled_vision_supplement_is_excluded_from_body_precision(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "vision-supplement.pdf"
    source_text = "System boundary includes purchased electricity and direct fuel use."
    _write_single_text_pdf(pdf_path, source_text)
    supplement = (
        "图片说明：流程图额外标注节点 A 指向节点 B，数值为 42.75，"
        "这些结构化视觉信息不存在于 PDF 文本层。"
    )

    report = _analyzer().analyze(
        pdf_path,
        (
            f"<!-- ODL_PAGE:1 -->\n{source_text}\n\n"
            f"<!-- PAGE_FALLBACK:VISION -->\n\n{supplement}"
        ),
    )

    page = report.per_page[0]
    assert page.markdown_text_char_count > page.text_fidelity_output_char_count
    assert page.text_fidelity_output_char_count == page.pdf_text_char_count
    assert page.text_retention_ratio == 1.0
    assert page.text_precision_ratio == 1.0
    assert "TEXT_PRECISION_LOW" not in page.warnings
    assert report.status is PdfQualityStatus.PASSED


def test_equivalent_latex_operator_does_not_create_false_retention_failure(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "formula-operator.pdf"
    _write_single_text_pdf(pdf_path, "E=AD*EF")

    report = _analyzer().analyze(
        pdf_path,
        "<!-- ODL_PAGE:1 -->\n$$E=AD\\times EF$$",
    )

    assert report.per_page[0].text_retention_ratio == 1.0
    assert report.status is PdfQualityStatus.PASSED


@pytest.mark.parametrize(
    "markup_only_output",
    [
        "<table><tr><td></td></tr></table>",
        "[x](https://tabletrtdtabletrtd.example)",
    ],
)
def test_markup_and_link_targets_cannot_forge_text_retention(
    tmp_path: Path,
    markup_only_output: str,
) -> None:
    pdf_path = tmp_path / "markup-forgery.pdf"
    _write_single_text_pdf(pdf_path, "tabletrtdtabletrtd")

    report = _analyzer().analyze(
        pdf_path,
        f"<!-- ODL_PAGE:1 -->\n{markup_only_output}",
    )

    page = report.per_page[0]
    assert page.text_retention_ratio == 0.0
    assert page.ocr_required is True
    assert report.status is PdfQualityStatus.OCR_REQUIRED


def test_short_formula_page_cannot_be_silently_dropped(tmp_path: Path) -> None:
    pdf_path = tmp_path / "short-formula.pdf"
    _write_single_text_pdf(pdf_path, "CO2=42 t/y")

    report = _analyzer().analyze(pdf_path, "<!-- ODL_PAGE:1 -->")

    page = report.per_page[0]
    assert 0 < page.pdf_text_char_count < 10
    assert page.text_retention_ratio == 0.0
    assert page.ocr_required is True
    assert report.status is PdfQualityStatus.OCR_REQUIRED


def test_ocr_text_recovers_scan_but_low_confidence_is_reported(tmp_path: Path) -> None:
    pdf_path = tmp_path / "scan.pdf"
    _write_image_only_pdf(pdf_path)
    markdown = "<!-- ODL_PAGE:1 -->\n![scan](images/page-1.png)"

    report = _analyzer().analyze(
        pdf_path,
        markdown,
        ocr_results=[
            PdfOcrPageResult(
                page_number=1,
                text="Recognized carbon inventory content from the scanned page.",
                confidence=0.6,
            )
        ],
    )

    page = report.per_page[0]
    assert report.status is PdfQualityStatus.LOW_CONFIDENCE
    assert report.ocr_page_count == 1
    assert report.low_confidence_pages == (1,)
    assert report.text_coverage_ratio == 1.0
    assert page.is_image_only is True
    assert page.ocr_applied is True
    assert page.ocr_required is False
    assert page.low_confidence is True
    assert page.ocr_confidence == 0.6
    assert "OCR_LOW_CONFIDENCE" in page.warnings


def test_high_confidence_ocr_passes_and_report_is_json_serializable(tmp_path: Path) -> None:
    pdf_path = tmp_path / "scan.pdf"
    _write_image_only_pdf(pdf_path)
    markdown = "<!-- ODL_PAGE:1 -->\n![scan](images/page-1.png)"

    report = _analyzer().analyze(
        pdf_path,
        markdown,
        ocr_results={
            1: {
                "text": "Verified emission accounting content from OCR processing.",
                "confidence": 0.93,
            }
        },
    )
    serialized = report.to_dict()

    assert report.status is PdfQualityStatus.PASSED
    assert report.ocr_page_count == 1
    assert report.low_confidence_pages == ()
    assert serialized["status"] == "PASSED"
    assert serialized["thresholds"] == {
        "min_effective_text_chars": 10,
        "image_only_max_text_chars": 3,
        "image_only_min_coverage_ratio": 0.8,
        "min_ocr_confidence": 0.75,
        "min_text_retention_ratio": 0.97,
        "min_text_precision_ratio": 0.97,
    }
    assert serialized["per_page"][0]["warnings"] == [
        "ODL_TEXT_INSUFFICIENT",
        "IMAGE_ONLY",
    ]
    json.dumps(serialized, ensure_ascii=False)


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("min_effective_text_chars", 0),
        ("image_only_max_text_chars", -1),
        ("image_only_min_coverage_ratio", 1.1),
        ("min_ocr_confidence", -0.1),
        ("min_text_retention_ratio", 1.1),
        ("min_text_precision_ratio", 1.1),
    ],
)
def test_invalid_thresholds_are_rejected(keyword: str, value: int | float) -> None:
    with pytest.raises(ValueError):
        PdfQualityAnalyzer(**{keyword: value})
