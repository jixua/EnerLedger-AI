from __future__ import annotations

from app.rag.core.parser.pdf.table_structure import (
    OdlTableAbEvaluator,
    OdlTableAbReport,
    OdlTableMethod,
    OdlTableStrategyMetrics,
    PdfTableStructureExtractor,
)


def test_html_table_preserves_spans_header_hierarchy_and_source_page() -> None:
    markdown = """<!-- ODL_PAGE:3 -->
表 1 能碳排放统计
<table>
  <caption>能碳排放统计</caption>
  <thead>
    <tr><th rowspan="2">年份</th><th colspan="2">排放量</th></tr>
    <tr><th>范围一</th><th>范围二</th></tr>
  </thead>
  <tbody><tr><td>2025</td><td>10 tCO2e</td><td>20 tCO2e</td></tr></tbody>
</table>
"""

    report = PdfTableStructureExtractor().extract(markdown)

    assert report.raw_table_count == 1
    assert report.merged_table_count == 0
    table = report.tables[0]
    assert table.title == "能碳排放统计"
    assert table.source_pages == (3,)
    assert (table.row_count, table.column_count, table.header_row_count) == (3, 3, 2)
    assert table.header_hierarchy == (
        ("年份",),
        ("排放量", "范围一"),
        ("排放量", "范围二"),
    )
    year_cell = next(cell for cell in table.cells if cell.text == "年份")
    emission_cell = next(cell for cell in table.cells if cell.text == "排放量")
    assert year_cell.row_span == 2
    assert emission_cell.column_span == 2
    payload = table.to_dict()
    assert payload["source_page_range"] == {"start": 3, "end": 3}
    assert payload["text_matrix"] == [
        ["年份", "排放量", "排放量"],
        ["年份", "范围一", "范围二"],
        ["2025", "10 tCO2e", "20 tCO2e"],
    ]


def test_consecutive_pages_merge_repeated_header_and_drop_duplicate_header() -> None:
    markdown = """<!-- ODL_PAGE:1 -->
表 2 企业能源消费量

| 年份 | 能源品种 | 消费量 |
| --- | --- | --- |
| 2024 | 电力 | 120 MWh |

<!-- ODL_PAGE:2 -->
表 2 企业能源消费量（续）

| 年份 | 能源品种 | 消费量 |
| --- | --- | --- |
| 2025 | 天然气 | 80 Nm3 |
"""

    report = PdfTableStructureExtractor().extract(markdown)

    assert report.raw_table_count == 2
    assert report.merged_table_count == 1
    table = report.tables[0]
    assert table.source_pages == (1, 2)
    assert table.part_table_ids == ("table-0001", "table-0002")
    assert table.text_matrix == (
        ("年份", "能源品种", "消费量"),
        ("2024", "电力", "120 MWh"),
        ("2025", "天然气", "80 Nm3"),
    )
    decision = report.continuation_decisions[0]
    assert decision.merged is True
    assert "CONSECUTIVE_PAGES" in decision.reasons
    assert "REPEATED_HEADER_MATCH" in decision.reasons


def test_same_width_unrelated_tables_are_not_merged() -> None:
    markdown = """<!-- ODL_PAGE:1 -->
排放因子
| 类型 | 因子 |
| --- | --- |
| 电力 | 0.5 |
<!-- ODL_PAGE:2 -->
组织信息
| 部门 | 负责人 |
| --- | --- |
| 运营 | 张三 |
"""

    report = PdfTableStructureExtractor().extract(markdown)

    assert len(report.tables) == 2
    assert report.merged_table_count == 0
    decision = report.continuation_decisions[0]
    assert decision.merged is False
    assert "INSUFFICIENT_CONTINUATION_EVIDENCE" in decision.reasons


