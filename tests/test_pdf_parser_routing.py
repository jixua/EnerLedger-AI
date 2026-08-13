from __future__ import annotations

from pathlib import Path

import pymupdf

from app.rag.core.parser.pdf.base import BasePdfBackend
from app.rag.core.parser.pdf.models import PdfParseOptions
from app.rag.core.parser.pdf.registry import PdfBackendRegistry
from app.rag.core.parser.pdf.service import PdfParserService


def _png_bytes() -> bytes:
    pixmap = pymupdf.Pixmap(
        pymupdf.csRGB,
        pymupdf.IRect(0, 0, 8, 8),
        False,
    )
    pixmap.clear_with(0xDDEEFF)
    return pixmap.tobytes("png")


def _write_scan_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=240, height=320)
    page.insert_image(page.rect, stream=_png_bytes())
    document.save(path)
    document.close()


def _write_text_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=240, height=320)
    page.insert_text(
        (20, 40),
        "Born digital carbon accounting content stays on OpenDataLoader.",
        fontsize=9,
    )
    document.save(path)
    document.close()


def _write_scan_dominant_pdf(path: Path) -> None:
    document = pymupdf.open()
    text_page = document.new_page(width=240, height=320)
    text_page.insert_text((20, 40), "Born digital cover page", fontsize=9)
    for _ in range(4):
        scan_page = document.new_page(width=240, height=320)
        scan_page.insert_image(scan_page.rect, stream=_png_bytes())
    document.save(path)
    document.close()


class _RecordingBackend(BasePdfBackend):
    def __init__(self, name: str, calls: list[str], markdown: str) -> None:
        super().__init__()
        self.name = name
        self._calls = calls
        self._markdown = markdown

    def parse(self, source, options=None):
        self._calls.append(self.name)
        if not self._markdown:
            self.metadata[f"{self.name}_backend_error"] = f"{self.name} unavailable"
        return self._markdown, []


def _registry(
    calls: list[str],
    *,
    mineru_markdown: str = "MinerU parsed",
    opendataloader_markdown: str = "OpenDataLoader parsed",
) -> PdfBackendRegistry:
    registry = PdfBackendRegistry(default_backend="opendataloader", fallbacks="mineru")
    registry.register(
        "mineru",
        lambda _options: _RecordingBackend("mineru", calls, mineru_markdown),
    )
    registry.register(
        "opendataloader",
        lambda _options: _RecordingBackend(
            "opendataloader",
            calls,
            opendataloader_markdown,
        ),
    )
    return registry


def test_scanned_pdf_uses_mineru_before_configured_backend(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    _write_scan_pdf(source)
    calls: list[str] = []

    markdown, metadata = PdfParserService(_registry(calls)).parse(
        source,
        PdfParseOptions(backend="opendataloader"),
    )

    assert markdown == "MinerU parsed"
    assert calls == ["mineru"]
    assert metadata["pdf_parser_backend"] == "mineru"
    assert metadata["pdf_parser_backend_order"] == ["mineru", "opendataloader"]
    assert metadata["pdf_parser_route"] == "scanned_document_mineru"
    assert metadata["pdf_scan_detection"]["is_scanned_document"] is True


def test_born_digital_pdf_keeps_configured_backend_order(tmp_path: Path) -> None:
    source = tmp_path / "text.pdf"
    _write_text_pdf(source)
    calls: list[str] = []

    markdown, metadata = PdfParserService(_registry(calls)).parse(
        source,
        PdfParseOptions(backend="opendataloader"),
    )

    assert markdown == "OpenDataLoader parsed"
    assert calls == ["opendataloader"]
    assert metadata["pdf_parser_backend_order"] == ["opendataloader", "mineru"]
    assert metadata["pdf_parser_route"] == "configured_backend_order"
    assert metadata["pdf_scan_detection"]["is_scanned_document"] is False


def test_scan_dominant_pdf_uses_mineru_at_configured_ratio(tmp_path: Path) -> None:
    source = tmp_path / "scan-dominant.pdf"
    _write_scan_dominant_pdf(source)
    calls: list[str] = []

    markdown, metadata = PdfParserService(_registry(calls)).parse(
        source,
        PdfParseOptions(backend="opendataloader"),
    )

    assert markdown == "MinerU parsed"
    assert calls == ["mineru"]
    assert metadata["pdf_parser_route"] == "scanned_document_mineru"
    assert metadata["pdf_scan_detection"]["scanned_page_ratio"] == 0.8
    assert metadata["pdf_scan_detection"]["min_scanned_page_ratio"] == 0.8


def test_scanned_pdf_falls_back_when_mineru_is_unavailable(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    _write_scan_pdf(source)
    calls: list[str] = []

    markdown, metadata = PdfParserService(
        _registry(calls, mineru_markdown="")
    ).parse(
        source,
        PdfParseOptions(backend="opendataloader"),
    )

    assert markdown == "OpenDataLoader parsed"
    assert calls == ["mineru", "opendataloader"]
    assert metadata["pdf_parser_backend"] == "opendataloader"
    assert metadata["pdf_parser_attempts"] == [
        {
            "backend": "mineru",
            "success": False,
            "reason": "mineru unavailable",
        },
        {"backend": "opendataloader", "success": True},
    ]
