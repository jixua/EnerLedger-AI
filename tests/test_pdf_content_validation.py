from __future__ import annotations

import json

import pytest

from app.rag.core.parser.pdf.content_validation import (
    MarkdownPageContentSignals,
    PdfContentIssueSeverity,
    PdfContentValidationPolicy,
    PdfContentValidator,
    PdfSourcePageStructure,
    RegexMarkdownContentSignalDetector,
    formula_signature,
    validate_pdf_content,
)


def test_missing_confirmed_table_is_blocking_but_markdown_and_html_tables_pass() -> None:
    source = [PdfSourcePageStructure(page_number=1, table_count=1)]

    missing = validate_pdf_content(source, {1: "Only flattened prose remains."})
    markdown = validate_pdf_content(source, {1: "| A | B |\n|---|---|\n| 1 | 2 |"})
    html = validate_pdf_content(source, {1: "<table><tr><td>1</td></tr></table>"})

    assert missing.structural_passed is False
    assert [issue.code for issue in missing.blocking_issues] == ["TABLE_OUTPUT_MISSING"]
    assert missing.table_affected_pages == (1,)
    assert missing.source_table_count == 1
    assert missing.output_table_count == 0
    assert markdown.structural_passed is True
    assert markdown.output_table_count == 1
    assert html.structural_passed is True
    assert html.output_table_count == 1


def test_image_severity_uses_source_structure_not_short_text() -> None:
    source = [
        PdfSourcePageStructure(
            page_number=1,
            image_count=1,
            image_coverage_ratio=0.1,
        ),
        PdfSourcePageStructure(
            page_number=2,
            image_count=0,
            image_coverage_ratio=0.9,
        ),
        PdfSourcePageStructure(page_number=3),
    ]

    report = validate_pdf_content(source, {1: "", 2: "", 3: ""})

    assert report.structural_passed is False
    assert [issue.code for issue in report.warnings] == ["IMAGE_OUTPUT_MISSING"]
    assert [issue.code for issue in report.blocking_issues] == [
        "HIGH_IMAGE_COVERAGE_OUTPUT_MISSING"
    ]
    assert report.image_affected_pages == (1, 2)
    assert report.per_page[2].blocking_codes == ()
    assert report.per_page[2].warning_codes == ()


@pytest.mark.parametrize(
    "markdown",
    [
        "![emission boundary](images/page-1.png)",
        "![emission boundary][figure one]\n[Figure   One]: images/page-1.png",
        "![emission boundary][]\n[emission boundary]: images/page-1.png",
        '<img src="images/page-1.png" alt="boundary">',
        "[视觉描述|src=images/page-1.png: 该图展示企业排放边界与排放源。]",
        "图片说明：该图展示企业排放边界与排放源。",
        "<figure><figcaption>该图展示企业排放边界。</figcaption></figure>",
    ],
)
def test_image_reference_or_explicit_visual_description_satisfies_output(
    markdown: str,
) -> None:
    report = validate_pdf_content(
        [PdfSourcePageStructure(page_number=1, image_coverage_ratio=0.95)],
        {1: markdown},
    )

    assert report.structural_passed is True
    assert report.image_affected_pages == ()
    assert report.output_image_reference_count + report.output_visual_description_count > 0


def test_full_page_ocr_representation_covers_raster_tile_count() -> None:
    report = validate_pdf_content(
        [
            PdfSourcePageStructure(
                page_number=1,
                image_count=96,
                image_coverage_ratio=0.99,
            )
        ],
        {1: "图片说明：整页 OCR 已识别行业分类与代码表。"},
    )

    assert report.structural_passed is True
    assert report.blocking_issues == ()
    assert report.warnings == ()


def test_short_unlabelled_text_is_not_mistaken_for_a_visual_description() -> None:
    report = validate_pdf_content(
        [PdfSourcePageStructure(page_number=1, image_count=1)],
        {1: "图。"},
    )

    assert report.structural_passed is True
    assert [issue.code for issue in report.warnings] == ["IMAGE_OUTPUT_MISSING"]
    assert report.output_visual_description_count == 0


def test_empty_output_without_structural_expectations_is_not_a_content_failure() -> None:
    report = validate_pdf_content(
        [PdfSourcePageStructure(page_number=1)],
        {1: ""},
    )

    assert report.structural_passed is True
    assert report.blocking_issues == ()
    assert report.warnings == ()


