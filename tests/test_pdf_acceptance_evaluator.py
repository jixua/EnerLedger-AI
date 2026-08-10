from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from app.rag.evaluation.pdf_acceptance import (
    AcceptanceStatus,
    PdfAcceptanceEvaluator,
    evaluate_pdf_acceptance,
)


def _gold() -> dict:
    return {
        "document": {
            "file_name": "排放报告.pdf",
            "version": "v2",
            "source_page_count": 3,
        },
        "pages": [
            {
                "page_number": 1,
                "body_text": "2024 年排放因子为 2.50 tCO2e/t。",
                "is_scanned": False,
            },
            {
                "page_number": 2,
                "body_text": "活动数据与排放因子相乘得到排放量。 (1)",
                "is_scanned": True,
            },
            {"page_number": 3, "body_text": "系统边界如图所示。", "is_scanned": False},
        ],
        "scan_pages": [2],
        "critical_tokens": [
            {"category": "year", "value": "2024", "page_number": 1},
            {"category": "emission_factor", "value": "2.50", "page_number": 1},
            {"category": "unit", "value": "tCO2e/t", "page_number": 1},
            {"category": "formula_number", "value": "(1)", "page_number": 2},
        ],
        "formulas": [
            {
                "formula_id": "(1)",
                "page_number": 2,
                "expression": "E = AD × EF",
            }
        ],
        "tables": [
            {
                "table_id": "simple-1",
                "kind": "simple",
                "source_pages": [1],
                "cells": [["年份", "数值"], ["2024", "2.50"]],
            },
            {
                "table_id": "complex-1",
                "kind": "complex",
                "source_pages": [1, 2],
                "header_levels": [["排放源"], ["类型", "排放量"]],
                "cells": [["类型", "排放量"], ["燃烧", "10"]],
            },
        ],
        "visuals": [
            {
                "visual_id": "figure-1",
                "page_number": 3,
                "nodes": ["企业边界", "排放源"],
                "values": ["10 tCO2e"],
                "arrows": [
                    {"source": "排放源", "target": "企业边界", "type": "included_in"}
                ],
            }
        ],
        "citations": [
            {"file_name": "排放报告.pdf", "page_number": 1, "version": "v2"}
        ],
        "noise_chunk_ids": ["header-1"],
    }


def _prediction() -> dict:
    return {
        "pages": [
            {
                "page_number": 1,
                "text": "<!-- page: 1 -->\n2024 年排放因子为 2.50 tCO2e/t。",
                "ocr_completed": False,
            },
            {
                "page_number": 2,
                "text": "活动数据与排放因子相乘得到排放量。 (1)",
                "ocr_completed": True,
            },
            {"page_number": 3, "text": "系统边界如图所示。\n![figure](figure.png)"},
        ],
        "ocr_pages": [2],
        "formulas": [
            {
                "formula_id": "(1)",
                "page_number": 2,
                "expression": "E = AD × EF",
            }
        ],
        "tables": deepcopy(_gold()["tables"]),
        "visuals": deepcopy(_gold()["visuals"]),
        "citations": deepcopy(_gold()["citations"]),
        "chunks": [
            {
                "id": "figure-1",
                "chunk_type": "image",
                "retrieval_eligible": True,
                "visual_description": "该图展示排放源通过箭头连入企业边界。",
            },
            {
                "id": "seal-1",
                "chunk_type": "image",
                "retrieval_eligible": False,
                "content": "未提供图片说明",
            },
        ],
        "recall_results": [
            {"chunk_id": "content-1"},
            {"chunk_id": "content-2"},
            {"chunk_id": "content-3"},
            {"chunk_id": "content-4"},
            {"chunk_id": "content-5"},
        ],
    }


def _metrics_by_name(report) -> dict:
    return {metric.name: metric for metric in report.metrics}