def test_repeated_generic_header_with_different_titles_is_not_merged() -> None:
    markdown = """<!-- ODL_PAGE:1 -->
表 3 排放因子
| 类型 | 数值 |
| --- | --- |
| 电力 | 0.5 |
<!-- ODL_PAGE:2 -->
表 4 能源价格
| 类型 | 数值 |
| --- | --- |
| 天然气 | 3.2 |
"""

    report = PdfTableStructureExtractor().extract(markdown)

    assert len(report.tables) == 2
    assert report.merged_table_count == 0
    decision = report.continuation_decisions[0]
    assert decision.merged is False
    assert "CONSECUTIVE_PAGES" in decision.reasons
    assert "COLUMN_COUNT_MATCH" in decision.reasons
    assert "REPEATED_HEADER_MATCH" in decision.reasons
    assert "TABLE_TITLE_MATCH" not in decision.reasons
    assert "INSUFFICIENT_CONTINUATION_EVIDENCE" in decision.reasons


def test_same_title_without_repeated_header_or_continuation_label_is_not_enough() -> None:
    markdown = """<!-- ODL_PAGE:1 -->
排放数据
| 年份 | 排放量 |
| --- | --- |
| 2024 | 100 |
<!-- ODL_PAGE:2 -->
排放数据
| 范围 | 单位 |
| --- | --- |
| 范围一 | tCO2e |
"""

    report = PdfTableStructureExtractor().extract(markdown)

    assert len(report.tables) == 2
    assert report.continuation_decisions[0].merged is False
    assert "TABLE_TITLE_MATCH" in report.continuation_decisions[0].reasons


def test_table_without_odl_marker_is_structured_but_never_gets_guessed_page() -> None:
    report = PdfTableStructureExtractor().extract(
        "| 字段 | 值 |\n| --- | --- |\n| imageFile12 | 1 |"
    )

    assert report.warnings == ("MISSING_ODL_PAGE_MARKERS",)
    assert report.tables[0].source_pages == ()
    assert "MISSING_PAGE_PROVENANCE" in report.tables[0].warnings
    assert report.tables[0].to_dict()["page_provenance_complete"] is False


def test_mixed_markdown_and_html_tables_keep_original_offset_order() -> None:
    markdown = """<!-- ODL_PAGE:2 -->
表 A 排放因子
| 类型 | 数值 |
| --- | --- |
| 电力 | 0.5 |

表 B 活动数据
<table><tr><th>年份</th><th>用量</th></tr><tr><td>2025</td><td>10</td></tr></table>
"""

    report = PdfTableStructureExtractor().extract(markdown, merge_continuations=False)

    assert [table.table_id for table in report.tables] == ["table-0001", "table-0002"]
    assert [table.source_format.value for table in report.tables] == ["MARKDOWN", "HTML"]
    assert report.tables[0].text_matrix[1] == ("电力", "0.5")
    assert report.tables[1].text_matrix[1] == ("2025", "10")
    assert report.tables[0].source_offset_ranges[0][0] < report.tables[1].source_offset_ranges[0][0]
    assert report.tables[0].source_line_ranges == ((2, 4),)
    assert report.tables[1].source_line_ranges == ((7, 7),)


def test_odl_default_cluster_ab_metrics_are_json_serializable_and_conservative() -> None:
    extractor = PdfTableStructureExtractor()
    default_report = extractor.extract(
        "<!-- ODL_PAGE:1 -->\n| A | B |\n| --- | --- |\n| 1 | 2 |"
    )
    cluster_report = extractor.extract(
        "<!-- ODL_PAGE:1 -->\n| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
    )
    default = OdlTableStrategyMetrics.from_report(
        OdlTableMethod.DEFAULT,
        False,
        default_report,
        page_marker_count=1,
    )
    cluster = OdlTableStrategyMetrics.from_report(
        OdlTableMethod.CLUSTER,
        True,
        cluster_report,
        page_marker_count=1,
    )

    comparison = OdlTableAbReport.compare(default, cluster)

    assert comparison.recommendation is OdlTableMethod.CLUSTER
    assert comparison.to_dict()["cluster"]["non_empty_cell_count"] == 6
    assert "CLUSTER_NON_EMPTY_CELL_GAIN:2" in comparison.reasons

    direct = OdlTableAbEvaluator().evaluate(
        default_markdown=(
            "<!-- ODL_PAGE:1 -->\n| A | B |\n| --- | --- |\n| 1 | 2 |"
        ),
        cluster_markdown=(
            "<!-- ODL_PAGE:1 -->\n| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
        ),
        cluster_markdown_with_html=True,
    )
    assert direct.to_dict() == comparison.to_dict()