def test_source_formula_is_blocking_and_odl_only_formula_hint_is_warning() -> None:
    report = validate_pdf_content(
        [
            PdfSourcePageStructure(page_number=1, formula_count=1),
            PdfSourcePageStructure(page_number=2, odl_formula_count=2),
        ],
        {1: "Formula was flattened to prose.", 2: "No formula remains."},
    )

    assert report.structural_passed is False
    assert [issue.code for issue in report.blocking_issues] == ["FORMULA_OUTPUT_MISSING"]
    assert [issue.code for issue in report.warnings] == ["ODL_FORMULA_OUTPUT_MISSING"]
    assert report.formula_affected_pages == (1, 2)
    assert report.source_formula_count == 1
    assert report.odl_formula_count == 2
    assert report.output_formula_count == 0


@pytest.mark.parametrize(
    "formula",
    [
        "$$E = AD \\times EF$$",
        r"\[E = AD \times EF\]",
        r"The factor is \(EF_i\).",
        r"The result is $E=AD\times EF$.",
        "E = AD × EF",
        "<math><mi>E</mi><mo>=</mo><mi>AD</mi></math>",
    ],
)
def test_formula_representations_are_detected(formula: str) -> None:
    report = validate_pdf_content(
        [PdfSourcePageStructure(page_number=1, formula_count=1)],
        {1: formula},
    )

    assert report.structural_passed is True
    assert report.output_formula_count == 1
    assert report.formula_affected_pages == ()


def test_warning_policy_is_injectable_and_report_is_json_serializable() -> None:
    policy = PdfContentValidationPolicy(
        high_image_coverage_ratio=0.8,
        min_visual_description_chars=4,
        missing_table_severity=PdfContentIssueSeverity.WARNING,
        missing_source_formula_severity=PdfContentIssueSeverity.WARNING,
    )
    report = PdfContentValidator(policy=policy).validate(
        [PdfSourcePageStructure(page_number=2, table_count=1, formula_count=1)],
        {2: ""},
    )
    serialized = report.to_dict()

    assert report.structural_passed is True
    assert report.blocking_issues == ()
    assert [issue.code for issue in report.warnings] == [
        "TABLE_OUTPUT_MISSING",
        "FORMULA_OUTPUT_MISSING",
    ]
    assert serialized["affected_pages"] == {
        "table": [2],
        "image": [],
        "formula": [2],
    }
    assert serialized["table_counts"] == {"source": 1, "output": 0}
    assert serialized["blocking_issue_count"] == 0
    assert serialized["warning_count"] == 2
    assert serialized["structural_passed"] is True
    assert "passed" not in serialized
    json.dumps(serialized, ensure_ascii=False, allow_nan=False)


def test_markdown_detection_strategy_is_injectable() -> None:
    class StubDetector:
        def detect(self, markdown: str) -> MarkdownPageContentSignals:
            assert markdown == "custom representation"
            return MarkdownPageContentSignals(
                table_count=1,
                image_reference_count=1,
                formula_count=1,
            )

    report = PdfContentValidator(detector=StubDetector()).validate(
        [
            PdfSourcePageStructure(
                page_number=1,
                table_count=1,
                image_count=1,
                formula_count=1,
            )
        ],
        {1: "custom representation"},
    )

    assert report.structural_passed is True
    assert report.blocking_issues == ()
    assert report.warnings == ()


def test_detector_ignores_currency_and_rejects_invalid_inputs() -> None:
    detector = RegexMarkdownContentSignalDetector()
    assert detector.detect("Budget: $100 and plain text").formula_count == 0

    with pytest.raises(ValueError):
        PdfSourcePageStructure(page_number=0)
    with pytest.raises(ValueError):
        PdfSourcePageStructure(page_number=1, image_coverage_ratio=1.1)
    with pytest.raises(ValueError):
        PdfContentValidationPolicy(high_image_coverage_ratio=-0.1)
    with pytest.raises(ValueError):
        validate_pdf_content(
            [
                PdfSourcePageStructure(page_number=1),
                PdfSourcePageStructure(page_number=1),
            ],
            {1: ""},
        )


def test_representations_inside_code_do_not_hide_missing_pdf_content() -> None:
    markdown = """```markdown
| A | B |
|---|---|
![diagram](images/page-1.png)
$$E=mc^2$$
```
"""
    report = validate_pdf_content(
        [
            PdfSourcePageStructure(
                page_number=1,
                table_count=1,
                image_coverage_ratio=0.9,
                formula_count=1,
            )
        ],
        {1: markdown},
    )

    assert report.structural_passed is False
    assert [issue.code for issue in report.blocking_issues] == [
        "TABLE_OUTPUT_MISSING",
        "HIGH_IMAGE_COVERAGE_OUTPUT_MISSING",
        "FORMULA_OUTPUT_MISSING",
    ]