def test_complete_gold_and_prediction_pass_all_acceptance_metrics() -> None:
    report = evaluate_pdf_acceptance(_gold(), _prediction())
    serialized = report.to_dict()

    assert report.status is AcceptanceStatus.PASS
    assert report.acceptance_ready is True
    assert len(report.metrics) == 13
    assert all(metric.status is AcceptanceStatus.PASS for metric in report.metrics)
    assert serialized["summary"] == {
        "metric_count": 13,
        "passed": 13,
        "failed": 0,
        "not_evaluable": 0,
    }
    json.dumps(serialized, ensure_ascii=False, allow_nan=False)


def test_missing_gold_is_not_reported_as_passed() -> None:
    report = PdfAcceptanceEvaluator().evaluate({}, {"chunks": [], "recall_results": []})

    assert report.status is AcceptanceStatus.NOT_EVALUABLE
    assert report.acceptance_ready is False
    assert report.not_evaluable_metrics
    assert _metrics_by_name(report)["body_character_accuracy"].reason is not None


def test_each_quality_family_can_block_acceptance() -> None:
    gold = _gold()
    prediction = _prediction()
    prediction["pages"] = [prediction["pages"][0], prediction["pages"][2]]
    prediction["ocr_pages"] = []
    prediction["pages"][0]["text"] = "错误文本"
    prediction["formulas"][0]["expression"] = "E = AD + EF"
    prediction["tables"][0]["cells"][1][1] = "999"
    prediction["tables"][1]["source_pages"] = [2]
    prediction["visuals"][0]["arrows"] = []
    prediction["citations"][0]["version"] = "v1"
    prediction["chunks"][0]["visual_description"] = "未提供图片说明"
    prediction["recall_results"][0] = {"chunk_id": "header-1"}

    report = evaluate_pdf_acceptance(gold, prediction)
    metrics = _metrics_by_name(report)

    assert report.status is AcceptanceStatus.FAIL
    expected_failures = {
        "page_count_accuracy",
        "page_order_coverage",
        "scan_page_ocr_coverage",
        "body_character_accuracy",
        "critical_token_exactness",
        "formula_exactness",
        "simple_table_cell_accuracy",
        "cross_page_table_exactness",
        "visual_structure_exactness",
        "citation_exactness",
        "undescribed_image_chunks",
        "top5_noise_results",
    }
    assert {name for name, metric in metrics.items() if metric.status is AcceptanceStatus.FAIL} >= (
        expected_failures
    )


def test_complex_table_threshold_is_98_percent() -> None:
    cells = [[str(index) for index in range(100)]]
    gold = {
        "tables": [{"table_id": "t", "kind": "complex", "cells": cells}],
    }
    prediction = {
        "tables": deepcopy(gold["tables"]),
    }
    prediction["tables"][0]["cells"][0][0] = "wrong"

    one_wrong = _metrics_by_name(evaluate_pdf_acceptance(gold, prediction))
    assert one_wrong["complex_table_cell_accuracy"].value == 0.99
    assert one_wrong["complex_table_cell_accuracy"].status is AcceptanceStatus.PASS

    prediction["tables"][0]["cells"][0][1] = "wrong"
    prediction["tables"][0]["cells"][0][2] = "wrong"
    three_wrong = _metrics_by_name(evaluate_pdf_acceptance(gold, prediction))
    assert three_wrong["complex_table_cell_accuracy"].value == 0.97
    assert three_wrong["complex_table_cell_accuracy"].status is AcceptanceStatus.FAIL


