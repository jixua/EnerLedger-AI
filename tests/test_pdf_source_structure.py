from __future__ import annotations

from pathlib import Path

import pymupdf

from app.rag.core.parser.pdf.quality import PdfQualityAnalyzer
from app.rag.core.parser.pdf.source_structure import PdfSourceStructureInspector


def _write_born_digital_table_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=300, height=220)
    x_positions = (30, 150, 270)
    y_positions = (40, 80, 120)
    for x in x_positions:
        page.draw_line((x, y_positions[0]), (x, y_positions[-1]))
    for y in y_positions:
        page.draw_line((x_positions[0], y), (x_positions[-1], y))
    page.insert_text((40, 65), "Factor", fontsize=10)
    page.insert_text((160, 65), "Unit", fontsize=10)
    page.insert_text((40, 105), "1.25", fontsize=10)
    page.insert_text((160, 105), "tCO2e/MWh", fontsize=10)
    document.save(path)
    document.close()


def test_source_inspector_extracts_born_digital_table_cell_matrix(tmp_path: Path) -> None:
    pdf_path = tmp_path / "source-table.pdf"
    _write_born_digital_table_pdf(pdf_path)
    markdown = (
        "<!-- ODL_PAGE:1 -->\n"
        "| Factor | Unit |\n"
        "|---|---|\n"
        "| 1.25 | tCO2e/MWh |"
    )
    quality = PdfQualityAnalyzer(min_text_retention_ratio=0.97).analyze(
        pdf_path,
        markdown,
    )

    inspection = PdfSourceStructureInspector().inspect(pdf_path, markdown, quality)

    page = inspection.pages[0]
    assert page.table_count == 1
    assert page.table_matrices == (
        (
            ("Factor", "Unit"),
            ("1.25", "tCO2e/MWh"),
        ),
    )
    assert inspection.to_dict()["pages"][0]["table_matrices"] == [
        [["Factor", "Unit"], ["1.25", "tCO2e/MWh"]]
    ]


def test_source_formula_detection_rejects_url_query_parameters() -> None:
    inspector = PdfSourceStructureInspector()

    assert inspector._looks_like_formula("https://example.test/search?a=b") is False
    assert inspector._looks_like_formula("E_i = AD_i × EF_i (2)") is True