def test_partial_structure_counts_are_reported_as_shortfalls() -> None:
    report = validate_pdf_content(
        [
            PdfSourcePageStructure(
                page_number=1,
                table_count=2,
                image_count=2,
                formula_count=2,
            )
        ],
        {
            1: (
                "| A | B |\n"
                "|---|---|\n"
                "| 1 | 2 |\n\n"
                "![diagram](images/one.png)\n\n"
                "$E=x$"
            )
        },
    )

    assert report.structural_passed is False
    assert [issue.code for issue in report.blocking_issues] == [
        "TABLE_OUTPUT_COUNT_SHORTFALL",
        "FORMULA_OUTPUT_COUNT_SHORTFALL",
    ]
    assert [issue.code for issue in report.warnings] == [
        "IMAGE_OUTPUT_COUNT_SHORTFALL"
    ]
    assert report.output_table_count == 1
    assert report.output_image_reference_count == 1
    assert report.output_formula_count == 1

    odl_report = validate_pdf_content(
        [PdfSourcePageStructure(page_number=1, odl_formula_count=2)],
        {1: "$x$"},
    )
    assert [issue.code for issue in odl_report.warnings] == [
        "ODL_FORMULA_OUTPUT_COUNT_SHORTFALL"
    ]


@pytest.mark.parametrize(
    "not_a_complete_formula",
    [
        "$100 USD$",
        '<img data-total="x=1" alt="no source">',
        "\\begin{equation}\nx=1\n\\end{align}",
        "$$ x=1",
        "<math>x=1",
    ],
)
def test_currency_markup_attributes_and_unclosed_formulas_do_not_false_pass(
    not_a_complete_formula: str,
) -> None:
    report = validate_pdf_content(
        [PdfSourcePageStructure(page_number=1, formula_count=1)],
        {1: not_a_complete_formula},
    )

    assert report.structural_passed is False
    assert report.output_formula_count == 0
    assert [issue.code for issue in report.blocking_issues] == [
        "FORMULA_OUTPUT_MISSING"
    ]


@pytest.mark.parametrize(
    "broken_reference",
    [
        '<img alt="missing src">',
        "![diagram][undefined]",
        "![diagram][]",
        "![diagram](   )",
    ],
)
def test_broken_image_references_do_not_satisfy_source_evidence(
    broken_reference: str,
) -> None:
    report = validate_pdf_content(
        [PdfSourcePageStructure(page_number=1, image_coverage_ratio=0.9)],
        {1: broken_reference},
    )

    assert report.structural_passed is False
    assert report.output_image_reference_count == 0
    assert [issue.code for issue in report.blocking_issues] == [
        "HIGH_IMAGE_COVERAGE_OUTPUT_MISSING"
    ]


@pytest.mark.parametrize(
    "empty_or_inconsistent_table",
    [
        "| Header |\n|---|",
        "| A | B |\n|---|---|\n| only one |",
        "<table></table>",
        "<table><tr><td></td></tr></table>",
        (
            "<table><tr><th>A</th><th>B</th></tr>"
            "<tr><td>only one</td></tr></table>"
        ),
    ],
)
def test_empty_or_inconsistent_tables_do_not_satisfy_source_evidence(
    empty_or_inconsistent_table: str,
) -> None:
    report = validate_pdf_content(
        [PdfSourcePageStructure(page_number=1, table_count=1)],
        {1: empty_or_inconsistent_table},
    )

    assert report.structural_passed is False
    assert report.output_table_count == 0


def test_html_colspan_and_markdown_code_pipes_keep_valid_column_counts() -> None:
    html = validate_pdf_content(
        [PdfSourcePageStructure(page_number=1, table_count=1)],
        {
            1: (
                '<table><tr><th colspan="2">Header</th></tr>'
                "<tr><td>1</td><td>2</td></tr></table>"
            )
        },
    )
    markdown = validate_pdf_content(
        [PdfSourcePageStructure(page_number=1, table_count=1)],
        {1: "| A | B |\n|---|---|\n| `x|y` | z |"},
    )

    assert html.structural_passed is True
    assert markdown.structural_passed is True