def test_unexpected_structured_facts_are_not_hidden_by_complete_gold_recall() -> None:
    gold = _gold()
    prediction = _prediction()
    prediction["critical_tokens"] = deepcopy(gold["critical_tokens"])
    prediction["critical_tokens"].append(
        {"category": "year", "value": "2099", "page_number": 1}
    )
    prediction["tables"].append(
        {
            "table_id": "hallucinated-continuation",
            "kind": "complex",
            "source_pages": [2, 3],
            "header_levels": [["x"]],
            "cells": [],
        }
    )

    metrics = _metrics_by_name(evaluate_pdf_acceptance(gold, prediction))

    assert metrics["critical_token_exactness"].status is AcceptanceStatus.FAIL
    assert metrics["critical_token_exactness"].details["unexpected"]
    assert metrics["cross_page_table_exactness"].status is AcceptanceStatus.FAIL
    assert metrics["cross_page_table_exactness"].details["predicted_cross_page_tables"] == 2


def test_runtime_image_structure_metadata_is_checked() -> None:
    prediction = {
        "chunks": [
            {
                "id": "image-1",
                "structure": {
                    "chunk_role": "image",
                    "retrieval_eligible": True,
                    "image_asset": {
                        "retrieval_asset": True,
                        "retrieval_text": "该图展示排放源、排放边界及两者的箭头关系。",
                    },
                },
            }
        ]
    }

    metric = _metrics_by_name(evaluate_pdf_acceptance({}, prediction))[
        "undescribed_image_chunks"
    ]
    assert metric.status is AcceptanceStatus.PASS
    assert metric.details["retrievable_image_chunks"] == 1


def test_text_accuracy_ignores_layout_whitespace_page_markers_and_image_links() -> None:
    gold = {"pages": [{"page_number": 1, "body_text": "排放 因子\n2.50"}]}
    prediction = {
        "pages": [
            {
                "page_number": 1,
                "text": "<!-- page_number: 1 -->\n排放因子 2.50\n![logo](logo.png)",
            }
        ]
    }

    metric = _metrics_by_name(evaluate_pdf_acceptance(gold, prediction))[
        "body_character_accuracy"
    ]
    assert metric.value == 1.0
    assert metric.status is AcceptanceStatus.PASS


def test_text_accuracy_ignores_runtime_odl_page_marker() -> None:
    report = evaluate_pdf_acceptance(
        {"pages": [{"page_number": 1, "body_text": "排放因子 2.50"}]},
        {
            "pages": [
                {
                    "page_number": 1,
                    "text": "<!-- ODL_PAGE:1 -->\n排放因子 2.50",
                }
            ]
        },
    )

    assert _metrics_by_name(report)["body_character_accuracy"].value == 1.0


