from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from app.rag.core.llm.exceptions import (
    AuthenticationError,
    ConfigurationException,
    RateLimitError,
)
from app.rag.core.llm.providers.google import GoogleProvider
from app.rag.core.parser.pdf.page_fallback import (
    AnalyzeImagePageProviderAdapter,
    PageFallbackMethod,
    PageProviderOutput,
    PdfPageFallbackProcessor,
    QualityGatedOcrPageProvider,
    RapidOcrPageProviderAdapter,
    RenderedPdfPage,
)
from app.rag.core.parser.pdf.quality import PdfQualityAnalyzer, PdfQualityStatus


def _png_bytes() -> bytes:
    pixmap = pymupdf.Pixmap(
        pymupdf.csRGB,
        pymupdf.IRect(0, 0, 8, 8),
        False,
    )
    pixmap.clear_with(0xBBDDEE)
    return pixmap.tobytes("png")


def _write_mixed_pdf(path: Path) -> None:
    document = pymupdf.open()

    scan_page = document.new_page(width=144, height=180)
    scan_page.insert_image(scan_page.rect, stream=_png_bytes())

    chart_page = document.new_page(width=180, height=144)
    chart_page.insert_image(
        pymupdf.Rect(10, 45, 170, 130),
        stream=_png_bytes(),
    )
    chart_page.insert_textbox(
        pymupdf.Rect(10, 10, 170, 42),
        "Carbon emission trend chart with complete source text.",
        fontsize=9,
    )

    text_page = document.new_page(width=144, height=180)
    text_page.insert_textbox(
        pymupdf.Rect(10, 10, 134, 100),
        "Ordinary energy accounting paragraph without any visual element.",
        fontsize=9,
    )

    document.save(path)
    document.close()


def _write_text_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=144, height=180)
    page.insert_textbox(
        pymupdf.Rect(10, 10, 134, 100),
        "A complete text-only carbon accounting page for fallback selection.",
        fontsize=9,
    )
    document.save(path)
    document.close()


def _write_vector_chart_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=240, height=180)
    page.draw_rect(pymupdf.Rect(20, 50, 90, 100), color=(0, 0, 0))
    page.draw_rect(pymupdf.Rect(150, 50, 220, 100), color=(0, 0, 0))
    page.draw_line((90, 75), (150, 75), color=(0, 0, 0))
    page.draw_line((140, 68), (150, 75), color=(0, 0, 0))
    page.draw_line((140, 82), (150, 75), color=(0, 0, 0))
    page.insert_text((20, 25), "Activity data flows to reported emissions.", fontsize=9)
    document.save(path)
    document.close()


def _write_small_grouped_vector_pdf(path: Path, *, logo: bool) -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    page.insert_textbox(
        pymupdf.Rect(40, 40, 560, 100),
        "Complete carbon accounting context remains available as source text.",
        fontsize=10,
    )
    if logo:
        box = pymupdf.Rect(540, 15, 570, 35)
    else:
        box = pymupdf.Rect(220, 260, 340, 310)
    shape = page.new_shape()
    shape.draw_rect(box)
    left = pymupdf.Point(box.x0, (box.y0 + box.y1) / 2)
    middle = pymupdf.Point((box.x0 + box.x1) / 2, box.y0)
    right = pymupdf.Point(box.x1, (box.y0 + box.y1) / 2)
    bottom = pymupdf.Point((box.x0 + box.x1) / 2, box.y1)
    shape.draw_line(left, middle)
    shape.draw_line(middle, right)
    shape.draw_line(right, bottom)
    shape.draw_line(bottom, left)
    shape.draw_line(left, right)
    shape.finish(color=(0, 0, 0))
    shape.commit()
    document.save(path)
    document.close()


def _write_small_raster_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    page.insert_textbox(
        pymupdf.Rect(40, 40, 560, 120),
        "Complete source text accompanies a small visual asset on this page.",
        fontsize=10,
    )
    page.insert_image(
        pymupdf.Rect(250, 300, 310, 360),
        stream=_png_bytes(),
    )
    document.save(path)
    document.close()


def _mixed_markdown() -> str:
    return (
        "<!-- ODL_PAGE:1 -->\n"
        "![full page scan](images/page-1.png)\n"
        "<!-- ODL_PAGE:2 -->\n"
        "OpenDataLoader retained the chart caption and surrounding carbon trend text.\n"
        "![emission chart](images/page-2-chart.png)\n"
        "<!-- ODL_PAGE:3 -->\n"
        "OpenDataLoader retained this ordinary energy accounting paragraph."
    )


