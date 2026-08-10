from __future__ import annotations

import base64
from io import BytesIO
from types import SimpleNamespace

import docx
import pytest

from app.rag.core.markdown_parser.models import ElementType
from app.rag.core.markdown_parser.parser import MarkdownParser
from app.rag.core.parser.pdf.table_structure import PdfTableStructureExtractor
from app.rag.core.parser.providers.word_parser import WordParser
from app.services.document_ingestion import (
    DocumentQualityGateError,
    SimpleDocumentIngestionService,
)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


class _Storage:
    def __init__(self) -> None:
        self.uploads: list[dict] = []

    def upload_bytes(self, **kwargs) -> None:
        self.uploads.append(kwargs)

    @staticmethod
    def build_object_url(bucket: str, object_key: str) -> str:
        return f"http://minio.local/{bucket}/{object_key}"


def _complex_docx(tmp_path):
    document = docx.Document()
    document.add_heading("能碳核算表", level=1)
    table = document.add_table(rows=3, cols=3)
    table.cell(0, 0).text = "类别"
    table.cell(0, 1).text = "排放量"
    table.cell(0, 1).merge(table.cell(0, 2))
    table.cell(1, 0).text = "范围一"
    table.cell(1, 1).text = "2024"
    table.cell(1, 2).text = "2025"
    table.cell(2, 0).text = "燃料"
    table.cell(2, 1).text = "10"
    table.cell(2, 2).text = "9"
    nested = table.cell(2, 0).add_table(rows=1, cols=2)
    nested.cell(0, 0).text = "煤"
    nested.cell(0, 1).text = "5"
    document.add_picture(BytesIO(_PNG))
    path = tmp_path / "complex.docx"
    document.save(path)
    return path


def _quality_service() -> SimpleDocumentIngestionService:
    service = object.__new__(SimpleDocumentIngestionService)
    service._pdf_table_structure_extractor = PdfTableStructureExtractor()
    return service


def test_word_parser_persists_images_and_preserves_complex_table(tmp_path) -> None:
    source = _complex_docx(tmp_path)
    storage = _Storage()
    parser = WordParser(
        storage=storage,
        image_bucket="parsed",
        image_prefix="documents/1/images",
    )

    markdown = parser.parse(source)
    result = MarkdownParser().parse(markdown, source_file=source.name)
    report = PdfTableStructureExtractor().extract(markdown, merge_continuations=False)

    assert len(storage.uploads) == 1
    assert "http://minio.local/parsed/documents/1/images/" in markdown
    assert "mock-minio://" not in markdown
    assert parser.metadata["image_assets_persisted"] is True
    assert parser.metadata["source_image_reference_count"] == 1
    assert parser.metadata["image_occurrence_count"] == 1
    assert parser.metadata["source_table_count"] == 2
    assert parser.metadata["source_top_level_table_count"] == 1
    assert parser.metadata["source_nested_table_count"] == 1
    assert parser.metadata["mammoth_html_table_count"] == 2
    assert len(result.tables) == 1
    assert len(report.tables) == 1
    assert report.tables[0].cells[1].column_span == 2
    assert "煤" in str(report.tables[0].text_matrix)


def test_word_quality_attaches_structure_and_passes_complete_docx(tmp_path) -> None:
    source = _complex_docx(tmp_path)
    parser = WordParser(
        storage=_Storage(),
        image_bucket="parsed",
        image_prefix="documents/1/images",
    )
    markdown = parser.parse(source)
    parse_result = MarkdownParser().parse(markdown, source_file=source.name)
    parse_output = {
        "markdown": markdown,
        "parse_result": parse_result,
        "metadata": parser.extract_metadata(),
    }

    status, report = _quality_service()._process_word_quality(
        identity=SimpleNamespace(filename=source.name),
        parse_output=parse_output,
        markdown=markdown,
    )

    assert status == "PASSED"
    assert report["structured_table_count"] == 1
    table = next(e for e in parse_result.elements if e.type is ElementType.TABLE)
    assert table.metadata["table_structure"]["cells"][1]["column_span"] == 2


def test_word_quality_rejects_unpersisted_images(tmp_path) -> None:
    source = _complex_docx(tmp_path)
    parser = WordParser()
    markdown = parser.parse(source)
    parse_output = {
        "markdown": markdown,
        "parse_result": MarkdownParser().parse(markdown, source_file=source.name),
        "metadata": parser.extract_metadata(),
    }

    with pytest.raises(DocumentQualityGateError) as error:
        _quality_service()._process_word_quality(
            identity=SimpleNamespace(filename=source.name),
            parse_output=parse_output,
            markdown=markdown,
        )

    assert error.value.error_code == "WORD_CONTENT_VALIDATION_FAILED"
    assert "WORD_IMAGE_ASSETS_NOT_PERSISTED" in error.value.quality_report["blocking_issues"]