def test_cli_writes_report_and_uses_distinct_exit_codes(tmp_path: Path) -> None:
    gold_path = tmp_path / "gold.json"
    prediction_path = tmp_path / "prediction.json"
    output_path = tmp_path / "report.json"
    gold_path.write_text(json.dumps(_gold(), ensure_ascii=False), encoding="utf-8")
    prediction_path.write_text(json.dumps(_prediction(), ensure_ascii=False), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/evaluate_pdf_acceptance.py",
            "--gold",
            str(gold_path),
            "--prediction",
            str(prediction_path),
            "--output",
            str(output_path),
            "--compact",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "PASS"
    assert json.loads(output_path.read_text(encoding="utf-8"))["acceptance_ready"] is True


@pytest.mark.parametrize(
    ("key", "metric_name"),
    [
        ("formulas", "formula_exactness"),
        ("tables", "simple_table_cell_accuracy"),
        ("visuals", "visual_structure_exactness"),
        ("citations", "citation_exactness"),
    ],
)
def test_malformed_gold_collection_is_never_treated_as_an_empty_pass(
    key: str,
    metric_name: str,
) -> None:
    report = evaluate_pdf_acceptance({key: "not-an-array"}, {key: []})
    metric = _metrics_by_name(report)[metric_name]

    assert metric.status is AcceptanceStatus.NOT_EVALUABLE
    assert metric.reason and "必须是对象数组" in metric.reason


@pytest.mark.parametrize(
    ("key", "metric_name"),
    [
        ("formulas", "formula_exactness"),
        ("tables", "complex_table_cell_accuracy"),
        ("visuals", "visual_structure_exactness"),
        ("citations", "citation_exactness"),
    ],
)
def test_malformed_prediction_collection_fails_instead_of_vacuously_passing(
    key: str,
    metric_name: str,
) -> None:
    report = evaluate_pdf_acceptance({key: []}, {key: "not-an-array"})
    metric = _metrics_by_name(report)[metric_name]

    assert metric.status is AcceptanceStatus.FAIL
    assert metric.details["schema_error"]


def test_structured_pdf_table_runtime_dict_is_evaluated_from_parse_quality() -> None:
    gold = {
        "tables": [
            {
                "table_id": "table-0001",
                "kind": "simple",
                "source_pages": [1],
                "cells": [["年份", "排放量"], ["2024", "10 tCO2e"]],
            }
        ]
    }
    runtime_table = {
        "table_id": "table-0001",
        "title": "排放量",
        "source_format": "MARKDOWN",
        "source_page_range": {"start": 1, "end": 1},
        "source_pages": [1],
        "page_provenance_complete": True,
        "row_count": 2,
        "column_count": 2,
        "header_row_count": 1,
        "header_hierarchy": [["年份"], ["排放量"]],
        "cells": [
            {
                "cell_id": "c1",
                "row_index": 0,
                "column_index": 0,
                "text": "年份",
                "row_span": 1,
                "column_span": 1,
            },
            {
                "cell_id": "c2",
                "row_index": 0,
                "column_index": 1,
                "text": "排放量",
                "row_span": 1,
                "column_span": 1,
            },
            {
                "cell_id": "c3",
                "row_index": 1,
                "column_index": 0,
                "text": "2024",
                "row_span": 1,
                "column_span": 1,
            },
            {
                "cell_id": "c4",
                "row_index": 1,
                "column_index": 1,
                "text": "10 tCO2e",
                "row_span": 1,
                "column_span": 1,
            },
        ],
        "cell_reference_matrix": [["c1", "c2"], ["c3", "c4"]],
        "text_matrix": [["年份", "排放量"], ["2024", "10 tCO2e"]],
        "part_table_ids": ["table-0001"],
        "warnings": [],
    }
    prediction = {
        "parse_quality": {
            "table_structure": {
                "schema_version": 1,
                "tables": [runtime_table],
            }
        }
    }

    metrics = _metrics_by_name(evaluate_pdf_acceptance(gold, prediction))

    assert metrics["simple_table_cell_accuracy"].status is AcceptanceStatus.PASS
    assert metrics["simple_table_cell_accuracy"].value == 1.0


def test_parse_quality_runtime_pages_ocr_and_visuals_are_normalized() -> None:
    relationship = {"source": "排放源", "relation": "included_in", "target": "企业边界"}
    gold = {
        "pages": [
            {"page_number": 1, "is_scanned": False},
            {"page_number": 2, "is_scanned": True},
        ],
        "scan_pages": [2],
        "visuals": [
            {
                "source_page": 1,
                "entities": ["排放源", "企业边界"],
                "relationships": [relationship],
                "key_values": [{"label": "排放量", "value": "10"}],
                "units": ["tCO2e"],
            }
        ],
    }
    prediction = {
        "parse_quality": {
            "page_quality": {
                "page_markers": [1, 2],
                "per_page": [
                    {"page_number": 1, "ocr_applied": False, "low_confidence": False},
                    {"page_number": 2, "ocr_applied": True, "low_confidence": False},
                ],
            },
            "fallback": {
                "results": [
                    {
                        "page_number": 1,
                        "method": "vision",
                        "structured_data": {
                            "source_page": 1,
                            "summary": "系统边界图",
                            "entities": ["排放源", "企业边界"],
                            "relationships": [relationship],
                            "key_values": [{"label": "排放量", "value": "10"}],
                            "units": ["tCO2e"],
                        },
                    },
                    {
                        "page_number": 2,
                        "method": "ocr",
                        "structured_data": {
                            "source_page": 2,
                            "summary": "",
                            "entities": [],
                            "relationships": [],
                            "key_values": [],
                            "units": [],
                        },
                    },
                ]
            },
        }
    }

    metrics = _metrics_by_name(evaluate_pdf_acceptance(gold, prediction))

    assert metrics["page_count_accuracy"].status is AcceptanceStatus.PASS
    assert metrics["page_order_coverage"].status is AcceptanceStatus.PASS
    assert metrics["scan_page_ocr_coverage"].status is AcceptanceStatus.PASS
    assert metrics["visual_structure_exactness"].status is AcceptanceStatus.PASS


def test_parse_quality_runtime_image_assets_are_checked_for_retrieval_text() -> None:
    prediction = {
        "parse_quality": {
            "image_assets": {
                "assets": [
                    {
                        "source_ref": "chart.png",
                        "retrieval_asset": True,
                        "retrieval_text": "图表展示排放量从 10 增长到 12 tCO2e。",
                    },
                    {
                        "source_ref": "seal.png",
                        "retrieval_asset": False,
                        "retrieval_text": None,
                    },
                ]
            }
        }
    }

    metric = _metrics_by_name(evaluate_pdf_acceptance({}, prediction))[
        "undescribed_image_chunks"
    ]

    assert metric.status is AcceptanceStatus.PASS
    assert metric.details["source"] == "parse_quality.image_assets.assets"
    assert metric.details["retrievable_image_chunks"] == 1


def test_critical_tokens_use_boundaries_instead_of_substring_matches() -> None:
    gold = {
        "critical_tokens": [
            {"category": "year", "value": "2024", "page_number": 1},
            {"category": "factor", "value": "2.50", "page_number": 1},
        ]
    }
    prediction = {"pages": [{"page_number": 1, "text": "12024 年，因子 12.500。"}]}

    metric = _metrics_by_name(evaluate_pdf_acceptance(gold, prediction))[
        "critical_token_exactness"
    ]

    assert metric.status is AcceptanceStatus.FAIL
    assert metric.value == 0.0
    assert len(metric.details["missing"]) == 2


@pytest.mark.parametrize(
    ("gold_count", "predicted_text", "expected_value", "unexpected_count"),
    [
        (2, "2024年", 0.5, 0),
        (1, "2024年与2024年", 0.5, 1),
    ],
)
def test_critical_token_occurrence_counts_must_match_exactly(
    gold_count: int,
    predicted_text: str,
    expected_value: float,
    unexpected_count: int,
) -> None:
    gold = {
        "critical_tokens": [
            {"category": "year", "value": "2024", "page_number": 1}
            for _index in range(gold_count)
        ]
    }
    prediction = {"pages": [{"page_number": 1, "text": predicted_text}]}

    metric = _metrics_by_name(evaluate_pdf_acceptance(gold, prediction))[
        "critical_token_exactness"
    ]

    assert metric.status is AcceptanceStatus.FAIL
    assert metric.value == expected_value
    assert len(metric.details["unexpected"]) == unexpected_count


def test_invalid_nested_visual_or_citation_schema_cannot_pass() -> None:
    gold = {
        "visuals": [
            {
                "page_number": 1,
                "entities": ["企业"],
                "relationships": [],
                "values": [],
            }
        ],
        "citations": [
            {"file_name": "report.pdf", "page_number": 1, "version": "v1"}
        ],
    }
    prediction = {
        "visuals": [{"page_number": 1, "entities": "企业"}],
        "citations": [{"file_name": "report.pdf", "page_numbers": "1", "version": "v1"}],
    }

    metrics = _metrics_by_name(evaluate_pdf_acceptance(gold, prediction))

    assert metrics["visual_structure_exactness"].status is AcceptanceStatus.FAIL
    assert metrics["citation_exactness"].status is AcceptanceStatus.FAIL