def _quality_analyzer() -> PdfQualityAnalyzer:
    return PdfQualityAnalyzer(
        min_effective_text_chars=10,
        image_only_max_text_chars=3,
        image_only_min_coverage_ratio=0.8,
        min_ocr_confidence=0.75,
        # These tests isolate fallback targeting. Text-retention behavior has its own
        # quality-gate suite and the fixture intentionally paraphrases source text.
        min_text_retention_ratio=0,
    )


class _FakeOcrProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[int, RenderedPdfPage]] = []

    async def recognize_page(
        self,
        *,
        page_number: int,
        image: RenderedPdfPage,
    ) -> PageProviderOutput:
        self.calls.append((page_number, image))
        return PageProviderOutput(
            text="Scanned carbon inventory value is 42 tCO2e.",
            markdown="Scanned carbon inventory value is **42 tCO2e**.",
            confidence=0.91,
        )


class _FakeVisionProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[int, RenderedPdfPage, str]] = []

    async def analyze_page(
        self,
        *,
        page_number: int,
        image: RenderedPdfPage,
        odl_markdown: str,
    ) -> PageProviderOutput:
        self.calls.append((page_number, image, odl_markdown))
        return PageProviderOutput(
            text="The chart shows emissions falling from 50 to 42 tCO2e.",
            markdown="Chart supplement: emissions fall from **50** to **42 tCO2e**.",
            confidence=0.88,
            warnings=("VISION_FAKE_WARNING",),
        )


class _LargeStructuredVisionProvider:
    async def analyze_page(self, **_kwargs: object) -> PageProviderOutput:
        return PageProviderOutput(
            text="structured visual result",
            markdown="structured visual result",
            structured_data={
                "has_visual_content": True,
                "visual_content_type": "chart",
                "summary": "x" * 2048,
            },
        )


@pytest.mark.asyncio
async def test_fallback_processes_only_ocr_required_and_chart_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "mixed.pdf"
    _write_mixed_pdf(pdf_path)
    markdown = _mixed_markdown()
    quality = _quality_analyzer().analyze(pdf_path, markdown)
    assert quality.status is PdfQualityStatus.OCR_REQUIRED

    ocr_provider = _FakeOcrProvider()
    vision_provider = _FakeVisionProvider()
    fallback = await PdfPageFallbackProcessor(
        ocr_provider=ocr_provider,
        vision_provider=vision_provider,
    ).process(pdf_path, markdown, quality)

    assert [result.page_number for result in fallback.results] == [1, 2]
    assert [result.method for result in fallback.results] == [
        PageFallbackMethod.OCR,
        PageFallbackMethod.VISION,
    ]
    assert len(ocr_provider.calls) == 1
    assert len(vision_provider.calls) == 1

    ocr_page_number, rendered_scan = ocr_provider.calls[0]
    assert ocr_page_number == 1
    assert rendered_scan.page_number == 1
    assert rendered_scan.dpi == 280
    assert rendered_scan.media_type == "image/png"
    assert rendered_scan.image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert (rendered_scan.width, rendered_scan.height) == (560, 700)

    vision_page_number, rendered_chart, odl_context = vision_provider.calls[0]
    assert vision_page_number == 2
    assert rendered_chart.page_number == 2
    assert rendered_chart.dpi == 280
    assert "surrounding carbon trend text" in odl_context

    ocr_result, vision_result = fallback.results
    assert ocr_result.markdown.startswith(
        "<!-- ODL_PAGE:1 -->\n![full page scan](images/page-1.png)"
    )
    assert "<!-- PAGE_FALLBACK:OCR -->" in ocr_result.markdown
    assert "42 tCO2e" in ocr_result.markdown
    assert vision_result.markdown.startswith("<!-- ODL_PAGE:2 -->")
    assert "<!-- PAGE_FALLBACK:VISION -->" in vision_result.markdown
    assert vision_result.warnings == ("VISION_FAKE_WARNING",)