def test_same_table_count_cannot_hide_wrong_cell_values_or_shape() -> None:
    source = [
        PdfSourcePageStructure(
            page_number=1,
            table_matrices=(
                (
                    ("emission factor", "unit"),
                    ("1.25", "tCO2e/MWh"),
                ),
            ),
        )
    ]

    wrong_value = validate_pdf_content(
        source,
        {
            1: (
                "| emission factor | unit |\n"
                "|---|---|\n"
                "| 9.99 | tCO2e/MWh |"
            )
        },
    )
    wrong_shape = validate_pdf_content(
        source,
        {
            1: (
                "<table><tr><th>emission factor</th><th>unit</th></tr>"
                "<tr><td colspan='2'>1.25 tCO2e/MWh</td></tr></table>"
            )
        },
    )
    exact = validate_pdf_content(
        source,
        {
            1: (
                "<table><tr><th>emission factor</th><th>unit</th></tr>"
                "<tr><td>1.25</td><td>tCO2e/MWh</td></tr></table>"
            )
        },
    )

    assert [issue.code for issue in wrong_value.blocking_issues] == [
        "TABLE_CONTENT_MISMATCH"
    ]
    assert [issue.code for issue in wrong_shape.blocking_issues] == [
        "TABLE_CONTENT_MISMATCH"
    ]
    wrong_unit_case = validate_pdf_content(
        source,
        {1: "| emission factor | unit |\n|---|---|\n| 1.25 | tCO2e/mWh |"},
    )
    assert [issue.code for issue in wrong_unit_case.blocking_issues] == [
        "TABLE_CONTENT_MISMATCH"
    ]
    assert exact.structural_passed is True


def test_formula_count_cannot_hide_wrong_operator_variable_subscript_or_number() -> None:
    source = [
        PdfSourcePageStructure(
            page_number=1,
            formula_signatures=(formula_signature("E_i=AD_i×EF_i(2)"),),
        )
    ]

    for forged in (
        r"$$E_i=AD_i+EF_i\tag{2}$$",
        r"$$E_i=AD_i\times EF_j\tag{2}$$",
        r"$$e_i=AD_i\times EF_i\tag{2}$$",
        r"$$E=AD_i\times EF_i\tag{2}$$",
        r"$$E_i=AD_i\times EF_i\tag{3}$$",
    ):
        report = validate_pdf_content(source, {1: forged})
        assert report.output_formula_count == 1
        assert [issue.code for issue in report.blocking_issues] == [
            "FORMULA_TOKEN_MISMATCH"
        ]

    exact = validate_pdf_content(
        source,
        {1: r"$$E_i=AD_i\times EF_i\tag{2}$$"},
    )
    assert exact.structural_passed is True


def test_empty_source_and_page_set_mismatch_have_explicit_policies() -> None:
    with pytest.raises(ValueError, match="source_pages must not be empty"):
        validate_pdf_content([], {})

    empty_report = validate_pdf_content(
        [],
        {},
        policy=PdfContentValidationPolicy(allow_empty_source_pages=True),
    )
    assert empty_report.structural_passed is True
    assert empty_report.page_set_complete is True
    assert empty_report.evaluated_page_count == 0

    source = [
        PdfSourcePageStructure(page_number=1),
        PdfSourcePageStructure(page_number=2),
    ]
    with pytest.raises(ValueError, match="source/Markdown page set mismatch"):
        validate_pdf_content(source, {1: "", 3: ""})

    mismatch_report = validate_pdf_content(
        source,
        {1: "", 3: ""},
        policy=PdfContentValidationPolicy(allow_page_set_mismatch=True),
    )
    assert mismatch_report.structural_passed is True
    assert mismatch_report.page_set_complete is False
    assert mismatch_report.missing_markdown_pages == (2,)
    assert mismatch_report.unexpected_markdown_pages == (3,)
    assert mismatch_report.evaluated_page_count == 2


def test_only_native_integer_fields_are_accepted_and_report_stays_json_safe() -> None:
    class ForeignInt(int):
        pass

    with pytest.raises(TypeError, match="native int"):
        PdfSourcePageStructure(page_number=ForeignInt(1))
    with pytest.raises(TypeError, match="native int"):
        PdfSourcePageStructure(page_number=1, table_count=True)
    with pytest.raises(TypeError, match="native int"):
        MarkdownPageContentSignals(formula_count=1.0)
    with pytest.raises(TypeError, match="native int"):
        PdfContentValidationPolicy(min_visual_description_chars=True)
    with pytest.raises(TypeError, match="native int"):
        validate_pdf_content(
            [PdfSourcePageStructure(page_number=1)],
            {ForeignInt(1): ""},
        )

    payload = validate_pdf_content(
        [PdfSourcePageStructure(page_number=1, image_coverage_ratio=1)],
        {1: ""},
    ).to_dict()
    json.dumps(payload, ensure_ascii=False, allow_nan=False)