@pytest.mark.asyncio
async def test_ocr_result_mapping_can_be_reused_by_quality_analyzer(tmp_path: Path) -> None:
    pdf_path = tmp_path / "mixed.pdf"
    _write_mixed_pdf(pdf_path)
    markdown = _mixed_markdown()
    analyzer = _quality_analyzer()
    initial_quality = analyzer.analyze(pdf_path, markdown)

    fallback = await PdfPageFallbackProcessor(
        ocr_provider=_FakeOcrProvider(),
        vision_provider=_FakeVisionProvider(),
    ).process(pdf_path, markdown, initial_quality)
    refreshed_quality = analyzer.analyze(
        pdf_path,
        markdown,
        ocr_results=fallback.ocr_results,
    )

    assert set(fallback.ocr_results) == {1}
    assert fallback.ocr_results[1].confidence == 0.91
    assert refreshed_quality.status is PdfQualityStatus.PASSED
    assert refreshed_quality.per_page[0].ocr_required is False
    assert refreshed_quality.ocr_page_count == 1

    serialized = fallback.to_dict()
    assert serialized["processed_page_count"] == 2
    assert serialized["ocr_results"]["1"]["confidence"] == 0.91
    json.dumps(serialized, ensure_ascii=False)


@pytest.mark.asyncio
async def test_missing_providers_preserve_odl_page_markdown_with_warnings(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "mixed.pdf"
    _write_mixed_pdf(pdf_path)
    markdown = _mixed_markdown()
    quality = _quality_analyzer().analyze(pdf_path, markdown)

    fallback = await PdfPageFallbackProcessor().process(pdf_path, markdown, quality)

    assert len(fallback.results) == 2
    ocr_result, vision_result = fallback.results
    assert ocr_result.text == ""
    assert ocr_result.markdown == ("<!-- ODL_PAGE:1 -->\n![full page scan](images/page-1.png)")
    assert "OCR_PROVIDER_MISSING" in ocr_result.warnings
    assert fallback.ocr_results == {}
    assert vision_result.text == ""
    assert "VISION_PROVIDER_MISSING" in vision_result.warnings


@pytest.mark.asyncio
async def test_text_only_page_is_not_rendered_or_sent_to_providers(tmp_path: Path) -> None:
    pdf_path = tmp_path / "text.pdf"
    _write_text_pdf(pdf_path)
    markdown = (
        "<!-- ODL_PAGE:1 -->\n"
        "A complete text-only carbon accounting page for fallback selection."
    )
    quality = _quality_analyzer().analyze(pdf_path, markdown)
    ocr_provider = _FakeOcrProvider()
    vision_provider = _FakeVisionProvider()

    fallback = await PdfPageFallbackProcessor(
        ocr_provider=ocr_provider,
        vision_provider=vision_provider,
    ).process(pdf_path, markdown, quality)

    assert fallback.results == ()
    assert ocr_provider.calls == []
    assert vision_provider.calls == []


@pytest.mark.asyncio
async def test_vector_only_flow_diagram_is_sent_to_vision_provider(tmp_path: Path) -> None:
    pdf_path = tmp_path / "vector-flow.pdf"
    _write_vector_chart_pdf(pdf_path)
    markdown = "<!-- ODL_PAGE:1 -->\nActivity data flows to reported emissions."
    quality = _quality_analyzer().analyze(pdf_path, markdown)
    assert quality.per_page[0].image_count == 0
    assert quality.per_page[0].vector_drawing_count >= 2
    vision_provider = _FakeVisionProvider()

    fallback = await PdfPageFallbackProcessor(
        vision_provider=vision_provider,
    ).process(pdf_path, markdown, quality)

    assert len(fallback.results) == 1
    assert fallback.results[0].method is PageFallbackMethod.VISION
    assert len(vision_provider.calls) == 1


@pytest.mark.asyncio
async def test_small_unlabeled_grouped_vector_chart_is_not_missed(tmp_path: Path) -> None:
    pdf_path = tmp_path / "small-vector-chart.pdf"
    _write_small_grouped_vector_pdf(pdf_path, logo=False)
    markdown = (
        "<!-- ODL_PAGE:1 -->\n"
        "Complete carbon accounting context remains available as source text."
    )
    quality = _quality_analyzer().analyze(pdf_path, markdown)
    page_quality = quality.per_page[0]
    assert page_quality.vector_drawing_count == 1
    assert page_quality.vector_segment_count >= 4
    assert 0.01 <= page_quality.vector_coverage_ratio < 0.08
    provider = _FakeVisionProvider()

    fallback = await PdfPageFallbackProcessor(vision_provider=provider).process(
        pdf_path,
        markdown,
        quality,
    )

    assert len(fallback.results) == 1
    assert fallback.results[0].method is PageFallbackMethod.VISION
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_tiny_unlabeled_vector_logo_is_not_sent_to_vision(tmp_path: Path) -> None:
    pdf_path = tmp_path / "tiny-vector-logo.pdf"
    _write_small_grouped_vector_pdf(pdf_path, logo=True)
    markdown = (
        "<!-- ODL_PAGE:1 -->\n"
        "Complete carbon accounting context remains available as source text."
    )
    quality = _quality_analyzer().analyze(pdf_path, markdown)
    page_quality = quality.per_page[0]
    assert page_quality.vector_segment_count >= 4
    assert page_quality.vector_coverage_ratio < 0.01
    provider = _FakeVisionProvider()

    fallback = await PdfPageFallbackProcessor(vision_provider=provider).process(
        pdf_path,
        markdown,
        quality,
    )

    assert fallback.results == ()
    assert provider.calls == []


@pytest.mark.asyncio
async def test_small_unknown_raster_asset_is_visually_assessed_with_exact_manifest(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "small-unknown-raster.pdf"
    _write_small_raster_pdf(pdf_path)
    markdown = (
        "<!-- ODL_PAGE:1 -->\n"
        "Complete source text accompanies a small visual asset on this page.\n"
        "![equipment layout](images/unknown.png)"
    )
    quality = _quality_analyzer().analyze(pdf_path, markdown)
    page_quality = quality.per_page[0]
    assert 0 < page_quality.image_coverage_ratio < 0.01
    provider = _FakeVisionProvider()

    fallback = await PdfPageFallbackProcessor(vision_provider=provider).process(
        pdf_path,
        markdown,
        quality,
    )

    assert [result.method for result in fallback.results] == [PageFallbackMethod.VISION]
    assert len(provider.calls) == 1
    manifest = provider.calls[0][1].asset_manifest
    assert len(manifest) == 1
    assert manifest[0]["source_ref"] == "images/unknown.png"
    assert str(manifest[0]["asset_key"]).startswith("asset-occurrence:")


@pytest.mark.asyncio
async def test_document_structured_report_byte_limit_blocks_persistence(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "large-structured-report.pdf"
    _write_small_raster_pdf(pdf_path)
    markdown = (
        "<!-- ODL_PAGE:1 -->\n"
        "Complete source text accompanies a small visual asset on this page.\n"
        "![equipment layout](images/unknown.png)"
    )
    quality = _quality_analyzer().analyze(pdf_path, markdown)

    fallback = await PdfPageFallbackProcessor(
        vision_provider=_LargeStructuredVisionProvider(),
        max_structured_report_bytes=128,
    ).process(pdf_path, markdown, quality)

    assert fallback.results[0].error_code == "STRUCTURED_REPORT_BYTES_EXCEEDED"
    assert fallback.results[0].retryable is False
    assert fallback.results[0].structured_data is None
    assert "STRUCTURED_REPORT_BYTES_EXCEEDED" in fallback.warnings[0]


@pytest.mark.asyncio
async def test_render_limits_fail_before_allocating_or_calling_provider(tmp_path: Path) -> None:
    pdf_path = tmp_path / "mixed.pdf"
    _write_mixed_pdf(pdf_path)
    markdown = _mixed_markdown()
    quality = _quality_analyzer().analyze(pdf_path, markdown)
    ocr_provider = _FakeOcrProvider()

    fallback = await PdfPageFallbackProcessor(
        ocr_provider=ocr_provider,
        vision_provider=_FakeVisionProvider(),
        max_rendered_page_pixels=1,
    ).process(pdf_path, markdown, quality)

    assert fallback.results
    assert all(result.error_code == "PAGE_RENDER_LIMIT_EXCEEDED" for result in fallback.results)
    assert all(result.retryable is False for result in fallback.results)
    assert ocr_provider.calls == []


class _FakeAnalyzeImageProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def analyze_image(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            content="  Official HTTP provider result.  ",
            finish_reason="stop",
        )


class _StructuredAnalyzeImageProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def analyze_image(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=json.dumps(
                {
                    "markdown": "排放量 = 活动数据 × 排放因子",
                    "confidence": 0.93,
                    "has_visual_content": True,
                    "visual_content_type": "flowchart",
                    "summary": "流程图展示活动数据与排放因子共同计算排放量。",
                    "entities": ["活动数据", "排放因子", "排放量"],
                    "relationships": [
                        {"source": "活动数据", "relation": "参与计算", "target": "排放量"}
                    ],
                    "key_values": [{"label": "排放量", "value": "42 tCO2e"}],
                    "units": ["tCO2e"],
                    "unexpected_blob": "must not be persisted",
                },
                ensure_ascii=False,
            ),
            finish_reason="stop",
        )


class _TruncatedAnalyzeImageProvider:
    async def analyze_image(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            content=json.dumps(
                {"markdown": "partial OCR text", "confidence": 0.99},
            ),
            finish_reason="length",
        )


class _NoVisualAnalyzeImageProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def analyze_image(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=json.dumps(
                {
                    "markdown": "",
                    "has_visual_content": False,
                    "visual_content_type": "none",
                    "summary": "",
                    "entities": [],
                    "relationships": [],
                    "key_values": [],
                    "units": [],
                },
                ensure_ascii=False,
            ),
            finish_reason="stop",
        )


class _FinishReasonAnalyzeImageProvider:
    def __init__(self, finish_reason: str | None) -> None:
        self.finish_reason = finish_reason

    async def analyze_image(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            content=json.dumps(
                {"markdown": "provider payload", "confidence": 0.98},
            ),
            finish_reason=self.finish_reason,
        )


class _RateLimitedOcrProvider:
    async def recognize_page(self, **_kwargs: object) -> PageProviderOutput:
        error = RuntimeError("rate limited")
        error.status_code = 429
        raise error


@pytest.mark.asyncio
async def test_analyze_image_adapter_reuses_existing_provider_contract() -> None:
    provider = _FakeAnalyzeImageProvider()
    adapter = AnalyzeImagePageProviderAdapter(
        provider,
        model_name="official-vision-model",
    )
    image = RenderedPdfPage(
        page_number=4,
        image_bytes=_png_bytes(),
        media_type="image/png",
        dpi=280,
        width=560,
        height=700,
    )

    ocr_output = await adapter.recognize_page(page_number=4, image=image)
    vision_output = await adapter.analyze_page(
        page_number=4,
        image=image,
        odl_markdown="Existing ODL chart caption.",
    )

    assert ocr_output.text == "Official HTTP provider result."
    assert ocr_output.confidence is None
    assert ocr_output.warnings == ("CONFIDENCE_UNAVAILABLE",)
    assert vision_output.text == "Official HTTP provider result."
    assert len(provider.calls) == 2
    assert provider.calls[0]["media_type"] == "image/png"
    assert provider.calls[0]["model"] == "official-vision-model"
    assert base64.b64decode(str(provider.calls[0]["image_base64"])) == image.image_bytes
    assert "Existing ODL chart caption." in str(provider.calls[1]["prompt"])


@pytest.mark.asyncio
async def test_ocr_adapter_preserves_confidence_formula_and_visual_structure() -> None:
    provider = _StructuredAnalyzeImageProvider()
    adapter = AnalyzeImagePageProviderAdapter(provider, model_name="vision-model")
    image = RenderedPdfPage(
        page_number=2,
        image_bytes=_png_bytes(),
        media_type="image/png",
        dpi=280,
        width=560,
        height=700,
    )

    output = await adapter.recognize_page(page_number=2, image=image)

    assert output.confidence == 0.93
    assert "活动数据 × 排放因子" in output.markdown
    assert output.structured_data["source_page"] == 2
    assert output.structured_data["relationships"][0]["target"] == "排放量"
    assert output.structured_data["has_visual_content"] is True
    assert "markdown" not in output.structured_data
    assert "confidence" not in output.structured_data
    assert "unexpected_blob" not in output.structured_data
    assert "公式上下标和运算符" in str(provider.calls[0]["prompt"])
    assert "has_visual_content" in str(provider.calls[0]["prompt"])
    assert provider.calls[0]["temperature"] == 0
    assert provider.calls[0]["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_qwen_visual_adapter_requests_non_thinking_json_output() -> None:
    provider = _StructuredAnalyzeImageProvider()
    adapter = AnalyzeImagePageProviderAdapter(provider, model_name="Qwen3.6-27B")
    image = RenderedPdfPage(1, _png_bytes(), "image/png", 280, 100, 100)

    await adapter.recognize_page(page_number=1, image=image)

    assert provider.calls[0]["enable_thinking"] is False
    assert provider.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_kimi_visual_adapter_uses_supported_non_thinking_json_parameters() -> None:
    provider = _StructuredAnalyzeImageProvider()
    adapter = AnalyzeImagePageProviderAdapter(provider, model_name="kimi-k2.6")
    image = RenderedPdfPage(1, _png_bytes(), "image/png", 280, 100, 100)

    await adapter.recognize_page(page_number=1, image=image)

    assert provider.calls[0]["temperature"] == 1.0
    assert provider.calls[0]["thinking"] == {"type": "disabled"}
    assert provider.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_kimi_plan_visual_adapter_uses_code_endpoint_temperature() -> None:
    provider = _StructuredAnalyzeImageProvider()
    provider.api_base_url = "https://api.kimi.com/coding/v1/chat/completions"
    adapter = AnalyzeImagePageProviderAdapter(provider, model_name="kimi-k2.6")
    image = RenderedPdfPage(1, _png_bytes(), "image/png", 280, 100, 100)

    await adapter.recognize_page(page_number=1, image=image)

    assert provider.calls[0]["temperature"] == 0.6
    assert provider.calls[0]["thinking"] == {"type": "disabled"}
    assert provider.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_explicit_no_visual_assessment_closes_small_unknown_asset_task(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "small-decoration.pdf"
    _write_small_raster_pdf(pdf_path)
    markdown = (
        "<!-- ODL_PAGE:1 -->\n"
        "Complete source text accompanies a small visual asset on this page.\n"
        "![equipment layout](images/unknown.png)"
    )
    quality = _quality_analyzer().analyze(pdf_path, markdown)
    raw_provider = _NoVisualAnalyzeImageProvider()
    adapter = AnalyzeImagePageProviderAdapter(raw_provider)

    fallback = await PdfPageFallbackProcessor(vision_provider=adapter).process(
        pdf_path,
        markdown,
        quality,
    )

    assert len(fallback.results) == 1
    result = fallback.results[0]
    assert result.error_code is None
    assert result.structured_data["has_visual_content"] is False
    assert result.markdown == markdown
    prompt = str(raw_provider.calls[0]["prompt"])
    assert "asset_key" in prompt
    assert "images/unknown.png" in prompt


@pytest.mark.asyncio
async def test_adapter_marks_token_truncation_as_incomplete() -> None:
    adapter = AnalyzeImagePageProviderAdapter(_TruncatedAnalyzeImageProvider())
    image = RenderedPdfPage(1, _png_bytes(), "image/png", 280, 100, 100)

    output = await adapter.recognize_page(page_number=1, image=image)

    assert output.truncated is True
    assert output.confidence == 0.0
    assert "PROVIDER_OUTPUT_TRUNCATED" in output.warnings


@pytest.mark.parametrize("finish_reason", ["stop", "STOP", "end_turn", "stop_sequence"])
@pytest.mark.asyncio
async def test_adapter_accepts_only_known_success_finish_reasons(
    finish_reason: str,
) -> None:
    adapter = AnalyzeImagePageProviderAdapter(
        _FinishReasonAnalyzeImageProvider(finish_reason)
    )
    image = RenderedPdfPage(1, _png_bytes(), "image/png", 280, 100, 100)

    output = await adapter.recognize_page(page_number=1, image=image)

    assert output.completion_error_code is None
    assert output.text == "provider payload"
    assert output.confidence == 0.98


@pytest.mark.parametrize(
    ("finish_reason", "expected_code", "truncated"),
    [
        ("length", "PROVIDER_OUTPUT_TRUNCATED", True),
        ("MAX_TOKENS", "PROVIDER_OUTPUT_TRUNCATED", True),
        ("content_filter", "PROVIDER_OUTPUT_FILTERED", False),
        ("SAFETY", "PROVIDER_OUTPUT_FILTERED", False),
        ("RECITATION", "PROVIDER_OUTPUT_FILTERED", False),
        ("OTHER", "PROVIDER_ABNORMAL_FINISH", False),
        (None, "PROVIDER_FINISH_REASON_MISSING", False),
    ],
)
@pytest.mark.asyncio
async def test_adapter_discards_non_normal_provider_completions(
    finish_reason: str | None,
    expected_code: str,
    truncated: bool,
) -> None:
    adapter = AnalyzeImagePageProviderAdapter(
        _FinishReasonAnalyzeImageProvider(finish_reason)
    )
    image = RenderedPdfPage(1, _png_bytes(), "image/png", 280, 100, 100)

    output = await adapter.recognize_page(page_number=1, image=image)

    assert output.completion_error_code == expected_code
    assert output.truncated is truncated
    assert output.text == ""
    assert output.markdown == ""
    assert output.structured_data is None
    assert output.confidence == 0.0
    assert expected_code in output.warnings


@pytest.mark.asyncio
async def test_google_prompt_safety_block_without_candidate_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = GoogleProvider(
        api_key="test-key",
        api_base_url="https://example.invalid/v1beta",
        model_name="official-vision-model",
    )

    async def blocked_response(**_kwargs: object) -> dict[str, object]:
        return {"promptFeedback": {"blockReason": "SAFETY"}}

    monkeypatch.setattr(provider._client, "generate_content", blocked_response)
    adapter = AnalyzeImagePageProviderAdapter(provider)
    image = RenderedPdfPage(1, _png_bytes(), "image/png", 280, 100, 100)

    output = await adapter.recognize_page(page_number=1, image=image)

    assert output.finish_reason == "safety"
    assert output.completion_error_code == "PROVIDER_OUTPUT_FILTERED"
    assert output.text == ""


@pytest.mark.asyncio
async def test_fallback_never_merges_truncated_provider_payload(tmp_path: Path) -> None:
    pdf_path = tmp_path / "mixed.pdf"
    _write_mixed_pdf(pdf_path)
    markdown = _mixed_markdown()
    quality = _quality_analyzer().analyze(pdf_path, markdown)
    adapter = AnalyzeImagePageProviderAdapter(_TruncatedAnalyzeImageProvider())

    fallback = await PdfPageFallbackProcessor(
        ocr_provider=adapter,
        vision_provider=_FakeVisionProvider(),
    ).process(pdf_path, markdown, quality)

    ocr_result = next(
        result for result in fallback.results if result.method is PageFallbackMethod.OCR
    )
    assert ocr_result.error_code == "PROVIDER_OUTPUT_TRUNCATED"
    assert ocr_result.retryable is False
    assert ocr_result.truncated is True
    assert "partial OCR text" not in ocr_result.markdown
    assert ocr_result.markdown == (
        "<!-- ODL_PAGE:1 -->\n![full page scan](images/page-1.png)"
    )


@pytest.mark.asyncio
async def test_transient_provider_failure_is_marked_retryable(tmp_path: Path) -> None:
    pdf_path = tmp_path / "mixed.pdf"
    _write_mixed_pdf(pdf_path)
    markdown = _mixed_markdown()
    quality = _quality_analyzer().analyze(pdf_path, markdown)

    fallback = await PdfPageFallbackProcessor(
        ocr_provider=_RateLimitedOcrProvider(),
        vision_provider=_FakeVisionProvider(),
    ).process(pdf_path, markdown, quality)

    ocr_result = next(
        result for result in fallback.results if result.method is PageFallbackMethod.OCR
    )
    assert ocr_result.error_code == "PROVIDER_HTTP_429"
    assert ocr_result.retryable is True
    assert fallback.has_retryable_failure is True


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (422, False),
        (408, True),
        (425, True),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_provider_http_error_retry_policy_is_narrow(
    status_code: int,
    retryable: bool,
) -> None:
    error = RuntimeError(f"provider status {status_code}")
    error.status_code = status_code

    code, actual_retryable = PdfPageFallbackProcessor._classify_provider_error(error)

    assert code == f"PROVIDER_HTTP_{status_code}"
    assert actual_retryable is retryable


def test_explicit_retryable_cannot_override_deterministic_http_400() -> None:
    error = RuntimeError("bad request")
    error.status_code = 400
    error.retryable = True

    assert PdfPageFallbackProcessor._classify_provider_error(error) == (
        "PROVIDER_HTTP_400",
        False,
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            AuthenticationError(message="Invalid API Key", provider_type="test"),
            ("PROVIDER_AUTHENTICATION_FAILED", False),
        ),
        (
            ConfigurationException("api_base_url is not configured"),
            ("PROVIDER_CONFIGURATION_INVALID", False),
        ),
        (
            RateLimitError(message="rate limited", provider_type="test"),
            ("PROVIDER_RATE_LIMITED", True),
        ),
        (
            RuntimeError("content safety policy blocked"),
            ("PROVIDER_DETERMINISTIC_FAILURE", False),
        ),
        (TimeoutError("timed out"), ("PROVIDER_TRANSIENT_FAILURE", True)),
        (RuntimeError("unknown vendor failure"), ("PROVIDER_REQUEST_FAILED", True)),
    ],
)
def test_provider_non_http_error_retry_policy(
    error: BaseException,
    expected: tuple[str, bool],
) -> None:
    assert PdfPageFallbackProcessor._classify_provider_error(error) == expected


@pytest.mark.parametrize("dpi", [249, 301, True])
def test_render_dpi_must_stay_in_supported_range(dpi: int) -> None:
    with pytest.raises(ValueError, match="dpi"):
        PdfPageFallbackProcessor(dpi=dpi)


def test_provider_output_rejects_invalid_confidence() -> None:
    with pytest.raises(ValueError, match="confidence"):
        PageProviderOutput(text="invalid", confidence=1.1)


@pytest.mark.asyncio
async def test_rapidocr_adapter_normalizes_pp_ocrv6_output() -> None:
    class _Result:
        txts = ("能源消耗", "42 tCO2e")
        scores = (0.98, 0.92)

        @staticmethod
        def to_markdown() -> str:
            return "能源消耗  42 tCO2e"

    calls = []

    def engine(image_bytes: bytes):
        calls.append(image_bytes)
        return _Result()

    adapter = RapidOcrPageProviderAdapter(engine_factory=lambda: engine)
    image = RenderedPdfPage(
        page_number=1,
        image_bytes=_png_bytes(),
        media_type="image/png",
        dpi=280,
        width=8,
        height=8,
    )

    output = await adapter.recognize_page(page_number=1, image=image)

    assert calls == [image.image_bytes]
    assert output.text == "能源消耗\n42 tCO2e"
    assert output.markdown == "能源消耗  42 tCO2e"
    assert output.confidence == pytest.approx(0.95)
    assert output.warnings == ("OCR_SOURCE:RAPIDOCR_PP_OCRV6",)


@pytest.mark.asyncio
async def test_quality_gated_ocr_keeps_good_local_result() -> None:
    primary = _FakeOcrProvider()
    fallback = _FakeOcrProvider()
    provider = QualityGatedOcrPageProvider(
        primary,
        fallback,
        min_effective_text_chars=10,
        min_confidence=0.8,
    )
    image = RenderedPdfPage(1, _png_bytes(), "image/png", 280, 8, 8)

    output = await provider.recognize_page(page_number=1, image=image)

    assert output.confidence == 0.91
    assert len(primary.calls) == 1
    assert fallback.calls == []


@pytest.mark.asyncio
async def test_quality_gated_ocr_falls_back_when_local_confidence_is_low() -> None:
    class _LowConfidenceProvider(_FakeOcrProvider):
        async def recognize_page(self, **kwargs) -> PageProviderOutput:
            self.calls.append((kwargs["page_number"], kwargs["image"]))
            return PageProviderOutput(
                text="local text is long enough",
                confidence=0.3,
                warnings=("OCR_SOURCE:RAPIDOCR_PP_OCRV6",),
            )

    primary = _LowConfidenceProvider()
    fallback = _FakeOcrProvider()
    provider = QualityGatedOcrPageProvider(
        primary,
        fallback,
        min_effective_text_chars=10,
        min_confidence=0.8,
    )
    image = RenderedPdfPage(1, _png_bytes(), "image/png", 280, 8, 8)

    output = await provider.recognize_page(page_number=1, image=image)

    assert len(fallback.calls) == 1
    assert "OCR_SOURCE:VISION_FALLBACK" in output.warnings
    assert any(item.startswith("LOCAL_OCR_REJECTED:CONFIDENCE_LOW") for item in output.warnings)


def test_rendered_png_default_stays_below_provider_data_uri_limit() -> None:
    fourteen_mib = 14 * 1024 * 1024
    fifteen_mib = 15 * 1024 * 1024

    assert (
        PdfPageFallbackProcessor._provider_data_uri_size(fourteen_mib)
        <= PdfPageFallbackProcessor._MAX_PROVIDER_DATA_URI_BYTES
    )
    assert (
        PdfPageFallbackProcessor._provider_data_uri_size(fifteen_mib)
        > PdfPageFallbackProcessor._MAX_PROVIDER_DATA_URI_BYTES
    )
    with pytest.raises(ValueError, match="raw_image_bytes"):
        PdfPageFallbackProcessor._provider_data_uri_size(-1)
